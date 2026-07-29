# AI Trading Copilot — NIFTY Options Knowledge, Analysis & Position Advisory Rulebook

> **Repo location:** `docs/options-rulebook.md`
> **Status:** Source-of-truth trading logic for the Option Trading feature.
> **Implementation note:** This rulebook defines WHAT the rules are. The engineering decisions (pure-code implementation, no LLM in any loop, module layout, schema, cadence, config) are defined in the agent prompt (`option-trading-engine-prompt.md`) and override any implementation implications here. All Greeks, PCR, OI deltas, R:R, lot sizing, timestamps, and contract metadata are calculated/fetched deterministically in code — never inferred.

---

## 1. Role and Objective

The engine's job is NOT to constantly generate trades. Its job is to:

1. Pull and validate real-time market data.
2. Understand the current NIFTY market structure.
3. Analyze price action, support/resistance, option chain, OI, Change in OI, volume, IV, Greeks, PCR and other relevant data.
4. Classify the market as: **Bullish / Bearish / Range-bound / Uncertain-conflicting**.
5. Detect high-quality breakout, breakdown, reversal or continuation setups.
6. Decide whether there is actually a trade worth taking.
7. If there is a valid trade: select CE or PE, select appropriate expiry, select appropriate strike, define entry zone, define underlying invalidation, define option stop-loss, define targets, calculate Risk:Reward, calculate position size, explain the reasoning.
8. If no high-quality setup exists, explicitly output: **WAIT / NO TRADE**.
9. After the user manually enters a trade, continuously reassess the original thesis and provide: **HOLD / TRAIL STOP / PARTIAL PROFIT / EXIT / THESIS INVALIDATED**.

Never place an order automatically. The default mode is **ADVISORY ONLY**.

## 2. Most Important Principle

Never start analysis by asking "Which option should I buy?"

Always start with: "Is there a valid trade setup in the underlying?"

Sequence (never reversed):

```
MARKET → DIRECTION → STRUCTURE → CONFIRMATION → TRADE OR NO TRADE
→ EXPIRY → STRIKE → ENTRY → STOP LOSS → TARGET → POSITION SIZE
```

Cheap premium is NOT a reason to buy an option.

## 3. Data Required

Before generating any recommendation, obtain current and timestamped data.

**Underlying data:** NIFTY spot, open, high, low, previous close, current LTP, intraday candles, volume where applicable, VWAP if used, relevant moving averages if configured, ATR / volatility measure, recent swing highs/lows.

**Option chain (for relevant strikes):** expiry, strike, CE LTP, PE LTP, CE OI, PE OI, CE Change in OI, PE Change in OI, CE volume, PE volume, CE IV, PE IV, bid, ask, bid/ask quantity if available, Delta, Gamma, Theta, Vega.

**Additional context:** PCR, historical PCR if available, Max Pain if calculated, upcoming scheduled market events, India VIX / volatility context, current trading session/time, days/time remaining to expiry, available expiries, current contract lot size, transaction cost/slippage assumptions.

Never invent missing data. If critical data is stale or unavailable: **OUTPUT = WAIT / DATA INSUFFICIENT**.

## 4. Data Freshness Rule

Every analysis must know **DATA_TIMESTAMP**. Never compare current price with old OI / old option chain as if they were simultaneous.

For dynamic option-chain analysis, maintain historical snapshots (e.g. 09:30, 09:35, 09:40, …). This allows analysis such as `25200 CE OI: 70L → 55L → 40L` rather than only seeing `Current OI = 40L`. Dynamic change is more useful than a single static snapshot.

## 5. CE and PE Basics

**Call option (CE)** generally benefits from an upward move in the underlying, all else equal. Example: NIFTY 25,000 with expected bullish move to 25,300 → potential directional instrument is CE. But bullish expectation alone is NOT enough to issue BUY.

**Put option (PE)** generally benefits from a downward move, all else equal. Example: NIFTY 25,000 → expected 24,700 → potential instrument is PE. Again: direction must be confirmed before selecting the option.

## 6. ITM / ATM / OTM

Assume NIFTY = 25,000.

CE: 24700 CE = ITM, 24900 CE = ITM, 25000 CE = ATM, 25100 CE = OTM, 25300 CE = OTM.
Rule for CE: lower strike → ITM, near spot → ATM, higher strike → OTM.

PE: 24700 PE = OTM, 24900 PE = OTM, 25000 PE = ATM, 25100 PE = ITM, 25300 PE = ITM.
Rule for PE: lower strike → OTM, near spot → ATM, higher strike → ITM.

Always determine ATM dynamically from current spot and actually listed strikes.

## 7. Option Premium

```
OPTION PREMIUM = INTRINSIC VALUE + TIME VALUE
CE intrinsic value = max(0, Spot − Strike)
PE intrinsic value = max(0, Strike − Spot)
```

Example: NIFTY = 25,300; 25000 CE intrinsic = 300; if premium = ₹340, time value = ₹40.
Example: NIFTY = 24,700; 25000 PE intrinsic = ₹300.

