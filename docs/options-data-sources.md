# Option Trading — Data Source

> Scope note: this file documents only the data-source decision for the
> Option Trading feature (Phase 15). A full README section covering the
> whole feature is deferred to that phase's final docs+PR task, once the
> rules engine / trade selector / notifications are built — see the
> phase's task breakdown.

## Angel One SmartAPI

`skills/angel_client.py`. Chosen because SmartAPI supports officially
automatable, unattended login (client code + PIN + TOTP via `pyotp`) —
no daily browser OAuth. This matters because the engine runs on cron
with no human in the loop each morning.

Required environment variables (names only — see `.env.example` for
placeholders, real values go in `.env`, never committed):

- `ANGEL_API_KEY`
- `ANGEL_CLIENT_CODE`
- `ANGEL_PIN`
- `ANGEL_TOTP_SECRET`

**Static-IP whitelist caveat**: Angel One whitelists a static IP per
registered app. If auth previously worked and starts failing with a
forbidden/permission error, the most likely cause is a rotated home/ISP
IP, not a credentials problem — `angel_client.py` includes the current
public IP (via `ifconfig.me`) in the raised error message for exactly
this diagnosis.

**Known SmartAPI bug this module designs around**: the Option Greeks
endpoint has been reported to occasionally return monthly-expiry Greeks
when a weekly expiry is requested. `angel_client.assert_greeks_sanity()`
is a mandatory runtime check for this — it compares ATM theta/IV between
the requested weekly and monthly expiries and raises `GreeksSanityError`
if they're suspiciously identical, refusing to let that data reach the
rules engine.

## yfinance — supplementary, not a broker connection

`skills/options_data_fetch.py`. Angel One's `getMarketData`/`optionGreek`
don't cover everything the analytics layer needs, so yfinance fills the
gaps: NIFTY intraday candles for price-structure/swing-high-low analysis,
a backup spot value to cross-check Angel One's fetched spot against
(`check_spot_divergence`), and India VIX. No credentials required.
