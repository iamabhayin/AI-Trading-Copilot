"""Data Fetch Skill (Data Agent) — Section 3.1.

Agent/Model: Ollama (news coarse relevance filter only).

Pulls OHLCV candles from the broker API (Zerodha Kite / Upstox / Fyers), or
Alpha Vantage / yfinance for testing. Pulls latest news headlines per ticker
(NewsAPI / Finnhub). Runs a coarse Ollama relevance filter on raw headlines
before they reach the Signal Skill — high volume, low stakes, cheap. Outputs
clean structured data; final news synthesis judgment happens in Claude via
the Signal Skill, not here.
"""

# TODO: Phase 1 — data fetch + indicator engine (plain Python, console output)
