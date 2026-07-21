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
3. An always-on status digest (current price, % distance to stop-loss
   and target) for every active position, regardless of whether either
   check above fired — pure arithmetic, with Ollama used only for
   phrasing polish (never the numbers), same split as the other digest
   skills. Sent to Discord #monitoring only, not Telegram.

Both alert types are deduplicated via the `alerts_sent` table so the same
trigger doesn't spam repeatedly across successive monitor runs.
"""

# TODO: Phase 5 — monitor skill (code tripwire + agent risk judgment), light daily/twice-daily check

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic
import requests

from config.settings import Settings
from db.database import execute, fetch_all, fetch_one

MODEL = "claude-sonnet-5"

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


def build_position_status(position: dict, current_price: float) -> dict:
    """Compute current price and % distance to stop-loss/target for a
    single active position — pure arithmetic, no LLM. Distance is null
    where the corresponding level isn't set (a position can have neither,
    e.g. after the linkage fix drops a mismatched suggestion).
    """
    status = {
        "ticker": position["ticker"],
        "current_price": current_price,
        "entry_price": position["entry_price"],
        "stop_loss": position["stop_loss"],
        "target": position["target"],
        "distance_to_stop_loss_pct": None,
        "distance_to_target_pct": None,
    }
    if position["stop_loss"] is not None:
        status["distance_to_stop_loss_pct"] = (current_price - position["stop_loss"]) / current_price * 100
    if position["target"] is not None:
        status["distance_to_target_pct"] = (position["target"] - current_price) / current_price * 100
    return status


def monitor_all_positions() -> tuple[list[dict], list[dict]]:
    """Fetch every active position, run both checks against fresh price/
    news data, and return `(alerts, statuses)`: every newly-fired alert
    across the watchlist, and a plain price/distance-to-SL-target status
    for every active position regardless of whether it triggered an alert
    (powers the always-on status digest). One position's fetch failure
    (e.g. a delisted ticker) must not block the rest — same isolation
    standard as the Signal Skill's per-ticker loop.
    """
    from skills.data_fetch_skill import fetch_news, fetch_ohlcv, filter_news_relevance

    all_alerts = []
    all_statuses = []
    for row in fetch_all("SELECT * FROM positions WHERE status = 'active'"):
        position = dict(row)
        try:
            ohlcv = fetch_ohlcv(position["ticker"], timeframe="1d", period="5d")
            current_price = float(ohlcv["close"].iloc[-1])
            news = filter_news_relevance(position["ticker"], fetch_news(position["ticker"]))
        except Exception as exc:  # one bad/delisted ticker must not block the rest of the check
            print(f"  skipping {position['ticker']}: {exc}")
            continue
        all_alerts.extend(monitor_position(position, current_price, news))
        all_statuses.append(build_position_status(position, current_price))

    return all_alerts, all_statuses


def _template_status_digest(statuses: list[dict]) -> str:
    """Deterministic fallback formatting — no LLM involved."""
    if not statuses:
        return "*Position Status*\nNo active positions."

    lines = ["*Position Status*"]
    for s in statuses:
        line = f"{s['ticker']}: {s['current_price']:.2f}"
        if s["distance_to_stop_loss_pct"] is not None:
            line += f" | SL {s['stop_loss']:.2f} ({s['distance_to_stop_loss_pct']:+.1f}%)"
        if s["distance_to_target_pct"] is not None:
            line += f" | Target {s['target']:.2f} ({s['distance_to_target_pct']:+.1f}%)"
        lines.append(line)
    return "\n".join(lines)


def format_status_digest(statuses: list[dict]) -> str:
    """Turn per-position status data into a digest message. Uses Ollama
    for phrasing polish only — the numbers are plain arithmetic computed
    in `build_position_status`, never touched by an LLM. Falls back to a
    deterministic template if OLLAMA_HOST isn't configured or unreachable.
    """
    settings = Settings.load()
    base_message = _template_status_digest(statuses)
    if not settings.ollama_host:
        return base_message

    prompt = (
        "Rewrite the following position status digest as a short, clear "
        "Discord message. Keep every number exactly as given, keep the "
        "Markdown bold heading, and do not add any information that "
        "isn't already present.\n\n" + base_message
    )
    try:
        resp = requests.post(
            f"{settings.ollama_host}/api/generate",
            json={"model": settings.ollama_model, "prompt": prompt, "stream": False},
            timeout=30,
        )
        resp.raise_for_status()
        polished = resp.json().get("response", "").strip()
        return polished or base_message
    except requests.RequestException:
        return base_message  # fail open — better a plain message than none at all


def main() -> None:
    """CLI/cron entry point: check every active position and deliver any
    newly-fired alerts. This is what the scheduler invokes.
    """
    from config.startup import StartupService
    from skills.notify_skill import route_message

    # Cron's captured stdout defaults to the Windows console codepage
    # (cp1252), which can't encode characters Ollama's phrasing pass may
    # introduce (e.g. the Rupee sign, U+20B9) -- crashed print(digest)
    # below in production even though the actual alert/digest delivery
    # had already succeeded. UTF-8 covers everything cp1252 can and more.
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    StartupService().start()

    alerts, statuses = monitor_all_positions()
    if not alerts:
        print("No active positions triggered an alert.")

    for alert in alerts:
        print(f"[{alert['alert_type']}] {alert['message']}")
        try:
            # Deterministic plain text (embeds field names like "stop_loss")
            # is never meant to be parsed as Markdown — see
            # send_telegram_message's docstring for why that matters here.
            route_message("monitoring", alert["message"], telegram_parse_mode=None)
        except Exception as exc:  # one alert's delivery failure must not block the rest
            print(f"  (not sent: {exc})")

    if statuses:
        digest = format_status_digest(statuses)
        print(digest)
        try:
            # Status digest is Discord #monitoring only, not Telegram —
            # informational check-in, not an actionable trade alert, same
            # split as the metals/news digests.
            route_message("monitoring", digest, send_telegram=False)
        except Exception as exc:
            print(f"  (status digest not sent: {exc})")


if __name__ == "__main__":
    main()
