# nseswing: pullback swing-trading decision support for NSE

`nseswing` implements one swing-trading method for NSE cash equities: buy pullbacks in
strong stocks that are consolidating after an uptrend. It does five things:

- screens for candidates (Stage 1)
- marks support zones (Stage 2)
- watches for the intraday entry trigger
- sizes each position from a fixed account-risk budget (Stage 3)
- manages exits in R-multiples (Stage 4)

All prices are in ₹, all times are IST, and every lookback is counted in NSE trading days.
There are no F&O instruments and no non-Indian markets.

> **Decision support, not financial advice.** The method relies on chart review: every
> setup comes with annotated charts and a plan card for your judgment. No live order is sent
> unless you switch on every live setting (see [Safety](#safety)). Paper trading is the default.

| Your setup | Built for |
|---|---|
| Universe | Nifty 500 constituents (EQ series; BE/BZ kept and flagged as trade-for-trade) |
| Data and broker | Upstox API (v2/v3); NSE public files for reference data |
| Stack | Python 3.11+, pandas, Plotly, Streamlit, pytest |
| Account | ₹10,00,000, 0.5% risk per trade, CNC delivery |
| Execution | Paper trading (alerts-only and live are available) |

Read [ARCHITECTURE.md](ARCHITECTURE.md) for the module list and data flow.

---

## Quick start (offline demo)

```bash
pip install -e ".[ui,dev]"
```

```bash
nseswing demo
```

```bash
streamlit run app/streamlit_app.py
```

`nseswing demo` builds a **synthetic** stock with fictional `.SYN` symbols and replays the
reference scenario bar by bar. That scenario is: the stock gaps up, pulls back into an EMA + AVWAP
zone, a 1-min bar breaks above the prior bar's high, and profit is taken into the rise.
The demo prints every rule as it fires, in IST:

```
09:10 PREOPEN_WATCH   expected open ₹485.76 (+1.20% gap) is 1.91% above zone MEDIUM ₹471.70–₹476.50: EMA9 + EMA21 + AVWAP
09:28 ZONE_APPROACH   within 1.0% of zone top ₹476.50: monitoring 1-min chart
09:41 DIP_INTO_ZONE   dipped into zone … (low ₹472.90)
09:42 TRIGGER         traded ₹477.05 > previous bar high ₹476.98 after dipping to ₹472.90
09:42 PLAN            entry ≈₹477.05, stop ₹472.90 (day low), 1204 shares, ₹ risk 4996.60, 57.44% of account …
09:42 ENTRY_FILLED / STOP_PLACED (SL-M placed right after the fill)
13:25 TARGET_PARTIAL  sold 397 @ ₹489.70 (+3.00R gross) [same-day: taxed as intraday]
15:30 OVERNIGHT_GTT   GTT stop SELL 807 below ₹472.90 is active
…     TRAIL_EXIT      daily close below the 9 EMA: sell the rest at the next open
```

It also writes an HTML report to `reports/charts/` with the intraday, daily and weekly
charts, the plan card, the levels table and the full rule log. The demo data is
**fictional**: it shows the mechanics and says nothing about how the method performs.

## Daily workflow with Upstox

### One-time setup

1. Create an app in the Upstox developer console and note its API key, secret and redirect
   URL. Export them in your shell or a `.env` loader:
   ```bash
   export UPSTOX_API_KEY=... UPSTOX_API_SECRET=... UPSTOX_REDIRECT_URI=https://127.0.0.1/callback
   ```
2. Download the reference files: the Nifty 500 list, price bands, F&O list, ASM/GSM,
   corporate actions, the Upstox instrument master, and a holiday cross-check:
   ```bash
   nseswing refresh-reference
   ```
3. Keep your own theme lists in `config/themes.yaml` and any manual AVWAP anchors in
   `config/anchors.yaml`.

### Each trading day (IST)

| When | Command | What you get |
|---|---|---|
| before 09:00 | `nseswing upstox-login` (then `--code …`) | today's token (Upstox tokens expire daily) |
| before 09:00 | `nseswing screen` | ranked Stage 1 watchlist with pass/fail and the reason for every check, plus circuit and surveillance flags (CSV in `reports/`) |
| before 09:00 | `nseswing zones` | Stage 2 zones per watchlist stock |
| 09:00–09:15 | `nseswing preopen` | stocks whose zones sit near the expected open, with a draft plan card for each |
| 09:10 → close | `nseswing monitor` | live LTP monitoring, triggers, paper entries with the stop on fill, partials, overnight GTT |
| after close | `nseswing journal` | trades with charges and R gross / net |
| any time | `streamlit run app/streamlit_app.py` | dashboard: watchlist, charts, pre-open, replay, journal |

Other commands:

- `nseswing plan SYMBOL --entry 500 --stop 487.50` prints a plan card for any entry and stop.
- `nseswing hold-full SYMBOL on` is the Stage 4.5 override (hold the full position, skip partials).

### Replay mode (you supply the stock and date)

```bash
nseswing replay SYMBOL 2026-10-14 --interval 1 --days-after 10
```

This loads the daily history up to the prior session (adjusted for corporate actions) and
runs Stage 1 and Stage 2 as they stood before the open. It then feeds every 1-minute bar
through the same session engine the live monitor uses, and keeps managing the position
on later sessions. Replay always uses the paper broker. In the dashboard's Replay tab you
can step through the session bar by bar and watch the rules fire.

With `data.provider: csv` you can replay from your own exported files. The layout is
described in `src/nseswing/data/csvstore.py`.

---

## How each rule is implemented

### Stage 1: screen (`src/nseswing/screener.py`)

| Rule | Code | Settings |
|---|---|---|
| 1. 1/3/6-month returns, ranked per window | `Screener.run` | `screener.return_windows` |
| 2. ≥ 30% **or** top 30% in at least one window; "both" listed first | `momentum_check`, `rank_order` | `screener.momentum.*` |
| 3. Liquidity: 50-day turnover, volume, price | `liquidity_check` | `screener.liquidity.*`, `data.turnover_price` |
| 4. Watchlist | `ScreenRow.on_watchlist`, `ScreenResult.watchlist` | |
| 5. Hot themes (industry, thematic index, custom): ≥ 3 passers; fastest first | `Screener.run`, `ReferenceData.groups`, `_sort_key` | `screener.themes.*`, `config/themes.yaml` |
| 6. Weekly: prior advance then a pullback | `weekly_check` (`indicators.resample_weekly`) | `screener.weekly.*` |
| 7a. Higher pullback lows | `higher_lows`, `higher_lows_check` (`indicators.swing_points`) | `screener.daily.swing_lookaround` |
| 7b. Contracting range | `contraction_check` (ATR-ratio or swing-amplitude) | `screener.daily.contraction.*` |
| 7c. Reclaimed the 9/21 EMA, EMA flat or rising | `ema_reclaim_check` | `screener.daily.ema_reclaim.*` |
| 8. Disqualify falling highs + lows below declining averages | `disqualify_check` | `screener.disqualify.*` |
| Output: ranked list, reasons, flags | `ScreenResult.table`, `nseswing screen`, dashboard | |

### Stage 2: zones and trigger

| Rule | Code | Settings |
|---|---|---|
| 1. EMAs 9/21/50, swing highs/lows, unfilled gaps, AVWAP | `levels.build_level_map` (`indicators.*`) | `levels.*` |
| 2. AVWAP from the consolidation high plus manual anchors; reclaim → pullback state | `screener.find_consolidation`, `levels.load_manual_anchors`, `indicators.avwap_state` | `levels.avwap.*`, `config/anchors.yaml` |
| 3. Confluence zone (AVWAP + EMA21 + prior high = high priority) | `levels._cluster` | `levels.confluence.*` |
| 4. First retest after a break = priority setup | `levels.retest_status`, `Zone.first_retest` | `levels.retest.*` |
| 5. Pre-open list near the expected open; intraday chart 1/5/15-min | `preopen.preopen_list`, `session.SessionEngine`, `intraday/bars.py` | `preopen.*`, `intraday.timeframe_minutes` |
| 6. Dip → rebound → trades above the previous bar's high | `intraday/trigger.TriggerEngine` (`on_bar`, `check_intrabar`, `on_price`) | `intraday.trigger_offset_ticks`, `intraday.evaluation` |
| 7. Valid only if Stage 1 still passes and price is at the zone | `TriggerEngine._validate` | `intraday.at_zone_max_distance_pct` |
| 8. Falls straight through → no trade, zone marked broken, keep waiting | `TriggerEngine._check_breaks`, `levels.ZoneMemory` | `intraday.zone_break.*`, `levels.broken_zone_memory_days` |

### Stage 3: stop and size (`src/nseswing/risk.py`)

| Rule | Code | Settings |
|---|---|---|
| 1. Stop: day low, trigger-bar low or prior-bar low | `RiskCalculator.choose_stop` | `risk.stop_method` |
| 2. Target 2.5–3%; warn (never block) when wider | `RiskCalculator.plan` | `risk.max_stop_pct_warn` |
| 3. 0.5% account risk | `RiskCalculator.plan` | `account.risk_per_trade_pct` |
| 4. Round entry and stop to ticks, then risk/share, then shares (floor), position, % | `RiskCalculator.plan`, `core/ticks.py` | `ticks.*` |
| 5. Plan in advance, buy on the trigger, stop right after the fill, GTT overnight | `RiskCalculator.draft_plan`, `orders/base.OrderManager`, `TradeManager.end_of_continuous_session` | `trade_management.overnight_stop.*` |
| 6. Gap-down / slippage / lower-circuit warning | plan warnings, `orders/paper.PaperBroker` | `execution.paper.*` |

### Stage 4: manage (`src/nseswing/trademanager.py`)

| Rule | Code | Settings |
|---|---|---|
| 1. R, 3R/5R gross and net of costs | `RiskCalculator.plan`, `costs.ChargeModel.net_target_price` | `targets.r_multiples`, `costs.*` |
| 2. Sell a portion near 3R / 5R | `TradeManager.on_bar` | `trade_management.partials` |
| 3. Trail the rest with the 9 EMA | `TradeManager.end_of_day` | `trade_management.trail.*` |
| 4. Extension alert; near the upper circuit | `TradeManager._extension_check`, `on_bar` | `trade_management.extension.*` |
| 5. Hold-full override when the market is strong and other positions have cushions | `TradeManager.set_hold_full`, `hold_full_hint`, `nseswing hold-full` | `trade_management.hold_full_override.*` |

### Indian market rules

| Rule | Code |
|---|---|
| Sessions 09:15–15:30, pre-open 09:00–09:15 (monitoring only in the normal session) | `core/calendar.Session`, `sessions.*` |
| Closing Auction Session (F&O stocks continuous until 15:15; SL orders cancelled at the transition) | `Session.continuous_end`, `PaperBroker.expire_day_orders`, `sessions.cas.*` |
| NSE holidays and special sessions | `core/calendar.NSECalendar`, `config/nse_calendar.yaml`, holiday check in `refresh-reference` |
| Corporate-action adjustment before returns, EMAs, AVWAP, swings | `corpactions.adjust_ohlcv`, `adjust_intraday` |
| Price bands and circuit flags; no stop fill while locked at the lower circuit | `ReferenceData.circuit_limits`, `CircuitLimits.proximity`, `PaperBroker.on_bar` |
| Tick table and rounding | `core/ticks.TickTable`, `round_to_tick` |
| ASM / GSM / trade-for-trade flags, optional exclusion | `data/reference.SecurityInfo.surveillance_flags`, `surveillance.*` |
| T+1 settlement (and BTST / same-day tax handling) | `NSECalendar.settlement_date`, `TradeManager._record_sell` |
| Overnight stop: GTT, or re-place the SL next morning; DDPI/eDIS note | `TradeManager.end_of_continuous_session`, `start_of_session`, `overnight_compliance`, `orders/upstox_live.gtt_stop_payload` |
| Charges in config; R gross and net | `costs.ChargeModel`, `journal.Journal` |
| SEBI retail-algo rules for live API orders | `orders/upstox_live.assert_live_allowed`, order rate limiter, optional algo header |
| ₹ with Indian digit grouping (lakh / crore) | `core/money.format_inr`, `format_inr_short` |

### Deliverables

| Deliverable | Where |
|---|---|
| 1. Architecture and module list | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 2. One config file with every parameter, assumption, cost rate and the tick table | `config/settings.yaml` (reference [below](#configuration-reference)) |
| 3. Annotated weekly, daily and intraday charts | `charts.py`, the replay report, the dashboard |
| 4. Trade-plan card | `plancard.py` (CLI, dashboard, reports) |
| 5. Trade journal | `journal.py` (SQLite: trades, fills with charges, events, R gross/net) |
| 6. Unit tests incl. acceptance tests | `tests/` (`tests/test_acceptance.py`) |
| 7. This README | |

---

## Acceptance tests

`python -m pytest` runs the whole suite (132 tests). The six acceptance cases from the brief live in
`tests/test_acceptance.py` and are checked exactly:

| Case | Test | Result checked |
|---|---|---|
| Position size | `test_acceptance_position_size` | risk/share ₹12.50, max loss ₹5,000, 400 shares, ₹2,00,000 = 20%, stop 2.5% |
| R targets | `test_acceptance_r_targets_gross` | 1R ₹12.50, 3R ₹537.50, 5R ₹562.50 (plus net targets above gross) |
| Higher lows | `test_acceptance_higher_lows_450_then_475_passes` | prior low ₹450 → current ₹475 passes 7a (series-based and direct) |
| Trigger | `test_acceptance_trigger_fires_one_tick_above_prior_5min_high`, `test_acceptance_no_trigger_when_price_drops_through_zone` | previous 5-min high ₹500.00 → fires at ₹500.05; falling through the zone gives no trigger and a broken zone |
| Tick rounding | `test_acceptance_stop_rounded_to_tick_before_shares` | stop ₹490.03 → ₹490.00 **before** sizing (500 shares, not 501) |
| Overnight stop | `test_acceptance_overnight_cnc_position_has_active_gtt`, `test_acceptance_overnight_flags_replace_sl_next_morning` | after 15:30 a CNC position has an active GTT stop, or the plan flags the SL for 09:15 and it is re-placed |

`tests/test_replay.py` runs the reference scenario end to end: rule order, timings and the
journal result. It also checks the breakdown case (no trade, zones broken), a 5-min replay,
and the live monitor driven by a fake clock and tick feed.

---

## Interpretations of the method (tunable)

Every qualitative rule became a named setting. They are all in `config/settings.yaml`,
tagged `[ASSUMPTION]`, and listed in the [reference](#configuration-reference). The ones
that shape results most:

- **"Meets both conditions"** means the ≥ 30% return and the top-30% rank hold *in the
  same window* (`both_conditions_scope`).
- **"Rose fastest"** means the highest 1-month return (`fastest_metric`).
- **Weekly advance then pullback:** a ≥ 20% rise from a low to the peak within 26 weeks,
  then a ≥ 5% pullback, with the peak not in the current week.
- **Consolidation (base):** the bars since the highest high of the last 60 sessions. That
  bar is also the default AVWAP anchor. The base must be at least 5 bars old.
- **Higher lows:** the last two confirmed fractal swing lows (±3 bars) inside the base.
- **Contracting range:** the average true range % of the last 10 days is ≤ 0.80 × that of
  the earlier part of the base.
- **Reclaimed and flat-or-rising EMA:** the close is above the 9 or 21 EMA after a close
  below it within 15 days, and the EMA's 5-day slope is ≥ −0.05%/day.
- **Disqualifier:** the last two swing highs and lows are both falling, and every close
  of the last 5 days is below a declining 21 and 50 EMA.
- **Confluence:** levels within 1% form one zone.
  - HIGH priority = AVWAP + 21 EMA + a prior high.
  - MEDIUM = two different level types (two EMAs count as one type).
  - LOW = a single level.
- **"At the zone":** the dip low stays within the zone (0.25% tolerance) and the trigger
  is ≤ 1% above the zone top.
- **"Broke straight through":** a bar closes 0.25% below the zone bottom with no trigger,
  or the stock opens below the zone.
- **Partials:** 33% of the original quantity at 3R and another 33% at 5R; the rest trails
  on a daily close below the 9 EMA, sold at the next open.
- **Extension:** the close is ≥ 10% or ≥ 3 ATR above the 9 EMA.
- **Hold-full hint:** the market is "strong" when the Nifty 50 closes above a rising
  21 EMA, and "cushions" means every other position is up ≥ 1R.

Nothing outside the method is switched on by default. The optional extras are tagged
`[OPTIONAL]` and are off: moving the stop to breakeven, a maximum position %, a
no-new-entries time, provisional swing lows, and a cap on pullback depth. The one physical
constraint that is always on is that CNC buys are capped by available cash.

## Facts that changed or that the brief did not mention

These came out of checking current exchange and broker rules (see the **[VERIFY]** tags in
the config):

- **Closing Auction Session (from 3 Aug 2026).** For stocks with F&O contracts, continuous
  trading ends at **15:15**, and **pending stop-loss orders are cancelled** when the stock
  moves into the auction. Other stocks still trade until 15:30. The system stops trigger
  monitoring at 15:15 for those stocks and treats 15:15 as the end of their DAY stop.
  The overnight-stop logic then applies exactly as it does at 15:30 for everything else.
- **Pre-open (from 7 Sep 2026).** The window is still 09:00–09:15. Market orders are taken
  only until about 09:05, and order entry closes at a random moment around 09:08–09:10.
  The expected open is treated as final after 09:10.
- **A triggered Upstox GTT is placed as a LIMIT order.** On a gap below the stop it can sit
  unfilled. The paper broker models this, and the system raises a critical
  `STOP_UNFILLED` alert. If you prefer a market-style exit on gaps, set
  `overnight_stop.method: replace_next_morning`, which re-places an SL-M at 09:15.
  SL orders are not accepted in the pre-open auction.
- **Upstox says its historical candles are already adjusted** for corporate actions. In
  `auto` mode a split or bonus is only applied when the raw series still shows the jump,
  so nothing is adjusted twice. Every decision is printed in the replay notes.
- **SEBI retail-algo framework (from 1 Apr 2026).** Live API orders need a registered
  static IP, a daily login and broker-side algo tagging, and above 10 orders/sec exchange
  registration. The live lock requires `static_ip_registered: true` and limits orders to
  5/sec by default.
- **Same-day exits of CNC buys are taxed as intraday** trades (lower STT, no DP charge). A
  T+1 sale before the shares are credited carries no DP charge by default. The journal
  records these adjustments, so net R is right.

## Safety

- `execution.mode` is `paper` by default. **Live orders need four switches:**
  `mode: live`, `live.enabled: true`, `live.confirm_live_trading: true` and
  `live.static_ip_registered: true`. `UpstoxLiveGateway` re-checks them on every order.
- **No entry without a stop.** `OrderManager.submit_entry` raises `MissingStop`. The stop
  is placed on the fill event. If it is rejected, a CRITICAL alert fires and, by default,
  the position is flattened (`on_stop_failure`).
- **Overnight compliance.** After the close, `TradeManager.overnight_compliance` checks
  that every open delivery position has an active GTT stop or a re-place flag; otherwise it
  raises a critical `UNPROTECTED` alert.
- Plan cards and alerts state that gaps, slippage or a lower-circuit lock can fill below
  the stop, and give the loss at today's lower circuit.

## Verifying the adapters before relying on them

The build sandbox could not reach `upstox.com`, `api.upstox.com`, `assets.upstox.com`,
`nseindia.com` or `nsearchives.nseindia.com`. The Upstox and NSE code was therefore
written from the published formats and tested against hand-written fixtures in
`tests/fixtures/`, never against live responses. On your machine:

1. Run `nseswing refresh-reference` and confirm every line says `OK`. NSE sometimes blocks
   scripted downloads; you can also place the CSV and JSON files in `data/reference/` by hand.
2. Open `data/reference/upstox_instruments_nse.json` and check `tick_size` for a stock you
   know. If 0.05 shows as `5.0`, the field is in paise. `upstox.instrument_tick_unit: auto`
   handles either form, but you can pin it.
3. Replay a stock that had a split or bonus inside the lookback, and read the
   `corporate action … applied / skipped_already_adjusted` notes.
4. Compare `nseswing screen` circuit flags and `preopen` quotes with the Upstox app during
   market hours.
5. Before any live use: keep `mode: paper` for several sessions and compare the paper
   fills with real prints. Then place a 1-share order by hand through Upstox and compare
   the order and GTT payloads in `orders/upstox_live.py`.
6. Check `config/nse_calendar.yaml` against NSE's circular. Two 2026 dates, Bakri Id
   (28 May) and Dussehra (20 Oct), were not confirmed from a primary source. 2027 needs
   adding when NSE publishes it.

## Limitations

- BSE-only listings are not wired in. The universe is the Nifty 500 on NSE.
- Live monitoring polls LTP every 2 s and builds 1-minute bars from it. The Upstox
  WebSocket feed is not used.
- Demergers and unusual actions need a factor in `config/corporate_actions_manual.yaml`.
- The F&O dynamic price band is only an estimate (`price_bands.fno_band_pct`) when no
  live quote is available.
- Turnover is estimated as close × volume, because Upstox candles carry no traded value.

## Project layout

```
config/      settings.yaml (all parameters), nse_calendar.yaml, themes.yaml, anchors.yaml, corporate_actions_manual.yaml
src/nseswing/
  core/      calendar, ticks, money (₹), IST time
  data/      upstox.py, nse.py, reference.py, csvstore.py, refresh.py
  corpactions.py  indicators.py  screener.py  levels.py  preopen.py
  intraday/  bars.py, trigger.py
  risk.py  costs.py  orders/ (base, paper, upstox_live)  trademanager.py
  session.py  replay.py  monitor.py  pipeline.py
  journal.py  alerts.py  plancard.py  charts.py  synthetic.py  cli.py
app/streamlit_app.py   dashboard
scripts/config_reference.py   regenerates the reference below
tests/       132 tests; fixtures/ are hand-written payloads (fictional symbols)
```

## Configuration reference

Every key in `config/settings.yaml`. Tags: **[ASSUMPTION]** turns a qualitative rule into
a number; **[OPTIONAL]** is not part of the method and is off by default; **[VERIFY]** is an
exchange, broker or tax fact to re-check periodically. After editing the YAML, run
`python scripts/config_reference.py`. A test fails if this table is out of date.

<!-- config-reference:start -->

_Generated from `config/settings.yaml` by `python scripts/config_reference.py`. Do not edit by hand._


#### `account`

| Key | Default | Meaning / assumption |
|---|---|---|
| `account.equity_inr` | `1000000` | account equity used for risk sizing (₹10,00,000) |
| `account.risk_per_trade_pct` | `0.5` | Stage 3.3: % of equity you accept losing per trade |
| `account.available_cash_inr` | `null` | cash for new CNC buys; null = same as equity |
| `account.product` | `CNC` | CNC (delivery, Upstox "D") / MIS (intraday, Upstox "I"). The method holds overnight, so CNC. |

#### `universe`

| Key | Default | Meaning / assumption |
|---|---|---|
| `universe.name` | `NIFTY500` | label only |
| `universe.constituents_url` | `https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv` | NSE's Nifty 500 constituent list (downloaded by refresh-reference) |
| `universe.constituents_file` | `data/reference/ind_nifty500list.csv` | cached copy; edit or replace to change the universe |
| `universe.extra_symbols` | `[]` | symbols you add by hand (NSE symbols) |
| `universe.exclude_symbols` | `[]` | symbols you never want screened |
| `universe.series_allowed` | `[EQ, BE, BZ]` | BE/BZ (trade-for-trade) are kept and flagged, see surveillance.* |

#### `data`

| Key | Default | Meaning / assumption |
|---|---|---|
| `data.provider` | `upstox` | upstox / csv  (csv = local files; used by replay, tests and the demo) |
| `data.csv_dir` | `data/csv` | csv layout: daily/<SYMBOL>.csv and intraday/<SYMBOL>/<YYYY-MM-DD>_1min.csv |
| `data.cache_dir` | `data/cache` | downloaded candles are cached here |
| `data.reference_dir` | `data/reference` | downloaded NSE / Upstox reference files |
| `data.state_dir` | `data/state` | broken zones, paper-broker state |
| `data.daily_history_trading_days` | `300` | bars fetched per stock (≥ 126 for 6M return + EMA50 / 50-day turnover warm-up) |
| `data.min_history_trading_days` | `140` | stocks with fewer bars are skipped, with the reason shown |
| `data.turnover_price` | `close` | [ASSUMPTION] Upstox candles carry no traded value, so turnover ≈ price × volume using close / hlc3 |

#### `upstox`

| Key | Default | Meaning / assumption |
|---|---|---|
| `upstox.api_base` | `https://api.upstox.com` | Upstox REST base URL |
| `upstox.hft_base` | `https://api-hft.upstox.com` | Upstox low-latency order endpoint (used only if execution.live.use_hft_endpoint) |
| `upstox.instruments_url` | `https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz` | Upstox instrument master (instrument keys, tick sizes) |
| `upstox.client_id_env` | `UPSTOX_API_KEY` | env var holding your app's API key |
| `upstox.client_secret_env` | `UPSTOX_API_SECRET` | env var holding your app's secret |
| `upstox.redirect_uri_env` | `UPSTOX_REDIRECT_URI` | env var holding the redirect URL registered for the app |
| `upstox.access_token_env` | `UPSTOX_ACCESS_TOKEN` | env var checked first for today's token |
| `upstox.token_file` | `.upstox_token.json` | written by `nseswing upstox-login` (gitignored). Tokens expire daily. |
| `upstox.timeout_seconds` | `15` | HTTP timeout per request |
| `upstox.rate_limit_per_second` | `20` | [VERIFY] Upstox standard APIs allow about 50/s; stay well below |
| `upstox.rate_limit_per_minute` | `400` | [VERIFY] about 500/min |
| `upstox.intraday_max_days_per_request` | `28` | [VERIFY] v3 minute candles: about one month per request |
| `upstox.daily_max_days_per_request` | `3650` | [VERIFY] v3 day candles: about ten years per request |
| `upstox.quote_batch_size` | `200` | instrument keys per quote/LTP request (API limit 500) |
| `upstox.instrument_tick_unit` | `auto` | [VERIFY] tick_size in the instrument file: rupees / paise / auto (pick the reading that fits the tick table) |
| `upstox.index_keys` | `NIFTY50: NSE_INDEX\|Nifty 50`<br>`NIFTY500: NSE_INDEX\|Nifty 500` | benchmark instrument keys (hold-full override hint) |

#### `nse`

| Key | Default | Meaning / assumption |
|---|---|---|
| `nse.user_agent` | `Mozilla/5.0 (compatible; nseswing/0.1)` | generic client name sent to NSE |
| `nse.timeout_seconds` | `20` | HTTP timeout per request |
| `nse.home_url` | `https://www.nseindia.com` | visited first to obtain cookies for /api/ calls |
| `nse.sec_list_url` | `https://nsearchives.nseindia.com/content/equities/sec_list.csv` | [VERIFY] price bands + series |
| `nse.fo_list_url` | `https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv` | [VERIFY] F&O underlyings (CAS, no fixed band) |
| `nse.asm_api` | `https://www.nseindia.com/api/reportASM` | [VERIFY] |
| `nse.gsm_api` | `https://www.nseindia.com/api/reportGSM` | [VERIFY] |
| `nse.corporate_actions_api` | `https://www.nseindia.com/api/corporates-corporateActions` | [VERIFY] |
| `nse.holidays_api` | `https://www.nseindia.com/api/holiday-master?type=trading` | [VERIFY] |
| `nse.preopen_api` | `https://www.nseindia.com/api/market-data-pre-open?key=ALL` | [VERIFY] |
| `nse.corporate_actions_lookback_days` | `400` | calendar days of corporate actions downloaded (≥ the daily history) |

#### `calendar`

| Key | Default | Meaning / assumption |
|---|---|---|
| `calendar.file` | `config/nse_calendar.yaml` | holidays + special sessions (a data file you update each December) |
| `calendar.fail_if_year_missing` | `false` | true: refuse dates in years the file does not cover; false: warn, treat weekdays as sessions |

#### `sessions`

| Key | Default | Meaning / assumption |
|---|---|---|
| `sessions.preopen_start` | `09:00` | pre-open call auction starts |
| `sessions.preopen_order_entry_end` | `09:08` | [VERIFY] since 7 Sep 2026 order entry closes at a random moment about 09:08–09:10 |
| `sessions.preopen_iep_final_after` | `09:10` | expected open is treated as final after this time |
| `sessions.market_open` | `09:15` | normal market (continuous trading) opens |
| `sessions.market_close` | `15:30` | normal market closes (non-CAS stocks; DAY orders expire) |
| `sessions.cas.enabled` | `true` | [VERIFY] Closing Auction Session (NSE, from 3 Aug 2026) |
| `sessions.cas.effective_from` | `2026-08-03` | [VERIFY] Closing Auction Session (NSE, from 3 Aug 2026) |
| `sessions.cas.applies_to` | `fno_stocks` | fno_stocks / all / none |
| `sessions.cas.continuous_end` | `15:15` | continuous trading ends; the exchange cancels pending SL orders |
| `sessions.cas.auction_end` | `15:35` | closing price known after this |

#### `corporate_actions`

| Key | Default | Meaning / assumption |
|---|---|---|
| `corporate_actions.mode` | `auto` | auto / always / never. auto applies a large action only if the raw series still shows its jump (no double adjustment) |
| `corporate_actions.verify_min_jump_pct` | `15` | [ASSUMPTION] actions moving price ≥ 15% are verified against the raw prev-close → open jump |
| `corporate_actions.adjust_rights` | `true` | rights issues adjusted with the theoretical ex-rights price |
| `corporate_actions.adjust_dividends` | `large_only` | none / all / large_only |
| `corporate_actions.large_dividend_min_pct` | `5.0` | [ASSUMPTION] a dividend ≥ 5% of the cum price is "large" |
| `corporate_actions.adjust_volume` | `true` | volume × 1/factor for splits, bonuses and consolidations |
| `corporate_actions.unexplained_gap_warn_pct` | `45` | [ASSUMPTION] warn on an overnight drop ≥ 45% with no recorded action (possible unadjusted split) |
| `corporate_actions.actions_file` | `data/reference/corporate_actions.json` | downloaded from NSE by `nseswing refresh-reference` |
| `corporate_actions.manual_actions_file` | `config/corporate_actions_manual.yaml` | demergers etc. with a factor you supply |

#### `ticks`

| Key | Default | Meaning / assumption |
|---|---|---|
| `ticks.table` | `{max_price: 250, max_inclusive: false, tick: 0.01}`<br>`{max_price: 1000, max_inclusive: true, tick: 0.05}`<br>`{max_price: 5000, max_inclusive: true, tick: 0.1}`<br>`{max_price: 10000, max_inclusive: true, tick: 0.5}`<br>`{max_price: 20000, max_inclusive: true, tick: 1.0}`<br>`{max_price: null, max_inclusive: false, tick: 5.0}` | [VERIFY] NSE price-slab tick sizes (revision effective 15 Apr 2025). A slab applies while price < max_price (or <= when max_inclusive is true). The last slab has max_price: null. |
| `ticks.prefer_instrument_master` | `true` | use the tick from Upstox's instrument file when available; otherwise the slab of the last close (NSE reviews slabs on closing prices) |
| `ticks.rounding.entry` | `up` | buy trigger / entry rounded up to a valid tick |
| `ticks.rounding.stop` | `down` | long stop rounded down, so risk is never understated |
| `ticks.rounding.target` | `down` | sell targets rounded down, so they are reachable |
| `ticks.rounding.zone` | `nearest` | zone edges |

#### `price_bands`

| Key | Default | Meaning / assumption |
|---|---|---|
| `price_bands.sec_list_file` | `data/reference/sec_list.csv` | NSE security list with the price band per symbol |
| `price_bands.fo_list_file` | `data/reference/fo_mktlots.csv` | NSE F&O underlyings list |
| `price_bands.fno_band_pct` | `10` | [ASSUMPTION] F&O stocks have no fixed band; this dynamic-band estimate is used when no live quote is available |
| `price_bands.default_band_pct` | `20` | [ASSUMPTION] band used when a symbol is missing from sec_list |
| `price_bands.near_circuit_pct` | `1.0` | [ASSUMPTION] "near" a circuit = within 1% of the circuit price |
| `price_bands.locked_tolerance_ticks` | `1` | a bar is "locked" at a circuit when its whole range is within this many ticks of the limit |

#### `surveillance`

| Key | Default | Meaning / assumption |
|---|---|---|
| `surveillance.asm_file` | `data/reference/asm.json` | downloaded ASM report |
| `surveillance.gsm_file` | `data/reference/gsm.json` | downloaded GSM report |
| `surveillance.manual_flags` | `{}` | {SYMBOL: "your reason"} flags you add yourself |
| `surveillance.t2t_series` | `[BE, BZ, BT]` | series treated as trade-for-trade |
| `surveillance.exclude_asm` | `false` | option to drop ASM names (default: flag only) |
| `surveillance.exclude_gsm` | `false` | option to drop GSM names (default: flag only) |
| `surveillance.exclude_t2t` | `false` | option to drop trade-for-trade names (default: flag only) |

#### `screener`

| Key | Default | Meaning / assumption |
|---|---|---|
| `screener.return_windows.m1` | `21` | Stage 1.1, trading days |
| `screener.return_windows.m3` | `63` | Stage 1.1, trading days |
| `screener.return_windows.m6` | `126` | Stage 1.1, trading days |
| `screener.momentum.min_return_pct` | `30` | Stage 1.2 absolute condition |
| `screener.momentum.top_rank_pct` | `30` | Stage 1.2 relative condition (method range 20–30) |
| `screener.momentum.both_conditions_scope` | `same_window` | [ASSUMPTION] "meets both" = both in one window (same_window) or each in any window (any_window) |
| `screener.liquidity.avg_days` | `50` | averaging window for turnover and volume |
| `screener.liquidity.min_avg_turnover_cr` | `10.0` | [ASSUMPTION] ₹ crore per day |
| `screener.liquidity.min_avg_volume` | `10000` | [ASSUMPTION] shares per day |
| `screener.liquidity.min_price` | `50.0` | [ASSUMPTION] ₹ |
| `screener.themes.min_members_hot` | `3` | Stage 1.5: a group is hot when ≥ 3 members passed step 2 |
| `screener.themes.use_industry` | `true` | group by the NSE industry column of the constituent list |
| `screener.themes.index_themes` | `NIFTY IT: https://nsearchives.nseindia.com/content/indices/ind_niftyitlist.csv`<br>`NIFTY PSU BANK: https://nsearchives.nseindia.com/content/indices/ind_niftypsubanklist.csv`<br>`NIFTY REALTY: https://nsearchives.nseindia.com/content/indices/ind_niftyrealtylist.csv`<br>`NIFTY INDIA DEFENCE: https://nsearchives.nseindia.com/content/indices/ind_niftyindiadefence_list.csv` | [VERIFY file names] thematic index → constituent CSV |
| `screener.themes.custom_themes_file` | `config/themes.yaml` | your own theme lists |
| `screener.themes.fastest_metric` | `ret_m1` | [ASSUMPTION] "rose fastest" = ret_m1 / ret_m3 / ret_m6 / best_rank |
| `screener.weekly.lookback_weeks` | `26` | [ASSUMPTION] window searched for the advance peak |
| `screener.weekly.advance_lookback_weeks` | `26` | [ASSUMPTION] how far before the peak the advance may start |
| `screener.weekly.min_advance_pct` | `20` | [ASSUMPTION] lowest low → peak gain |
| `screener.weekly.min_pullback_pct` | `5` | [ASSUMPTION] peak → lowest low after the peak |
| `screener.weekly.min_weeks_since_peak` | `1` | [ASSUMPTION] the peak is not in the current week |
| `screener.weekly.max_pullback_of_advance_pct` | `null` | [OPTIONAL] cap retracement as % of the advance (null = off) |
| `screener.daily.consolidation_lookback_days` | `60` | [ASSUMPTION] consolidation high = highest high in this window |
| `screener.daily.min_base_days` | `5` | [ASSUMPTION] that high must be at least this many bars old |
| `screener.daily.swing_lookaround` | `3` | [ASSUMPTION] swing low/high = lowest/highest bar within ±3 bars |
| `screener.daily.use_provisional_last_low` | `false` | [OPTIONAL] also count the latest, not yet confirmed, low |
| `screener.daily.contraction.method` | `atr_ratio` | atr_ratio / swing_amplitude |
| `screener.daily.contraction.recent_days` | `10` | [ASSUMPTION] |
| `screener.daily.contraction.max_ratio` | `0.8` | [ASSUMPTION] recent average true range % ≤ 0.80 × the earlier part of the base |
| `screener.daily.contraction.min_earlier_days` | `5` | earlier part needs ≥ 5 bars ... |
| `screener.daily.contraction.fallback_earlier_days` | `20` | ... otherwise compare with the 20 bars before the recent window |
| `screener.daily.ema_reclaim.emas` | `[9, 21]` | 7c |
| `screener.daily.ema_reclaim.require_prior_close_below` | `true` | [ASSUMPTION] "reclaimed" = close above now, after a close below within the lookback |
| `screener.daily.ema_reclaim.reclaim_lookback_days` | `15` | 7c |
| `screener.daily.ema_reclaim.slope_days` | `5` | 7c |
| `screener.daily.ema_reclaim.min_slope_pct_per_day` | `-0.05` | [ASSUMPTION] "flat or turning up" |
| `screener.disqualify.swing_count` | `2` | compare the last 2 swing highs and the last 2 swing lows |
| `screener.disqualify.emas` | `[21, 50]` | [ASSUMPTION] the "moving averages" price stays below |
| `screener.disqualify.below_days` | `5` | [ASSUMPTION] "stays below" = every close of the last 5 days below both |
| `screener.rank_order` | `[all_checks_pass, in_hot_group, both_conditions, fastest]` | watchlist sort keys, in order |

#### `levels`

| Key | Default | Meaning / assumption |
|---|---|---|
| `levels.emas` | `[9, 21, 50]` | Stage 2.1 |
| `levels.swing_lookaround` | `3` | [ASSUMPTION] prior swing highs/lows |
| `levels.lookback_days` | `120` | [ASSUMPTION] levels older than this are ignored |
| `levels.gaps.min_gap_pct` | `0.5` | [ASSUMPTION] smaller gaps are ignored |
| `levels.gaps.lookback_days` | `120` | how far back unfilled gaps are tracked |
| `levels.avwap.price` | `hlc3` | typical price used: hlc3 / ohlc4 / close |
| `levels.avwap.manual_anchors_file` | `config/anchors.yaml` | your manual AVWAP anchors |
| `levels.avwap.pullback_near_pct` | `1.5` | [ASSUMPTION] "pulls back toward it" = low within 1.5% above AVWAP after a reclaim |
| `levels.confluence.tolerance_pct` | `1.0` | [ASSUMPTION] levels within 1% of each other form one zone |
| `levels.confluence.zone_pad_pct` | `0.15` | [ASSUMPTION] zone edges padded by 0.15% |
| `levels.confluence.search_below_pct` | `12` | zones considered from 12% below ... |
| `levels.confluence.search_above_pct` | `1` | ... to 1% above the last close |
| `levels.confluence.high_priority_requires` | `[avwap, ema21, prior_high]` | Stage 2.3 high-priority recipe |
| `levels.confluence.medium_priority_min_types` | `2` | [ASSUMPTION] ≥ 2 different level types = medium priority |
| `levels.retest.touch_tolerance_pct` | `0.3` | [ASSUMPTION] a "touch" = low within 0.3% above the level |
| `levels.retest.important_swing_lookaround` | `5` | [ASSUMPTION] "important" prior highs use ±5 bars |
| `levels.broken_zone_memory_days` | `10` | [ASSUMPTION] a broken zone stays marked for 10 sessions unless price closes back above it |

#### `preopen`

| Key | Default | Meaning / assumption |
|---|---|---|
| `preopen.expected_open_source` | `upstox_quote` | upstox_quote / nse_preopen / prev_close |
| `preopen.near_zone_pct` | `3.0` | [ASSUMPTION] list a stock when its zone top is within 3% below the expected open (or the open is inside the zone) |
| `preopen.gap_below_zone_marks_broken` | `true` | [ASSUMPTION] opening below a zone counts as "broke straight through" |

#### `intraday`

| Key | Default | Meaning / assumption |
|---|---|---|
| `intraday.timeframe_minutes` | `5` | 1 / 5 / 15: monitored intraday chart |
| `intraday.approach_pct` | `1.0` | [ASSUMPTION] start monitoring when price is within 1% above a zone top |
| `intraday.trigger_offset_ticks` | `1` | "trades above the previous bar's high" = that high + 1 tick |
| `intraday.evaluation` | `intrabar` | intrabar (fires on the first trade above) / bar_close |
| `intraday.at_zone_max_distance_pct` | `1.0` | [ASSUMPTION] "at the zone" = trigger price ≤ zone top + 1% |
| `intraday.zone_break.rule` | `close_below` | close_below / trade_below |
| `intraday.zone_break.tolerance_pct` | `0.25` | [ASSUMPTION] broken = a bar closes 0.25% below the zone bottom with no trigger |
| `intraday.max_entries_per_symbol_per_day` | `1` | one entry per stock per session |
| `intraday.no_new_entries_after` | `null` | [OPTIONAL] e.g. "15:00" |
| `intraday.poll_seconds` | `2` | live LTP polling interval |

#### `risk`

| Key | Default | Meaning / assumption |
|---|---|---|
| `risk.stop_method` | `day_low` | day_low / trigger_bar_low / prior_bar_low  (Stage 3.1) |
| `risk.stop_buffer_ticks` | `0` | [OPTIONAL] place the stop N ticks below the chosen low |
| `risk.max_stop_pct_warn` | `3.0` | Stage 3.2: warn (do not block) above this |
| `risk.typical_stop_pct_min` | `2.5` | shown as information when the stop is tighter than the usual 2.5–3% |
| `risk.cap_to_available_cash` | `true` | CNC has no leverage: shares are capped by available cash |
| `risk.max_position_pct` | `null` | [OPTIONAL] cap position value as % of equity |

#### `targets`

| Key | Default | Meaning / assumption |
|---|---|---|
| `targets.r_multiples` | `[1, 3, 5]` | Stage 4.1 targets shown on every plan |

#### `trade_management`

| Key | Default | Meaning / assumption |
|---|---|---|
| `trade_management.partials` | `{at_r: 3, fraction: 0.33}`<br>`{at_r: 5, fraction: 0.33}` | Stage 4.2: "a portion" near 3R and/or 5R. Fractions of the ORIGINAL quantity. |
| `trade_management.near_target_pct` | `0.5` | alert when price is within 0.5% of a target |
| `trade_management.trail.rule` | `close_below_ema` | close_below_ema / close_below_prev_low |
| `trade_management.trail.ema` | `9` | Stage 4.3 |
| `trade_management.trail.activation` | `first_target` | [ASSUMPTION] first_target = trail once 3R is reached ("the rest") / entry |
| `trade_management.trail.exit_timing` | `next_open` | next_open / same_close |
| `trade_management.extension.pct_above_ema` | `10.0` | [ASSUMPTION] stretched = close ≥ 10% above the 9 EMA ... |
| `trade_management.extension.atr_multiple` | `3.0` | [ASSUMPTION] ... or ≥ 3 ATR above it |
| `trade_management.extension.atr_period` | `14` | Stage 4.4 |
| `trade_management.extension.ema` | `9` | Stage 4.4 |
| `trade_management.extension.mode` | `either` | either / both / pct / atr |
| `trade_management.upper_circuit_near_pct` | `1.0` | flag positions within 1% of the upper circuit |
| `trade_management.hold_full_override.allowed` | `true` | Stage 4.5 (manual; the system only shows a hint) |
| `trade_management.hold_full_override.benchmark` | `NIFTY50` | Stage 4.5 (manual; the system only shows a hint) |
| `trade_management.hold_full_override.market_strong_ema` | `21` | [ASSUMPTION] strong market = benchmark close > its 21 EMA and that EMA's slope ≥ 0 |
| `trade_management.hold_full_override.min_cushion_r` | `1.0` | [ASSUMPTION] every other open position is up ≥ 1R |
| `trade_management.move_stop_to_breakeven_after_first_partial` | `false` | [OPTIONAL] |
| `trade_management.overnight_stop.method` | `gtt` | gtt / replace_next_morning |
| `trade_management.overnight_stop.replace_time` | `09:15` | SL orders are not accepted in the pre-open call auction |
| `trade_management.overnight_stop.gtt_unfilled_alert` | `true` | alert when a triggered GTT limit order sits unfilled |

#### `costs`

| Key | Default | Meaning / assumption |
|---|---|---|
| `costs.brokerage.delivery_per_order_inr` | `20.0` | Upstox delivery: ₹20 per executed order |
| `costs.brokerage.delivery_pct_cap` | `2.5` | ... or 2.5% of order value if lower |
| `costs.brokerage.intraday_per_order_inr` | `20.0` | Upstox intraday: ₹20 per executed order ... |
| `costs.brokerage.intraday_pct_cap` | `0.05` | ... or this % of order value if lower (also used for same-day CNC exits) |
| `costs.stt.delivery_buy_pct` | `0.1` | STT on delivery buys |
| `costs.stt.delivery_sell_pct` | `0.1` | STT on delivery sells |
| `costs.stt.intraday_sell_pct` | `0.025` | STT on intraday sells (applies to same-day exits of CNC buys) |
| `costs.stt.round_to_rupee` | `true` | STT is rounded to the nearest rupee on contract notes |
| `costs.exchange_txn_pct` | `0.00297` | NSE cash market, each side (₹297 / crore) |
| `costs.sebi_fee_per_crore_inr` | `10.0` | [VERIFY] statutory rates and the Upstox tariff; check against your contract note |
| `costs.ipft_per_crore_inr` | `10.0` | NSE investor protection fund levy |
| `costs.stamp_duty.delivery_buy_pct` | `0.015` | stamp duty on delivery buys (₹1,500 / crore) |
| `costs.stamp_duty.intraday_buy_pct` | `0.003` | stamp duty on intraday buys |
| `costs.gst_pct` | `18.0` | [VERIFY] statutory rates and the Upstox tariff; check against your contract note |
| `costs.gst_applies_to` | `[brokerage, exchange_txn, sebi_fee, ipft, dp]` | [VERIFY] statutory rates and the Upstox tariff; check against your contract note |
| `costs.dp_charge_per_scrip_per_sell_day_inr` | `20.0` | Upstox shows ₹18.5–20 per scrip per sell day (+ GST) |
| `costs.dp_on_btst_sell` | `false` | [ASSUMPTION] no DP charge when shares are sold before the T+1 credit |

#### `settlement`

| Key | Default | Meaning / assumption |
|---|---|---|
| `settlement.cycle_days` | `1` | T+1 rolling settlement |

#### `execution`

| Key | Default | Meaning / assumption |
|---|---|---|
| `execution.mode` | `paper` | alerts / paper / live. No live orders unless all three live switches are on. |
| `execution.paper.slippage_ticks` | `1` | adverse slippage on market / stop fills |
| `execution.paper.gtt_fill_model` | `limit_at_trigger` | limit_at_trigger (Upstox places a triggered GTT as a LIMIT order) / market |
| `execution.paper.state_file` | `data/state/paper_broker.json` | paper orders persisted between sessions |
| `execution.live.enabled` | `false` | live switch 2 of 3 |
| `execution.live.confirm_live_trading` | `false` | live switch 3 of 3 |
| `execution.live.static_ip_registered` | `false` | [VERIFY] SEBI retail-algo framework: orders only from your registered static IP |
| `execution.live.max_orders_per_second` | `5` | [VERIFY] stay below the 10 orders/sec exchange registration threshold |
| `execution.live.entry_order_type` | `LIMIT` | LIMIT / MARKET |
| `execution.live.entry_limit_buffer_ticks` | `2` | limit price = trigger + 2 ticks |
| `execution.live.stop_order_type` | `SL-M` | SL-M / SL |
| `execution.live.stop_limit_buffer_pct` | `0.5` | SL only: limit price this far below the trigger price |
| `execution.live.order_tag` | `nseswing` | tag attached to every order |
| `execution.live.on_stop_failure` | `alert_and_flatten` | alert_and_flatten / alert |
| `execution.live.algo_header_name` | `null` | [VERIFY] header your broker asks algo orders to carry (e.g. X-Algo-Name), if any |
| `execution.live.algo_header_value` | `null` | value for algo_header_name |
| `execution.live.use_hft_endpoint` | `false` | true: place orders via api-hft.upstox.com/v3 |

#### `alerts`

| Key | Default | Meaning / assumption |
|---|---|---|
| `alerts.console` | `true` | print alerts to the terminal |
| `alerts.file` | `data/logs/alerts.jsonl` | JSONL log of every alert |
| `alerts.min_level` | `info` | debug / info / warning / critical |
| `alerts.telegram.enabled` | `false` | also send alerts to Telegram |
| `alerts.telegram.bot_token_env` | `TELEGRAM_BOT_TOKEN` | env var with the Telegram bot token |
| `alerts.telegram.chat_id_env` | `TELEGRAM_CHAT_ID` | env var with the Telegram chat id |

#### `charts`

| Key | Default | Meaning / assumption |
|---|---|---|
| `charts.output_dir` | `reports/charts` | HTML reports are written here |
| `charts.plotly_js` | `cdn` | cdn (small files, needs internet) / inline (offline, ~4 MB each) |
| `charts.daily_bars_shown` | `120` | sessions shown on the daily chart |
| `charts.weekly_bars_shown` | `78` | weeks shown on the weekly chart |
| `charts.height_px` | `760` | daily chart height |

#### `journal`

| Key | Default | Meaning / assumption |
|---|---|---|
| `journal.db_path` | `data/journal.sqlite` | SQLite trade journal |

<!-- config-reference:end -->