## 8. Expiry Value

At expiry, time value → 0; option value becomes intrinsic value.

Example: buy 24700 CE with NIFTY at 25,000; premium paid ₹340 (intrinsic 300 + time 40). At expiry NIFTY = 25,150 → intrinsic = 450, time value = 0, expiry value = ₹450. Gross premium gain = 450 − 340 = ₹110 per unit. Actual monetary P&L = premium difference × actual lot size × number of lots, minus charges/slippage.

## 9. Theta — Time Decay

Theta measures option-price sensitivity to passage of time. **Theta hurts option buyers, all else equal.** Theta generally accelerates near expiry for options with meaningful time value.

Example: ATM CE ₹100 → ₹94 → ₹85 → lower as expiry approaches with spot/IV unchanged.

Therefore: if market is sideways, direction unclear, no breakout exists, and expiry is close → **DO NOT FORCE CE/PE BUYING.** Preferred output: WAIT / NO TRADE. Theta is working against directional buyers.

**Pre-entry hard floor (added 2026-07-28):** even when the expected holding period nominally fits before expiry (Section 34), a fresh directional buy is rejected outright once `days_to_expiry <= theta_danger_days` (config, default 3) — this is checked at trade-selection time, not only during post-entry position monitoring. Implementation: `check_theta_danger()` (originally position-monitor-only, `skills/options_position_monitor.py`), reused as a final-rejection-pass check in `skills/options_trade_selector.py::select_trade()` (`"theta_danger"` reason).

## 10. Implied Volatility — IV

IV represents volatility expectations embedded in option prices. Generally, IV ↑ → premiums increase; IV ↓ → premiums decrease, all else equal. High IV can make options expensive.

**IV Crush:** before an important event IV may rise; after uncertainty resolves IV can collapse. Example: CE bought at ₹250; NIFTY moves +80 points in the expected direction; IV collapses; premium ₹250 → ₹210. Direction was correct, trade still lost.

**Rule:** never judge an option trade using direction alone. Before buying, assess whether volatility is unusually expensive and whether a scheduled event can cause IV crush.

## 11. Delta

Delta measures approximate premium sensitivity to underlying movement. Example: delta 0.50, NIFTY +100 → approx +₹50 directional contribution, before Gamma/Theta/Vega effects.

Deep OTM CE → low delta; ATM CE → ~0.50; deep ITM CE → delta approaches 1. PE delta is negative.

For ordinary directional buying: **prefer liquid ATM or slightly ITM options** unless strategy logic explicitly requires another strike.

## 12. Gamma

Gamma measures how quickly delta changes when the underlying moves. Memory aid: delta = speed, gamma = acceleration. Gamma is highest around ATM and especially important near expiry.

Example: ATM CE delta 0.50 → 0.60 → 0.75 → 0.90 as a strong favorable move develops.

Near expiry: gamma can make premium highly responsive, but theta is simultaneously aggressive. Near-expiry ATM options carry **high gamma opportunity + high theta risk** → require stronger confirmation and tighter risk management.

## 13. Vega

Vega measures premium sensitivity to IV. Example: vega 5, IV 20% → 21% → approx +₹5, all else equal. If IV falls 3 points: 5 × −3 = −₹15.

Vega is generally higher in longer-dated options, higher near ATM, lower close to expiry. Always consider vega when IV is elevated or event risk exists.

## 14. Open Interest — OI

OI = number of currently outstanding contracts. One contract has one buyer + one seller, so one new buyer + one new seller creates OI +1 (not +2).

**Critical rule:** OI alone does NOT reveal whether traders are bullish or bearish. Never say "high CE OI = definitely call writing" or "high PE OI = definitely put writing" without additional evidence.

## 15. Change in OI

Total OI answers: "Where are existing positions concentrated?"
Change in OI answers: "Where is fresh net positioning changing?"

Example: 25000 CE OI = 1 Cr with ΔOI +50,000, vs 25100 CE OI = 40L with ΔOI +10L → 25000 has more total positioning; 25100 has significantly more fresh activity. Both metrics matter.

## 16. Option Premium + OI Interpretation

For the SPECIFIC option contract being analyzed:

| Premium | OI | Classification |
|---|---|---|
| ↑ | ↑ | Long Build-up |
| ↓ | ↑ | Short Build-up / likely option writing |
| ↑ | ↓ | Short Covering |
| ↓ | ↓ | Long Unwinding |

Do not confuse underlying price with option premium when applying this table.

## 17. Option Chain Analysis — Core Sequence

1. Identify Spot.
2. Identify ATM.
3. Identify significant CE OI concentrations.
4. Identify significant PE OI concentrations.
5. Analyze Change in OI.
6. Analyze option premium + OI together.
7. Map potential support/resistance.
8. Check whether these levels are strengthening, weakening or migrating.
9. Analyze PCR.
10. Check IV/Greeks.
11. Confirm everything using underlying price action and volume.

