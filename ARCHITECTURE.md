# Architecture: `nseswing`

`nseswing` is a decision-support system for one swing-trading method on NSE cash
equities: buy pullbacks in strong stocks that are consolidating after an uptrend.
It screens for candidates, maps support zones, watches for the intraday entry
trigger, sizes the position from a fixed account-risk budget and manages exits in
R-multiples. It supports your chart review; it does not replace it. It is not
financial advice.

All prices are in INR (₹), all times are IST (`Asia/Kolkata`), and every lookback
is counted in NSE trading days. Each daily bar is one NSE session, so "21 days"
means 21 bars. Only the cash segment is used, with no F&O instruments.

## Setup this build targets

| Item | Choice | Where to change it |
|---|---|---|
| Universe | Nifty 500 constituents. BE/BZ series stay in and are flagged as trade-for-trade. | `universe.*` |
| Market data | Upstox API v2/v3: daily and 1/5/15-min candles, full quotes with circuit limits, LTP | `data.provider`, `upstox.*` |
| Broker | Upstox. Orders go out only in live mode, which is locked by default. | `execution.*` |
| Stack | Python 3.11+, pandas, Plotly, Streamlit, pytest | `pyproject.toml` |
| Account | ₹10,00,000 at 0.5% risk per trade, CNC delivery (Upstox product `D`) | `account.*` |
| Execution mode | `paper` (simulated fills). `alerts` and `live` are also available. | `execution.mode` |

## Module map

