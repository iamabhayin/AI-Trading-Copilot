"""Signal Skill (Strategy Agent) — Section 3.3.

Agent/Model: Claude.

Takes indicator output + news (pre-filtered by Ollama in the Data Fetch
Skill) + optionally a chart image, and sends it to the Claude API for
synthesis. Forces structured JSON output:
`{ticker, action, entry, stop_loss, target, confidence, rationale}`.
Ranks/filters into a shortlist ("prominent stocks"). This is the
highest-stakes judgment in the system — never downgraded to Ollama, even
during cost-saving/validation phases.
"""

# TODO: Phase 2 — signal skill with Claude API call (console-only, sanity-check suggestions)

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic

from config.settings import Settings
from db.database import execute

MODEL = "claude-sonnet-5"

VALID_ACTIONS = {"BUY", "SELL", "HOLD"}

SYSTEM_PROMPT = (
    "You are a swing-trading signal analyst. Given technical indicators and "
    "recent relevant news headlines for a stock, decide whether to suggest "
    "BUY, SELL, or HOLD. For BUY/SELL, give a concrete entry price, "
    "stop-loss, and target based on the supplied indicators (RSI, the "
    "14-day/50-day EMA pair, Bollinger Bands, and volume_ratio_20). For "
    "HOLD, entry/stop_loss/target may be null. Give a confidence score "
    "between 0 and 1, and a short rationale grounded in the specific "
    "indicator values and headlines you were given. "
    "Actively check the indicator snapshot for these named setups, and if "
    "one is present, name it explicitly in the rationale — it may support "
    "or contradict the action, use judgment, don't force a BUY just "
    "because a pattern is present. Absence of a pattern doesn't rule out "
    "BUY/SELL on other grounds, but when one is present, always name it: "
    "(1) Pullback in an uptrend: close is above ema_50d (uptrend intact) "
    "but has dipped toward ema_14d or bb_lower, with rsi_14 cooled into "
    "roughly the 35-50 zone without breaking trend structure. Favors a BUY "
    "with stop-loss just below the ema_14d/support level and target near "
    "bb_upper or the prior swing high. "
    "(2) EMA golden/death cross: check whether ema_14d crossed ema_50d "
    "within the recent history window — crossing above is bullish, "
    "crossing below is bearish — signaling a potential trend shift, "
    "especially when rsi_14 confirms the same direction. "
    "(3) Bollinger Band squeeze/breakout: check whether bb_upper minus "
    "bb_lower has been narrowing across the recent history window before "
    "comparing to the current spread (a narrowing-then-widening spread is "
    "low volatility building toward a breakout), followed by close "
    "pushing through either band, ideally with rsi_14 confirming momentum "
    "in the breakout direction. "
    "volume_ratio_20 (current volume divided by its own 20-period average, "
    "so 1.0 is average, 2.0 is double the usual volume) is a confirming "
    "signal for all three setups above, not a standalone trigger: treat a "
    "setup as stronger when volume_ratio_20 is meaningfully above 1 (the "
    "move is backed by real participation) and treat it with more caution "
    "when volume is thin, well below 1 (the move may not hold). Don't "
    "block a BUY/SELL on volume alone if the rest of the evidence is solid. "
    "You are also given an 'Overnight Global Cues' snapshot — S&P 500, "
    "Nasdaq, and crude oil % change, the VIX level, and USD/INR % change "
    "from the prior US/FX session (a cue may be missing if its fetch "
    "failed; treat that as no signal, not a red flag). This is "
    "market-wide context, not a per-ticker signal — weigh it into your "
    "synthesis the same way you already weigh news headlines. Use "
    "judgment about whether the overnight backdrop supports or "
    "complicates the ticker-specific setup; never apply a fixed rule to "
    "it (e.g. do not treat 'VIX above some level' as an automatic "
    "confidence downgrade)."
)

SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "entry": {"type": ["number", "null"]},
        "stop_loss": {"type": ["number", "null"]},
        "target": {"type": ["number", "null"]},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["action", "entry", "stop_loss", "target", "confidence", "rationale"],
    "additionalProperties": False,
}

