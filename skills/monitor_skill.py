"""Monitor Skill (Risk/Guard Agent + Broker-Native Tripwire) — Section 3.6.

Agent/Model: Claude (risk judgment). Hard stop-loss/target enforcement is
intended to run as a broker-native GTT/OCO order placed at trade entry,
not a polling loop — this codebase has no real broker order integration
yet (see Section 3.1's Phase 3+ TODO), so the code-side price check below
is a deterministic backup/visibility check, not the primary safety net.

Runs on a light cadence (once/twice daily, aligned with the pre-market/
post-market runs — not tight intraday polling):

1. A code-side stop-loss/target check against each active position's
   latest price — instant, deterministic, no LLM, and deliberately
   independent of the Claude call below so the safety-critical check
   never depends on API uptime.
2. A Claude risk-judgment call per position using fresh relevant news,
   for early-exit or stop-loss-adjustment judgment calls beyond a plain
   price cross — genuine reasoning, never downgraded to Ollama.

Both alert types are deduplicated via the `alerts_sent` table so the same
trigger doesn't spam repeatedly across successive monitor runs.
"""

# TODO: Phase 5 — monitor skill (code tripwire + agent risk judgment), light daily/twice-daily check

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic

from config.settings import Settings
from db.database import execute, fetch_all, fetch_one

MODEL = "claude-opus-4-8"

RISK_JUDGMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "should_alert": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["should_alert", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "You are a risk-monitoring analyst for an open swing-trading position. "
    "Given the position's entry price, current price, stop-loss, target, "
    "and recent relevant news, decide whether there is a genuine, material "
    "reason to alert the trader for an early exit or stop-loss-adjustment "
    "consideration — beyond a plain stop-loss/target price cross, which is "
    "handled separately. Only set should_alert to true for a real concern, "
    "not routine price movement."
)


def check_price_trigger(position: dict, current_price: float) -> str | None:
    """Deterministic, instant check: has price crossed the stored
    stop-loss or target? Returns "stop_loss", "target", or None. This is
    the code-tripwire half of the skill — no LLM involved.
    """
    if position["stop_loss"] is not None and current_price <= position["stop_loss"]:
        return "stop_loss"
    if position["target"] is not None and current_price >= position["target"]:
        return "target"
    return None


def evaluate_risk_judgment(position: dict, current_price: float, news: list[dict]) -> dict:
    """Ask Claude whether fresh news/context warrants an early-exit or
    stop-loss-adjustment alert for this position. Returns
    {"should_alert": bool, "reason": str}.
    """
    settings = Settings.load()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    relevant_headlines = [item["headline"] for item in news if item.get("relevant")]
    user_prompt = (
        f"Ticker: {position['ticker']}\n"
        f"Entry price: {position['entry_price']}\n"
        f"Current price: {current_price}\n"
        f"Stop-loss: {position['stop_loss']}\n"
        f"Target: {position['target']}\n"
        f"Relevant recent headlines: {json.dumps(relevant_headlines)}"
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": RISK_JUDGMENT_SCHEMA}},
        messages=[{"role": "user", "content": user_prompt}],
    )

    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined risk judgment for {position['ticker']}: {response.stop_details}")

    text = next(block.text for block in response.content if block.type == "text")
    return json.loads(text)


def already_alerted(position_id: int, alert_type: str) -> bool:
    """Dedup check: has this position already gotten this alert type since
    it was opened? Prevents repeat spam across successive monitor runs.
    """
    row = fetch_one(
        "SELECT id FROM alerts_sent WHERE position_id = ? AND alert_type = ?",
        (position_id, alert_type),
    )
    return row is not None


def record_alert(position_id: int, alert_type: str, message: str) -> int:
    """Log a sent alert so `already_alerted` can dedup future runs."""
    return execute(
        "INSERT INTO alerts_sent (position_id, alert_type, message) VALUES (?, ?, ?)",
        (position_id, alert_type, message),
    )


def monitor_position(position: dict, current_price: float, news: list[dict]) -> list[dict]:
    """Run both checks for a single active position. Returns every
    newly-fired (not already deduped-away) alert as a
    {"position_id", "ticker", "alert_type", "message"} dict.
    """
    alerts = []

    price_trigger = check_price_trigger(position, current_price)
    if price_trigger and not already_alerted(position["id"], price_trigger):
        message = (
            f"{position['ticker']}: price {current_price} crossed {price_trigger} "
            f"({position[price_trigger]})"
        )
        record_alert(position["id"], price_trigger, message)
        alerts.append(
            {"position_id": position["id"], "ticker": position["ticker"], "alert_type": price_trigger, "message": message}
        )

    try:
        judgment = evaluate_risk_judgment(position, current_price, news)
    except Exception as exc:  # Claude/network unavailable — the code tripwire above must not depend on this
        print(f"  risk judgment unavailable for {position['ticker']} ({exc}); price-trigger check above still ran")
        judgment = None

    if judgment and judgment.get("should_alert") and not already_alerted(position["id"], "risk_judgment"):
        message = f"{position['ticker']}: {judgment['reason']}"
        record_alert(position["id"], "risk_judgment", message)
        alerts.append(
            {"position_id": position["id"], "ticker": position["ticker"], "alert_type": "risk_judgment", "message": message}
        )

    return alerts


def monitor_all_positions() -> list[dict]:
    """Fetch every active position, run both checks against fresh price/
    news data, and return every newly-fired alert across the watchlist.
    """
    from skills.data_fetch_skill import fetch_news, fetch_ohlcv, filter_news_relevance

    all_alerts = []
    for row in fetch_all("SELECT * FROM positions WHERE status = 'active'"):
        position = dict(row)
        ohlcv = fetch_ohlcv(position["ticker"], timeframe="1d", period="5d")
        current_price = float(ohlcv["close"].iloc[-1])
        news = filter_news_relevance(position["ticker"], fetch_news(position["ticker"]))
        all_alerts.extend(monitor_position(position, current_price, news))

    return all_alerts


if __name__ == "__main__":
    from config.startup import StartupService
    from skills.notify_skill import route_message

    StartupService().start()

    alerts = monitor_all_positions()
    if not alerts:
        print("No active positions triggered an alert.")

    for alert in alerts:
        print(f"[{alert['alert_type']}] {alert['message']}")
        try:
            route_message("monitoring", alert["message"])
        except ValueError as exc:
            print(f"  (not sent to Telegram: {exc})")