**Golden rule: OPTION CHAIN = HYPOTHESIS. PRICE ACTION = CONFIRMATION.**

## 18. Base Example (Reference Table — used in unit tests)

NIFTY = 25,000.

| Strike | CE OI | CE ΔOI | CE Prem | PE Prem | PE ΔOI | PE OI |
|---|---|---|---|---|---|---|
| 24700 | 8L | 0 | 330 | 25 | +2L | 15L |
| 24800 | 12L | −1L | 245 | 40 | +15L | 60L |
| 24900 | 20L | +2L | 170 | 70 | +5L | 35L |
| 25000 | 30L | +4L | 110 | 105 | +5L | 30L |
| 25100 | 40L | +8L | 65 | 165 | +2L | 18L |
| 25200 | 70L | +20L | 35 | 240 | −1L | 10L |
| 25300 | 25L | +3L | 18 | 325 | −2L | 5L |

PCR = 1.20. ATM = 25000.

Strongest PE OI zone: 24800 PE = 60L → **support candidate 24,800**.
Strongest CE OI zone: 25200 CE = 70L → **resistance candidate 25,200**.

Initial market map: 24,800 support ← 25,000 spot → 25,200 resistance. Initial probable range 24,800–25,200. This is a hypothesis, NOT a guaranteed range.

## 19. Support Detection

Potential support may be identified using: significant PE OI, fresh PE positioning, historical price support, price rejection/bounce, volume, option premium behavior, OI behavior as price approaches the level.

Example: 24800 PE OI = 60L, ΔOI = +15L → initial conclusion: 24,800 = POTENTIAL support. Do NOT say "put writers definitely defend 24,800" until premium/OI behavior supports that interpretation.

## 20. Resistance Detection

Potential resistance may be identified using: significant CE OI, fresh CE positioning, historical resistance, price rejection, volume, option premium behavior, OI behavior near breakout.

Example: 25200 CE OI = 70L, ΔOI = +20L → initial: 25,200 = POTENTIAL resistance. Not guaranteed.

## 21. Range-Bound Example (used in unit tests)

Support 24,800; resistance 25,200. NIFTY: 25000 → 25080 → 24950 → 25060 → 24980. No sustained breakout, volume weak/normal, OI walls remain.

Classification: **RANGE-BOUND**. For directional option buying: **NO TRADE / WAIT** (price inside range, no directional confirmation, theta hurts buyers). Watch: above 25,200 → bullish breakout candidate; below 24,800 → bearish breakdown candidate.

## 22. Bullish Breakout Example (used in unit tests)

Resistance 25,200. NIFTY: 25000 → 25100 → 25180 → 25220 → 25250. Do NOT immediately buy CE.

Check 25200 CE. OI before breakout 70L; after: 70L → 55L → 40L. CE premium: ₹35 → ₹55 → ₹75.

Interpretation: **Premium ↑ + OI ↓ = Short Covering** → call-side shorts closing → resistance may be weakening. If volume is also strong (e.g. 1.8× recent average) and NIFTY sustains above 25,200: bullish breakout confidence increases.

## 23. Bullish Retest Example

Price: 25,200 breakout → 25,250 → retest 25,205 → bounce 25,240. Old resistance 25,200 becomes potential new support: **RESISTANCE → SUPPORT FLIP**.

Strong bullish confirmation may include: price above resistance, sustained hold, successful retest, CE resistance-side OI unwinding, CE premium consistent with short covering, strong volume, higher-high/higher-low structure. Then: investigate CE buy. Do NOT automatically buy — pass the setup to the Trade Selection Engine.

## 24. False Bullish Breakout Example (used in unit tests)

Resistance 25,200. NIFTY: 25180 → 25210 → 25220 (looks like breakout).

But 25200 CE OI: 70L → 80L → 95L, and CE premium: ₹40 → ₹35 → ₹28.

Interpretation: **Premium ↓ + OI ↑ = Short Build-up / fresh call writing.** Then NIFTY: 25220 → 25180 → 25150 — price failed to sustain.

Classification: **FALSE BREAKOUT / REJECTION**. Action: AVOID CE, WAIT.

**Important: a false bullish breakout does NOT automatically mean buy PE.** PE requires its own bearish confirmation.

## 25. Bearish Breakdown Example (used in unit tests)

Support 24,800. NIFTY: 25000 → 24900 → 24810 → 24760.

Check 24800 PE. OI: 60L → 45L → 25L. PE premium: ₹40 → ₹65 → ₹90.

Interpretation: **PE premium ↑ + PE OI ↓ = PE short covering** → put-side shorts closing → support may be weakening. If NIFTY sustains below 24,800 with strong volume: bearish evidence strengthens.

## 26. Bearish Retest Example

24,800 breaks. Price: 24800 → 24750 → retest 24790 → rejected → 24740. Old support 24,800 becomes potential new resistance: **SUPPORT → RESISTANCE FLIP**.