# Task 4 (gated behind Settings.enable_risk_regime_bias, default False —
# see config/settings.py): same contract plus a coarse regime
# classification, requested as part of the same synthesis call so
# enabling this adds no extra API call, only a bit of output on the runs
# it's active for. SUGGESTION_SCHEMA itself is untouched by design, so
# the default (flag off) path is byte-identical to before this task.
RISK_REGIME_FIELD = {"risk_regime": {"type": "string", "enum": ["risk_on", "risk_off", "neutral"]}}

SUGGESTION_SCHEMA_WITH_REGIME = {
    "type": "object",
    "properties": {**SUGGESTION_SCHEMA["properties"], **RISK_REGIME_FIELD},
    "required": [*SUGGESTION_SCHEMA["required"], "risk_regime"],
    "additionalProperties": False,
}

RISK_REGIME_PROMPT_ADDENDUM = (
    " Additionally, classify the overnight global cues you were given "
    "into a coarse risk_regime: 'risk_on' if the overnight backdrop "
    "broadly favors risk assets (equities up, VIX low/falling, no "
    "alarming move in crude or the rupee), 'risk_off' if it broadly "
    "discourages risk-taking (equities down, VIX elevated/rising, a "
    "sharp adverse move in crude or the rupee), or 'neutral' if the "
    "signals are mixed or unremarkable. This classification is separate "
    "from, and must not override, your BUY/SELL/HOLD action for this "
    "specific ticker — it only feeds into how suggestions get ranked "
    "against each other afterward, never the decision itself."
)

# Mild ranking nudge only, never a filter and never applied to the
# action Claude chose. Absent for any (regime, action) pair not listed
# here — including when risk_regime is None (flag off, or missing from
# an older/HOLD-only suggestion), so the default path is unaffected.
REGIME_RANKING_BOOST = {
    ("risk_on", "BUY"): 1.1,
    ("risk_off", "SELL"): 1.1,
}


def _build_user_prompt(
    ticker: str, indicators: dict, news: list[dict], recent: list[dict], global_cues: dict
) -> str:
    relevant_headlines = [item["headline"] for item in news if item.get("relevant")]
    return (
        f"Ticker: {ticker}\n"
        f"Indicators: {json.dumps(indicators, sort_keys=True)}\n"
        f"Recent history (oldest to newest): {json.dumps(recent)}\n"
        f"Overnight Global Cues: {json.dumps(global_cues, sort_keys=True)}\n"
        f"Relevant recent headlines: {json.dumps(relevant_headlines)}"
    )


def generate_suggestion(
    ticker: str,
    indicators: dict,
    news: list[dict],
    recent: list[dict],
    global_cues: dict,
    include_risk_regime: bool = False,
) -> dict:
    """Call Claude with indicator + news + overnight global-cues context
    and return a validated, structured suggestion: {ticker, action,
    entry, stop_loss, target, confidence, rationale}.

    `include_risk_regime=False` (the default) is byte-identical to this
    skill's behavior before Task 4 — same system prompt, same schema,
    same one JSON schema call. Only when both `Settings.enable_risk_regime_bias`
    and the `--regime-check` CLI flag are set (see `main`) does the
    caller pass `True`, which swaps in the regime-augmented prompt/schema
    for this same call — no extra API call either way.
    """
    settings = Settings.load()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    system_prompt = SYSTEM_PROMPT + RISK_REGIME_PROMPT_ADDENDUM if include_risk_regime else SYSTEM_PROMPT
    schema = SUGGESTION_SCHEMA_WITH_REGIME if include_risk_regime else SUGGESTION_SCHEMA

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system_prompt,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": _build_user_prompt(ticker, indicators, news, recent, global_cues)}],
    )

    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined to analyze {ticker}: {response.stop_details}")

    text = next(block.text for block in response.content if block.type == "text")
    suggestion = json.loads(text)
    suggestion["ticker"] = ticker
    return suggestion