| Module | Responsibility | Method rules |
|---|---|---|
| `config.py` | Loads `config/settings.yaml` into typed, validated models. There are **no defaults in code**: an unknown or missing key stops startup. | All |
| `core/timeutil.py` | IST clock, time parsing, tz-aware timestamps | Sessions |
| `core/calendar.py` | NSE holidays, special sessions (Budget day, Muhurat) and per-date session times, including the Closing Auction Session profile for F&O stocks; trading-day arithmetic | Calendar, sessions |
| `core/ticks.py` | Price-slab tick table and `Decimal` rounding (entry/trigger up, stop/target down) | Tick size |
| `core/money.py` | ₹ formatting with Indian digit grouping (`₹10,00,000`) and lakh/crore short forms | Display |
| `data/base.py` | `DataProvider` protocol, OHLCV schema checks, `Quote` model | Data |
| `data/upstox.py` | Upstox REST client: OAuth login, instrument master, v3 historical and intraday candles, full quotes (circuit limits), LTP, rate limiting | Data, circuits |
| `data/nse.py` | Parsers and downloaders for NSE public files: Nifty 500 and thematic index lists, `sec_list.csv` price bands, ASM/GSM lists, corporate actions, holiday master, pre-open data | Universe, bands, surveillance, corporate actions, pre-open |
| `data/reference.py` | Builds one `SecurityInfo` per symbol: industry, themes, band, F&O flag, series, ASM/GSM/T2T flags, tick | Themes, flags |
| `data/csvstore.py` | Local CSV cache and an offline `CsvProvider` for replay and tests | Data |
| `corpactions.py` | Parses NSE action text into split, bonus, consolidation, rights and dividend actions, computes price/volume factors, and adjusts OHLCV. It **only applies a factor when the raw series still shows the jump**, so data the provider already adjusted is never adjusted twice. It also warns on unexplained split-sized gaps. | Corporate actions |
| `indicators.py` | EMA, Wilder ATR, slope in %/day, fractal swing points, unfilled gaps, anchored VWAP, weekly resampling | Stage 1–2 |
| `screener.py` | **Stage 1**: returns and ranks, momentum tiers, liquidity, hot themes, weekly advance-then-pullback check, daily checks 7a/7b/7c, the disqualifier, and a ranked watchlist with a reason for every check | Stage 1 |
| `levels.py` | **Stage 2**: support levels (EMAs, swings, gaps, AVWAP with default and manual anchors), AVWAP reclaim/pullback state, confluence zones with priority, first-retest flag, broken-zone memory | Stage 2.1–2.4, 2.8 |
| `preopen.py` | Expected open and gap from pre-open data; list of stocks whose zones sit near the expected open | Stage 2.5 |
| `intraday/bars.py` | Builds 1/5/15-min bars aligned to the 09:15 open from ticks or 1-min candles | Stage 2.5 |
| `intraday/trigger.py` | Trigger state machine per zone: WAITING → ARMED (dip into zone) → TRIGGERED (trades above previous bar's high) or BROKEN (falls through with no rebound). Also runs the validity checks. | Stage 2.6–2.8 |
| `risk.py` | **Stage 3**: stop method (day low, trigger-bar low or prior-bar low), tick rounding *before* sizing, `Decimal` share count, warnings, and the trade plan | Stage 3 |
| `costs.py` | Charges per leg (brokerage, STT, exchange, SEBI, IPFT, stamp, GST, DP), gross and net P&L and R, and a solver for net-of-cost R targets | Costs, Stage 4.1 |
| `orders/base.py` | `OrderManager`: **refuses any entry without a stop**, places the stop as soon as the entry fills, converts it to GTT or flags a morning re-place for overnight holds, and rate-limits orders | Stage 3.5, overnight stops |
| `orders/paper.py` | Paper broker: slippage, gap-through-stop fills, no fills while locked at lower circuit, DAY expiry (15:30, or 15:15 for CAS stocks), GTT triggering as a limit order (Upstox behaviour) | Paper mode |
| `orders/upstox_live.py` | Upstox order and GTT payload builders and calls, behind the live lock (mode, explicit confirmation, static-IP declaration, OPS limiter, optional algo header) | Live mode |
| `trademanager.py` | **Stage 4**: 3R/5R partials (portion is a setting), 9-EMA trailing exit, extension alert, upper-circuit flag, hold-full override with an eligibility hint, end-of-day overnight-stop check | Stage 4 |
| `session.py` | `SessionEngine`: one trading day for a set of watchlist names. It wires triggers, risk, orders, trade manager, alerts and journal together. **Live monitoring and replay both drive this same engine.** | Stages 2–4 |
| `replay.py` | Steps a historical day bar by bar in IST through the `SessionEngine` and logs every rule as it fires; can carry on with later days to manage exits | Reference scenario |
| `monitor.py` | Live session loop: pre-open snapshot, LTP polling, 1-min candle polling, bar building, and the `SessionEngine` | Live/paper monitoring |
| `journal.py` | SQLite journal of trades, fills with a charges breakdown, and events; R gross and net; CSV export | Journal |
| `alerts.py` | Alert bus that writes to the console, a JSONL file and optionally Telegram | Alerts |
| `plancard.py` | Trade-plan card as text for the CLI and Markdown/HTML for Streamlit | Plan card |
| `charts.py` | Annotated Plotly charts (weekly, daily, intraday): EMAs, AVWAP, swings, gaps, zones, circuit limits, entry, stop, 3R/5R, trigger markers | Chart review |
| `pipeline.py` | Pre-market orchestration: load, adjust, screen, zones, pre-open | — |
| `synthetic.py` | Deterministic synthetic data for tests and the demo (a universe plus the reference scenario). It never stands in for real data. | Tests |
| `cli.py` | `nseswing` command line | — |
| `app/streamlit_app.py` | Dashboard: watchlist, setup charts and plan cards, pre-open list, replay, positions, journal, config view | UI |

## Data flow

```
 NSE public files ──► data/nse.py ──┐
 Upstox instruments ► data/upstox.py ┴► data/reference.py ─► SecurityInfo per symbol
                                                              (industry, themes, band, F&O, series,
                                                               ASM/GSM/T2T, tick)
 Upstox daily candles ─► data/csvstore.py (cache) ─► corpactions.adjust ─► adjusted daily OHLCV
                                                                              │
                                     screener.py  (Stage 1) ◄─────────────────┘
                                         │ ranked watchlist: pass/fail + reason per check + flags
                                         ▼
                                     levels.py    (Stage 2) ─► zones: priority, first retest, broken memory
                                         │
 09:00–09:15 pre-open quotes ─► preopen.py ─► near-zone list + draft plan cards (risk.py, costs.py)
                                         │
 09:15 → close: LTP + 1-min candles ─► intraday/bars.py ─► session.SessionEngine
                                                            ├─ intraday/trigger.py
                                                            ├─ risk.py + costs.py
                                                            ├─ orders/ (paper | upstox_live)
                                                            ├─ trademanager.py
                                                            ├─ alerts.py
                                                            └─ journal.py
 replay.py feeds historical bars into the same SessionEngine, bar by bar.
 charts.py, plancard.py and app/streamlit_app.py render everything for review.
```

## Daily timeline (IST)

| Time | What runs |
|---|---|
| Before 09:00 | Refresh reference files, update daily candles, adjust for corporate actions, run Stage 1, Stage 2 zones and charts |
| 09:00–09:15 | Pre-open. Read the expected open (indicative equilibrium price), compute the gap, list stocks whose zones are near the open, draft plan cards |
| 09:15 | Continuous trading. Watch approaches to zones, and once price is close, monitor the chosen intraday timeframe (1, 5 or 15 min) |
| On trigger | Validate it, compute stop and size, then (paper/live) send the buy. Once it fills, place the stop right away. |
| 15:15 | **CAS stocks (F&O-listed)**: continuous trading ends and the exchange cancels pending SL orders. Trigger monitoring stops for these names. |
| 15:30 | Other stocks: DAY orders expire |
| After close | EOD manager: 9-EMA trailing exits, extension alerts, partial-target status, overnight-stop check (GTT active, or flag the SL for re-placing next morning), journal |

## Design decisions

1. **One config file.** Every parameter, assumption, cost rate and the tick table
   live in `config/settings.yaml`, each with a comment. The pydantic models have no
   defaults and forbid extra keys, so the YAML is the only source of values. User
   *data* lives in separate files under `config/`: custom theme lists, manual AVWAP
   anchors and the NSE holiday calendar.
2. **Exact money maths.** Tick rounding, sizing, targets and charges use `Decimal`.
   Indicators use floats.
3. **One engine.** Live monitoring, paper trading and replay share `SessionEngine`,
   so the replay tests exercise the same code that runs live.
4. **Safety first.**
   - Live orders need `execution.mode: live`, `execution.live.enabled: true` and
     `execution.live.confirm_live_trading: true`.
   - `OrderManager` raises if an entry has no valid stop.
   - The stop is sent on the fill event. If the stop is rejected, the system raises
     a critical alert and, depending on config, flattens the position.
5. **Exchange rules as data.** The tick table, holidays, special sessions, CAS
   profile, price bands and surveillance lists come from config or reference files
   rather than code, so they can be updated when NSE changes them.
6. **Interpretations are explicit.** Every qualitative rule ("narrowing",
   "flattening", "at the zone") is turned into a named setting. They are listed in
   the README and in the config comments. Anything that is not in the method is
   marked `OPTIONAL` and defaults to off.

## Exchange and broker facts this build relies on (verify periodically)

| Fact | Used by | Config |
|---|---|---|
| Closing Auction Session from 3 Aug 2026 for stocks with F&O contracts. Continuous trading ends 15:15, the auction runs about 15:15–15:35, and pending SL orders are cancelled at the transition. Other stocks keep 09:15–15:30. | calendar, paper broker, overnight stops | `sessions.cas.*` |
| Pre-open revised 7 Sep 2026. The window is still 09:00–09:15. Market orders are accepted until about 09:05, and order entry closes at a random moment around 09:08–09:10. | pre-open | `sessions.preopen_*` |
| Tick slabs (April 2025 revision): <₹250 → ₹0.01; ₹250–1,000 → ₹0.05; ₹1,000–5,000 → ₹0.10; ₹5,000–10,000 → ₹0.50; ₹10,000–20,000 → ₹1.00; >₹20,000 → ₹5.00 | ticks | `ticks.table` |
| A triggered Upstox GTT is placed as a LIMIT order. Each GTT needs exactly one ENTRY rule. TARGET and STOPLOSS legs take the `IMMEDIATE` trigger type. | live gateway, paper GTT model | `execution.*` |
| Upstox staff say historical candles are adjusted for corporate actions | corporate actions (auto-verify) | `corporate_actions.mode` |
| SEBI retail-algo framework, in force from 1 Apr 2026: static IP, a 10 orders/sec registration threshold, a daily login, broker-side algo tagging | live lock | `execution.live.*` |
| Charges: STT 0.1% on each side for delivery, NSE transaction charge ₹297/crore, IPFT ₹10/crore, SEBI fee ₹10/crore, stamp duty 0.015% on the buy side, GST 18%, Upstox ₹20 per executed order, DP charge about ₹20 + GST per scrip per sell day | costs | `costs.*` |

## Testing

- `tests/test_acceptance.py` checks the six acceptance cases exactly.
- Every module has unit tests built on deterministic synthetic data.
- Upstox and NSE parsers are tested against **payload fixtures written from the
  published formats**. They were not recorded from live endpoints, because those
  hosts are blocked from the build sandbox. Check them against real responses
  before you rely on them.
- An end-to-end replay test runs the reference scenario: a gap-up open, a pullback
  into an AVWAP and EMA zone, a 1-min trigger above the previous bar's high, the
  stop placed on fill, a 3R partial and the overnight GTT. A second scenario has
  price falling through the zone, which should give no trade and mark the zone
  broken.
- A documentation test fails if any key in `settings.yaml` is missing from the
  README.