If price sustains below, retest fails, support-side positions weaken, volume strong, market structure bearish → investigate PE buy.

## 27. False Breakdown Example (used in unit tests)

Support 24,800. Price: 24820 → 24790 → 24770 (looks bearish). Then: 24770 → 24820 → 24900 — support reclaimed strongly.

If supportive PE positioning remains/builds and price confirms: classification **FALSE BREAKDOWN / SUPPORT DEFENDED**. Action: do not chase PE, WAIT. Do not automatically buy CE either — CE needs independent bullish confirmation.

## 28. Support/Resistance Migration

Support and resistance are dynamic.

Example: morning PE OI 24800 = 60L, 25000 = 30L. Later: 24800 = 35L, 25000 = 70L → support positioning migrated 24800 → 25000. If price confirms: possible bullish structural shift.

Similarly: morning CE OI 25200 = 70L, 25400 = 20L. Later: 25200 = 30L, 25400 = 75L → resistance migrated 25200 → 25400.

The engine must update levels dynamically. **Never keep stale support/resistance all day.**

## 29. PCR — Put Call Ratio

PCR = Put OI / Call OI. Example: 900L / 600L = 1.5.

Never use "PCR > 1 = BUY CE" or "PCR < 1 = BUY PE". PCR is **contextual evidence only**. Analyze: current PCR, PCR trend, historical range/percentile, price movement, strike-level positioning.

Example of supporting evidence: PCR rising + price rising + support shifting upward + PE-side positioning supportive → strengthens bullish evidence. PCR alone cannot generate a trade.

## 30. Max Pain

Max Pain = the strike where aggregate intrinsic-value payout based on current OI is minimized. It is a **low-weight contextual indicator** and expiry-related reference only. Never assume NIFTY must move to Max Pain.

Evidence priority order:
1. Price action / trend
2. Support & resistance
3. OI + Change in OI
4. Volume
5. IV / Greeks
6. PCR
7. Max Pain

Max Pain must never override stronger evidence.

## 31. Market Regime Classification

Every analysis must first classify the regime:

**BULLISH** — evidence: higher highs, higher lows, resistance breakout, sustained hold, successful retest, resistance-side short covering/unwinding, resistance migrating upward, strong volume, support moving upward. Action: investigate CE.

**BEARISH** — evidence: lower highs, lower lows, support breakdown, sustained trade below, failed retest, support-side positioning weakening, resistance moving lower, strong selling pressure. Action: investigate PE.

**RANGE-BOUND** — evidence: price trapped between S/R, repeated breakout failures, OI walls intact, weak directional momentum, price near middle of range. Action: generally no directional CE/PE buy → WAIT.

**UNCERTAIN / CONFLICTING** — example: price breaks resistance but CE short build-up increases, breakout cannot sustain, volume weak, PCR deteriorates, price structure conflicts. Action: WAIT. **When evidence conflicts, do not force a trade.**

## 32. Breakout Confirmation Rule

Never define "price crossed level = confirmed breakout". Instead evaluate:

1. Price crossed resistance
2. Price sustained above
3. Volume confirms
4. Retest holds (if available)
5. OI structure supports weakening resistance
6. Option premium/OI confirms short covering rather than fresh writing
7. Market structure remains bullish
8. Risk:Reward remains attractive
9. The selected option's OWN premium chart also broke its recent range and held (if enough premium history exists) — see Section 32a

More independent confirmations = higher confidence.

## 32a. Premium-Chart Confirmation (added 2026-07-28)

Underlying breakout alone is not enough. The specific CE/PE contract under consideration must show its own premium clearing its own recent high (CE) or low (PE) and holding for the last few readings — not just moving in the expected direction.

Example: NIFTY clears resistance and sustains, but the 25000 CE premium was ₹170 → ₹176 → ₹181 → ₹185 against a recent range topping out at ₹180 — premium confirms. If instead premium stalled at ₹175 → ₹178 → ₹176 without clearing its own prior high, treat this as **wait**: the breakout may be weak, IV/seller pressure may be working against the buyer, or the wrong strike may be selected.

Rule: **Underlying gives direction. Option premium confirms execution.** Absence of enough premium history (e.g. right after a WATCH escalation) is never treated as a rejection — it only gates once there is enough data to judge.

Implementation: `check_premium_breakout()` in `skills/options_rules_engine.py`, wired into `evaluate_breakout_confirmation()`'s `premium_confirms` field; `OPTIONS_PREMIUM_CONFIRM_SUSTAIN_COUNT` config (default 2).

## 33. Breakdown Confirmation Rule

Evaluate: price crossed below support, sustained below, strong volume, failed retest (if available), support-side positioning weakens, premium/OI confirms relevant covering/unwinding, market structure bearish, R:R remains attractive, PE's own premium breakdown-confirms per Section 32a. Then consider PE.

## 34. Expiry Selection

Never hard-code expiry weekday. Always retrieve available contracts dynamically from current exchange/broker data.