def save_suggestion(suggestion: dict, timeframe: str) -> int:
    """Persist a suggestion to the `suggestions` table. Returns the new row id."""
    return execute(
        """
        INSERT INTO suggestions (ticker, action, entry_price, stop_loss, target, confidence, rationale, timeframe)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            suggestion["ticker"],
            suggestion["action"],
            suggestion.get("entry"),
            suggestion.get("stop_loss"),
            suggestion.get("target"),
            suggestion.get("confidence"),
            suggestion.get("rationale"),
            timeframe,
        ),
    )


def _ranking_confidence(suggestion: dict) -> float:
    """Confidence used for shortlist ranking only — never changes the
    action Claude chose, never filters a suggestion out. When a
    suggestion carries a `risk_regime` tag (Task 4, gated behind
    `Settings.enable_risk_regime_bias`), a mild multiplier nudges ranking
    order when the action aligns with the regime; when `risk_regime` is
    absent (the default — flag off, or a HOLD/older suggestion), this is
    identical to the raw confidence score.
    """
    confidence = suggestion.get("confidence") or 0
    multiplier = REGIME_RANKING_BOOST.get((suggestion.get("risk_regime"), suggestion.get("action")), 1.0)
    return confidence * multiplier


def shortlist(suggestions: list[dict], limit: int = 5) -> list[dict]:
    """Rank non-HOLD suggestions by confidence (adjusted for risk-regime
    alignment when present — see `_ranking_confidence`) and return the
    top `limit` — the "prominent stocks" shortlist referenced in
    Section 3.3.
    """
    actionable = [s for s in suggestions if s["action"] in ("BUY", "SELL")]
    return sorted(actionable, key=_ranking_confidence, reverse=True)[:limit]


def main(argv: list[str] | None = None) -> None:
    """CLI/cron entry point: the full pipeline for the whole watchlist —
    data fetch -> indicators -> signal generation -> save -> notify the
    shortlisted (non-HOLD) suggestions. This is what the scheduler invokes.
    """
    from config.startup import StartupService
    from skills.data_fetch_skill import fetch_ticker_snapshot
    from skills.global_cues_skill import fetch_global_cues
    from skills.indicator_engine import compute_indicators, summarize_latest, summarize_recent
    from skills.notify_skill import notify_suggestion

    args = argv if argv is not None else sys.argv[1:]
    settings = StartupService().start()
    watchlist = settings.watchlist or ["AAPL"]
    timeframe = settings.default_timeframe or "1h"

    # Same overnight snapshot for the whole watchlist, so fetch once per
    # run rather than per ticker. fetch_global_cues() never raises (each
    # of its 5 tickers is isolated internally) -- worst case this is {},
    # and the suggestion prompt just shows no overnight context.
    global_cues = fetch_global_cues()

    # Task 4: risk-regime classification. Both the config flag AND this
    # CLI flag must be set -- neither is flipped on by this code. The
    # --regime-check flag is how a specific cron job invocation (meant to
    # be pre_market_run only, once/day) opts in; the config flag is the
    # separate "do I want this feature at all" switch. Leaving either off
    # keeps every generate_suggestion() call byte-identical to before
    # Task 4 -- zero cost/behavior change until both are explicitly set.
    include_risk_regime = settings.enable_risk_regime_bias and "--regime-check" in args

    suggestions = []
    for ticker in watchlist:
        print(f"--- {ticker} ---")
        try:
            snapshot = fetch_ticker_snapshot(ticker, timeframe)
            computed = compute_indicators(snapshot["ohlcv"], timeframe)
            indicators = summarize_latest(computed)
            recent = summarize_recent(computed)
            suggestion = generate_suggestion(
                ticker, indicators, snapshot["news"], recent, global_cues, include_risk_regime
            )
        except Exception as exc:  # one bad/delisted ticker must not block the rest of the watchlist
            print(f"  skipping {ticker}: {exc}")
            continue
        print(json.dumps(suggestion, indent=2))
        save_suggestion(suggestion, timeframe)
        suggestions.append(suggestion)

    print("\n--- Shortlist ---")
    for suggestion in shortlist(suggestions):
        print(f"{suggestion['ticker']}: {suggestion['action']} (confidence={suggestion['confidence']})")
        try:
            notify_suggestion(suggestion)
        except Exception as exc:  # one suggestion's delivery failure must not block the rest
            print(f"  (not sent: {exc})")


if __name__ == "__main__":
    main()
