"""Metals Price Skill — Jaipur Gold (24K) & Silver digest.

Agent/Model: Ollama (phrasing only, if configured) — this carries no
trading judgment (pure fetch-and-format), so it never touches Claude, per
the Code-vs-Agent split in the architecture doc's Section 2/11.

Not part of the NSE signal pipeline — a small standalone skill that
scrapes Jaipur's 24K gold (per gram + per 10g, the common Indian retail
convention) and silver (per gram) rates off a public Indian bullion-rate
aggregator, and posts a short digest to Discord #market-news twice daily
(morning/evening, mirroring the existing news-digest cron pattern).
Telegram is intentionally skipped — this is a passive informational
digest, not an actionable trade alert.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from bs4 import BeautifulSoup

from config.settings import Settings
from skills.notify_skill import route_message

GOLD_URL_TEMPLATE = "https://www.goodreturns.in/gold-rates/{slug}.html"
SILVER_URL_TEMPLATE = "https://www.goodreturns.in/silver-rates/{slug}.html"


def _rate_table(html: str) -> "BeautifulSoup":
    """Return the page's rate table (Gram/24K/22K/18K for gold, or
    Gram/Today/Yesterday/Change for silver) — same markup class on both
    goodreturns.in gold and silver pages.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="gr-table")
    if table is None:
        raise ValueError("rate table not found in page markup")
    return table


def _row_cells(table, gram: str) -> list:
    for row in table.find("tbody").find_all("tr"):
        cells = row.find_all("td")
        if cells and cells[0].get_text(strip=True) == gram:
            return cells
    raise ValueError(f"no rate row found for {gram}g")


def _parse_amount(cell) -> float:
    text = cell.get_text(strip=True)
    match = re.search(r"[\d,]+(?:\.\d+)?", text)
    if not match:
        raise ValueError(f"could not parse a rupee amount from {text!r}")
    return float(match.group().replace(",", ""))


def fetch_metal_rates(city: str = "Jaipur") -> dict:
    """Scrape today's 24K gold (per gram + per 10g) and silver (per gram)
    rates for `city` off goodreturns.in's city-specific rate pages —
    these already publish real local retail rates (sourced from IBJA's
    daily reference rate), unlike a spot-price API that would need a
    guessed-at local premium on top.

    Raises on any fetch/parse failure; the caller (`build_metals_digest`)
    decides how to degrade — this feature has no trading stakes, so
    failing loudly here and catching it one layer up is simpler than
    threading fail-open defaults through every parsing step.
    """
    slug = city.lower()
    headers = {"User-Agent": "Mozilla/5.0"}

    gold_resp = requests.get(GOLD_URL_TEMPLATE.format(slug=slug), headers=headers, timeout=10)
    gold_resp.raise_for_status()
    gold_table = _rate_table(gold_resp.text)
    gold_per_gram = _parse_amount(_row_cells(gold_table, "1")[1])  # column 1 = 24K
    gold_per_10g = _parse_amount(_row_cells(gold_table, "10")[1])

    silver_resp = requests.get(SILVER_URL_TEMPLATE.format(slug=slug), headers=headers, timeout=10)
    silver_resp.raise_for_status()
    silver_table = _rate_table(silver_resp.text)
    silver_per_gram = _parse_amount(_row_cells(silver_table, "1")[1])  # column 1 = Today

    return {
        "city": city,
        "gold_per_gram": gold_per_gram,
        "gold_per_10g": gold_per_10g,
        "silver_per_gram": silver_per_gram,
    }


def _template_message(rates: dict | None, city: str) -> str:
    """Deterministic fallback formatting — no LLM involved."""
    if rates is None:
        return f"*{city} Metals Digest*\nData unavailable today."
    return (
        f"*{rates['city']} Metals Digest*\n"
        f"Gold (24K): ₹{rates['gold_per_gram']:,.0f}/g | ₹{rates['gold_per_10g']:,.0f}/10g\n"
        f"Silver: ₹{rates['silver_per_gram']:,.0f}/g"
    )


def format_metals_digest(rates: dict | None, city: str = "Jaipur") -> str:
    """Turn a rates dict (or `None` on fetch failure) into a Discord-ready
    message. Uses Ollama for light phrasing polish only — the numbers
    themselves are never touched by an LLM. Falls back to a deterministic
    template if OLLAMA_HOST isn't configured or unreachable.
    """
    settings = Settings.load()
    base_message = _template_message(rates, city)
    if not settings.ollama_host:
        return base_message

    prompt = (
        "Rewrite the following gold/silver rate digest as a short, clear "
        "Discord message. Keep every number exactly as given, keep the "
        "Markdown bold heading, and do not add any information that isn't "
        "already present.\n\n" + base_message
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


def build_metals_digest(city: str = "Jaipur") -> str:
    """Fetch + format the digest, degrading to a "data unavailable" note
    if the scrape fails for any reason (site down, markup changed, etc.)
    rather than skipping the post entirely.
    """
    try:
        rates = fetch_metal_rates(city)
    except Exception as exc:
        print(f"  metals rate fetch failed: {exc}")
        rates = None
    return format_metals_digest(rates, city)


def notify_metals_digest(city: str = "Jaipur") -> None:
    """Build and send the metals digest to Discord #market-news only —
    the synchronous entry point used by the morning/evening cron jobs.
    """
    route_message("metals_price", build_metals_digest(city), send_telegram=False)


def main(argv: list[str] | None = None) -> None:
    """CLI/cron entry point: fetch, format, and send today's metals digest."""
    from config.startup import StartupService

    StartupService().start()
    notify_metals_digest()
    print("Metals digest sent.")


if __name__ == "__main__":
    main()