Select expiry based on: expected holding period, days/time to expiry, theta, gamma, vega, liquidity, event risk, bid-ask spread.

- **Intraday:** nearest liquid expiry may be considered; near expiry gamma is high and theta aggressive → require strong confirmation.
- **1–2 day trade:** nearest suitable expiry if sufficient time remains; if extremely close to expiry and the thesis may take time, consider the next suitable expiry.
- **3–5+ day swing:** choose an expiry that gives the trade sufficient time to develop. Never select an option expiring tomorrow for a thesis expected to take four trading days.

**Core rule: EXPECTED HOLDING PERIOD MUST FIT COMFORTABLY INSIDE TIME TO EXPIRY.**

## 35. Strike Selection

For normal directional buying, default preference: **liquid ATM or slightly ITM**.

Example: NIFTY = 25,250, bullish setup → 25000 CE = ITM, 25200 CE = slightly ITM, ~25250 ≈ ATM (depending on listed strikes), 25300 CE = slightly OTM, 25700 CE = far OTM. Prefer ATM / slightly ITM, subject to liquidity, delta, spread, IV, cost, R:R.

Avoid far OTM merely because it is cheap. Far OTM problems: low delta, higher probability of expiring worthless, requires faster/larger move, theta risk. **CHEAP ≠ GOOD VALUE.**

## 36. Liquidity Filter

Before recommending an option, check: volume, OI, bid, ask, bid-ask spread, market depth if available.

Example: Option A bid ₹100 / ask ₹101 → good relative spread. Option B bid ₹80 / ask ₹94 → poor spread → reject or penalize.

Never recommend a contract that cannot realistically be entered/exited efficiently.

## 37. Entry Rule

Do not chase the first breakout candle automatically. Preferred logic:

```
BREAKOUT → SUSTAIN → RETEST (if available) → CONFIRMATION → ENTRY ZONE
```

Example: resistance 25,200; price 25200 → 25250 → 25210 retest → 25235. Possible entry zone: NIFTY 25,220–25,240, provided breakout structure remains valid, option-chain confirmation remains supportive, and R:R remains acceptable.

Use an **ENTRY ZONE** rather than pretending there is one perfect price.

## 38. Stop-Loss / Invalidation

Never create a random premium stop. Wrong: "bought at ₹140, SL ₹120 because ₹20 feels reasonable."

Correct: first identify **what market condition makes the original thesis wrong**. Example: bullish thesis = "25,200 resistance broke and should now act as support" → if NIFTY decisively loses the breakout/retest structure, the thesis is invalid.

Define the **UNDERLYING INVALIDATION LEVEL**, then map it to an **OPTION PREMIUM SL** using live delta, volatility and market structure. Always provide both when possible:

```
Underlying invalidation: NIFTY below X
Option SL: ₹Y
```

## 39. Target Selection

Targets come from: next support/resistance, swing highs/lows, OI concentrations, ATR, price structure, Risk:Reward.

Example: breakout 25,200, entry 25,230, next major resistance 25,400 → T1 = 25,320, T2 = 25,380–25,400. Never claim an exact target is guaranteed.

## 40. Risk:Reward

Every trade must calculate expected R:R before recommendation.

Example: entry 25,230, invalidation 25,170 → risk ≈ 60 points; target 25,380 → reward ≈ 150 points → structural R:R = 1:2.5.

Actual option R:R must use: entry premium, estimated/defined option SL, target premium or modeled payoff, slippage, charges where relevant.

Minimum R:R is configurable (default: prefer ≥ 1:2). If a setup is bullish but R:R = 1:0.7 → **OUTPUT: SKIP / NO TRADE**. A correct market view can still be a bad trade.

## 41. Position Sizing

Size from MAXIMUM ACCEPTABLE LOSS.

Example: capital ₹2,00,000, max risk/trade 1% → ₹2,000. Option entry ₹150, SL ₹125 → risk/unit ₹25 → risk/lot = ₹25 × current exchange-defined lot size. Allowed lots = floor(max risk / risk per lot).

If one lot already exceeds max allowed risk: **NO TRADE**. Never increase risk merely because confidence is high. Always fetch current lot size dynamically.

## 42. Hard Rejection Rules

Output NO TRADE if ANY of the following:

1. Data stale
2. Critical data missing
3. No clear setup
4. Market in middle of range
5. Breakout unconfirmed
6. Breakdown unconfirmed
7. Conflicting signals
8. Poor liquidity
9. Excessive bid-ask spread
10. Option selected only because cheap
11. Far OTM without strategic justification
12. Insufficient time to expiry
13. Abnormal IV / event risk not accounted for
14. Risk:Reward below threshold
15. Position exceeds risk limit
16. Price already too close to target
17. Entry requires chasing an extended move
18. Market structure changed before entry
19. Theta danger — days to expiry at or below `theta_danger_days`, even if the holding-period fit check (Section 34) technically passed (added 2026-07-28)
20. The selected contract's own premium chart has not confirmed the underlying breakout/breakdown (Section 32a) — soft-gates `evaluate_breakout_confirmation()`'s `confirmed` flag rather than appearing in this list's own reason strings, but functionally the same effect

**NO TRADE is a valid and valuable recommendation.**

## 43. Scoring Model

Score candidate trades out of 100:

| Component | Weight |
|---|---|
| Trend / price action | 20 |
| Breakout/breakdown confirmation | 20 |
| OI structure | 15 |
| Change in OI | 10 |
| Volume | 10 |
| Risk:Reward | 10 |
| IV / Greeks | 5 |
| PCR | 5 |
| Liquidity | 5 |

Interpretation: 80–100 high-confidence candidate; 70–79 moderate candidate; 60–69 watch only; below 60 NO TRADE.

**Hard rejection rules override score.** Example: score 88 but R:R 1:0.8 → final output NO TRADE, not BUY.

## 44. Complete Bullish Example (end-to-end)

Initial: NIFTY 25,000, support 24,800, resistance 25,200, PCR 1.20.

11:00 — NIFTY 25,180: no trade yet.
11:20 — NIFTY 25,225: resistance crossed. Check 25200 CE: OI 70L → 50L, premium ₹35 → ₹55 → **short covering**. Volume 1.8× average. Price 25225 → 25205 retest → 25240 — retest held.

Updated state: trend bullish; 25,200 breakout confirmed; CE short covering yes; retest successful; volume strong; IV acceptable; next resistance 25,400.

Trade Selection Engine then: choose suitable expiry → choose liquid ATM/slightly ITM CE → define entry → define invalidation → define targets → calculate R:R → calculate position size → apply hard rejection rules.

Advisory output shape:

```
ACTION: BUY CANDIDATE — CE
MARKET STATE: Bullish Breakout Confirmed
UNDERLYING: NIFTY
BREAKOUT LEVEL: 25,200
ENTRY CONDITION: NIFTY must continue holding breakout/retest structure
PREFERRED ENTRY ZONE: 25,220–25,240
OPTION: liquid ATM/slightly ITM CE from suitable expiry
WHY: resistance breakout; successful retest; CE short covering; strong volume; bullish structure; IV acceptable
INVALIDATION: sustained loss of breakout structure
NEXT RESISTANCE: 25,400
TARGETS: T1 = 25,320; T2 = 25,380–25,400
Only recommend if R:R and position size satisfy risk rules.
```

## 45. Complete Bearish Example (end-to-end)

Initial: NIFTY 25,000, support 24,800, resistance 25,200. Price: 25000 → 24900 → 24790 — support crossed. Do not immediately buy PE.

24800 PE: OI 60L → 40L, premium rises → PE short covering. Volume strong. Price 24790 → 24750 → 24795 retest → 24740 — retest failed.

Analysis: support breakdown confirmed; support-side positioning weakening; retest failed; volume strong; structure bearish.

Advisory: BUY CANDIDATE — PE; entry only while breakdown structure remains valid; liquid ATM/slightly ITM PE, suitable expiry; invalidation = NIFTY reclaims and sustains above breakdown/retest structure; next support 24,600. Calculate entry premium, SL, targets, R:R, position size before final recommendation.

## 46. Complete No-Trade Example (used in unit tests)

NIFTY 25,000; support 24,800; resistance 25,200. Price: 24980 → 25040 → 25010 → 24970 → 25030. Volume weak, no breakout, OI walls stable.

Output:

```
ACTION: NO TRADE
MARKET: Range-bound
SUPPORT: 24,800 | RESISTANCE: 25,200
REASON: price near middle of range; no directional confirmation; no confirmed break; directional buying has weak edge; theta erodes premium
WATCH: above 25,200 evaluate bullish breakout; below 24,800 evaluate bearish breakdown
```

## 47. Open Position Monitoring

After the user manually enters, e.g. "Bought NIFTY [EXPIRY] 25200 CE at ₹145", store:

Position status OPEN; underlying NIFTY; direction BULLISH; instrument 25200 CE; actual expiry; entry premium ₹145; entry spot 25,235; **original thesis** (25,200 breakout + retest + CE short covering + volume); **original invalidation** (defined level/structure); original T1/T2; quantity.

**Never forget the ORIGINAL THESIS.**

## 48. Position Update Engine

For every open-position update, evaluate:

1. Is the original thesis still valid?
2. Has support/resistance changed?
3. Has OI structure changed?
4. Is opposite-side positioning strengthening?
5. Has IV changed materially?
6. Is theta becoming dangerous?
7. Has delta/gamma changed exposure materially?
8. Is target approaching?
9. Has Risk:Reward deteriorated?
10. Is a scheduled event approaching?
11. Is price action confirming continuation or reversal?

Then output one of: **HOLD / HOLD + TRAIL STOP / PARTIAL PROFIT / EXIT — TARGET / EXIT — THESIS INVALIDATED / EXIT — RISK CONDITIONS CHANGED**.

## 49. Hold Example

Position 25200 CE at ₹145. Current NIFTY 25,300; 25,200 breakout level holding; OI resistance migrated toward 25,400; volume supportive; no bearish reversal.

Output: HOLD; thesis still valid; current support 25,200; next resistance 25,400; hold while breakout structure remains valid; consider trailing stop per updated swing structure.

## 50. Exit Example — Thesis Invalidated

Bought CE on the 25,200 breakout. Then NIFTY: 25240 → 25190 → 25160 — 25,200 lost decisively. 25200 CE OI rebuilding with premium ↓ + OI ↑ (renewed call short build-up).

Output: **EXIT / THESIS INVALIDATED** — breakout failed; NIFTY lost 25,200; resistance re-established; fresh bearish derivatives evidence. Do not hold merely hoping price returns.

## 51. Partial Profit / Trailing Example

Entry 25200 CE at ₹145; NIFTY reaches T1; momentum still bullish.

Output: **PARTIAL PROFIT / TRAIL STOP** — T1 reached; book configured portion if strategy allows; trail remaining position below updated structural support. Do not convert a winning trade into an uncontrolled losing trade.

## 52. Never Change Thesis to Justify a Loss

If the trade was entered because "25,200 breakout should hold" and price decisively loses 25,200 — do not invent "maybe 25,100 will hold." That is a NEW thesis. The original trade must be evaluated against the ORIGINAL thesis. If invalid: EXIT per the risk plan. A new setup may be evaluated separately.

## 53. Position Update Output Format

```
MARKET: NIFTY
CURRENT SPOT: [value]
POSITION: [contract]
ENTRY: [value]
CURRENT PREMIUM: [value]
P&L: [value / %]
ORIGINAL THESIS: [text]
THESIS STATUS: VALID / WEAKENING / INVALID
CURRENT MARKET REGIME: BULLISH / BEARISH / RANGE / UNCERTAIN
SUPPORT: [levels]
RESISTANCE: [levels]
OI UPDATE: [summary]
IV/GREEKS: [important changes]
ACTION: HOLD / TRAIL / PARTIAL / EXIT
WHY: 1. 2. 3.
INVALIDATION: [level]
TARGETS: T1 / T2
RISK WARNING: [if any]
DATA TIMESTAMP: [timestamp]
```

## 54. New Trade Advisory Output Format

```
MARKET: NIFTY
DATA TIMESTAMP: [time]
SPOT: [value]
MARKET REGIME: Bullish / Bearish / Range / Uncertain
SETUP: Breakout / Breakdown / Retest / Continuation / No Setup
SUPPORT: [levels]
RESISTANCE: [levels]
OPTION CHAIN: [key OI + change-OI observations]
PCR: [value + interpretation]
IV: [normal / high / low + implication]
CONFIRMATION: [what is confirmed]
ACTION: BUY CE CANDIDATE / BUY PE CANDIDATE / WAIT / NO TRADE
EXPIRY: [selected actual expiry]
WHY THIS EXPIRY: [reason]
STRIKE: [strike + CE/PE]
MONEYNESS: ATM / ITM / OTM
WHY THIS STRIKE: [delta / liquidity / spread / theta reasoning]
ENTRY ZONE: [value]
UNDERLYING INVALIDATION: [value]
OPTION STOP: [value]
TARGET 1: [value]
TARGET 2: [value]
RISK:REWARD: [value]
POSITION SIZE: [lots]
CONFIDENCE: [x/100]
REASONS: 1. 2. 3. 4.
RISKS: 1. 2.
WHAT WOULD CANCEL THIS TRADE: [conditions]
```

### 54a. Confirmation Checklist Format (added 2026-07-28)

The CONFIRMATION field above is rendered as a fixed 9-line checklist in the actual Discord/Telegram advisory (`build_trade_checklist()` in `skills/options_rules_engine.py`), replacing free-text reasoning with the trading framework's own exact wording — each line is `Label → [value] ✔/✘`:

```
Trend → Bullish/Bearish ✔
Resistance breakout / Support breakdown → ✔
Volume → Above average / Strong ✔
Call unwinding / Put unwinding → ✔
Put writing / Call writing → ✔
Premium resistance breakout / PE premium breakout → ✔
ATM/ITM strike / ATM/ITM PE → ✔
IV acceptable → ✔
Risk:Reward ≥ 1:2 → ✔
```

Every row down through "Premium resistance breakout"/"PE premium breakout" and "ATM/ITM strike"/"Risk:Reward" is backed by a hard gate already enforced by `evaluate_breakout_confirmation()`/`select_trade()` — a delivered BUY message cannot show those unmet. "Put writing"/"Call writing" (opposite-side OI at the level strike) and "IV acceptable" (Greeks cross-check) are informational only, never gating, and can legitimately render ✘ even on a delivered BUY candidate — this is intentional: the checklist reports real confirmation strength (the rulebook's "more independent confirmations = higher confidence"), not a rubber stamp.

## 55. Final Decision Flow

```
FETCH LIVE DATA
↓ VALIDATE DATA + TIMESTAMP
↓ IDENTIFY SPOT + ATM
↓ CLASSIFY TREND / MARKET REGIME
↓ MAP SUPPORT + RESISTANCE
↓ ANALYZE CE/PE OI
↓ ANALYZE CHANGE IN OI
↓ ANALYZE OPTION PREMIUM + OI
↓ CHECK SUPPORT/RESISTANCE MIGRATION
↓ CHECK PCR
↓ CHECK IV + GREEKS
↓ CHECK PRICE ACTION + VOLUME
↓ DETECT: RANGE / BREAKOUT / BREAKDOWN / FALSE BREAKOUT / FALSE BREAKDOWN
↓ WAIT FOR CONFIRMATION
↓ IF NO VALID SETUP → NO TRADE
↓ IF VALID SETUP → SELECT DIRECTION (bullish → CE; bearish → PE)
↓ SELECT EXPIRY BASED ON HOLDING PERIOD
↓ SELECT LIQUID ATM / SLIGHTLY ITM STRIKE
↓ DEFINE ENTRY ZONE
↓ DEFINE UNDERLYING INVALIDATION
↓ DEFINE OPTION STOP
↓ DEFINE TARGETS
↓ CALCULATE RISK:REWARD
↓ CALCULATE POSITION SIZE
↓ APPLY HARD REJECTION RULES
↓ FINAL OUTPUT: BUY CE CANDIDATE / BUY PE CANDIDATE / WAIT / NO TRADE
↓ AFTER MANUAL ENTRY → MONITOR ORIGINAL THESIS
↓ HOLD / TRAIL / PARTIAL PROFIT / EXIT
```

## 56. Non-Negotiable Rules

1. Never recommend a trade from one indicator.
2. Option chain creates a hypothesis; price action confirms it.
3. Highest CE OI is potential resistance, not guaranteed resistance.
4. Highest PE OI is potential support, not guaranteed support.
5. OI alone cannot distinguish buying from writing.
6. Use option premium + OI together.
7. Change in OI is required to understand fresh activity.
8. Support/resistance are dynamic.
9. Continuously detect OI migration.
10. Price crossing a level is NOT automatically a breakout.
11. Breakout requires confirmation.
12. Retest improves confidence but is not mandatory in every fast market.
13. False breakout does not automatically mean take the opposite trade.
14. Middle of a range is generally poor for directional option buying.
15. Never buy far OTM merely because it is cheap.
16. Prefer liquid ATM/slightly ITM options for normal directional buying.
17. Select expiry based on expected holding period, not cheapest premium.
18. Never hard-code expiry weekday or lot size — fetch current contract metadata.
19. Account for theta near expiry.
20. Account for gamma near expiry.
21. Account for IV/vega around events.
22. PCR is supporting evidence, not a signal.
23. Max Pain is low-weight context, not a target.
24. Stop-loss comes from thesis invalidation, not arbitrary premium points.
25. Every trade requires acceptable Risk:Reward.
26. Every trade requires controlled position sizing.
27. Hard risk rules override confidence score.
28. If signals conflict, WAIT.
29. If data is stale or incomplete, WAIT.
30. NO TRADE is a successful decision when no edge exists.
31. Never invent market data.
32. Always state the timestamp of analyzed data.
33. Never silently change the original trade thesis after entry.
34. When the original thesis is invalidated, recommend exit according to the defined risk plan.
35. Explain every recommendation using observable evidence.

## 57. Core Philosophy

The goal is NOT "predict every NIFTY move." The goal is:

```
WAIT FOR HIGH-QUALITY SETUP
+ CONFIRM WITH MULTIPLE INDEPENDENT SIGNALS
+ SELECT THE RIGHT OPTION
+ CONTROL RISK
+ EXIT WHEN THESIS FAILS
```

Prefer **fewer high-quality trades** over more low-quality signals. The most important question is always: **"Do we have an edge right now?"** Unclear → WAIT. No → NO TRADE. Yes → define exactly why, what, when, where invalid, where target, how much risk — before recommending.

## 58. Implementation Notes (deterministic safeguards)

- Greeks, PCR, OI deltas, R:R, lot sizing, timestamps, and contract metadata are supplied/computed by the data and analytics layers in code. Nothing in this rulebook is interpreted or computed by a language model — the entire pipeline is deterministic Python (see agent prompt).
- NIFTY contract specifics (current NSE convention: weekly expiries on Tuesday, monthly on last Tuesday, holiday-adjusted to the previous trading day; four weekly expirations listed excluding the monthly) can change — hence Rule 18: always discover contracts dynamically, never hard-code the weekday.
- Pipeline architecture: Market Data Engine → Deterministic Analytics Engine → Rule/Validation Engine → Trade Selection Engine → Position Monitor.
- Advisory output is machine-consumable structured JSON matching Sections 53/54, written to `options_advisories` before any notification.
