"""Typed access to ``config/settings.yaml``.

The YAML file is the single source of every parameter, assumption, cost rate and
the tick table. The models below deliberately carry **no default values** and
forbid unknown keys: a missing or misspelled key stops the program at startup
instead of silently falling back to a value hidden in code.

Use :func:`load_settings` to read the file; pass ``overrides`` (a nested dict) to
change individual values, e.g. in tests or from the CLI.
"""

from __future__ import annotations

import copy
from datetime import date, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------- account / universe / data
class AccountCfg(_Model):
    equity_inr: Decimal
    risk_per_trade_pct: Decimal
    available_cash_inr: Decimal | None
    product: Literal["CNC", "MIS"]


class UniverseCfg(_Model):
    name: str
    constituents_url: str
    constituents_file: str
    extra_symbols: list[str]
    exclude_symbols: list[str]
    series_allowed: list[str]


class DataCfg(_Model):
    provider: Literal["upstox", "csv"]
    csv_dir: str
    cache_dir: str
    reference_dir: str
    state_dir: str
    daily_history_trading_days: int
    min_history_trading_days: int
    turnover_price: Literal["close", "hlc3"]


class UpstoxCfg(_Model):
    api_base: str
    hft_base: str
    instruments_url: str
    client_id_env: str
    client_secret_env: str
    redirect_uri_env: str
    access_token_env: str
    token_file: str
    timeout_seconds: float
    rate_limit_per_second: int
    rate_limit_per_minute: int
    intraday_max_days_per_request: int
    daily_max_days_per_request: int
    quote_batch_size: int
    instrument_tick_unit: Literal["auto", "rupees", "paise"]
    index_keys: dict[str, str]


class NseCfg(_Model):
    user_agent: str
    timeout_seconds: float
    home_url: str
    sec_list_url: str
    fo_list_url: str
    asm_api: str
    gsm_api: str
    corporate_actions_api: str
    holidays_api: str
    preopen_api: str
    corporate_actions_lookback_days: int


# --------------------------------------------------------------------------- calendar / sessions
class CalendarCfg(_Model):
    file: str
    fail_if_year_missing: bool


class CasCfg(_Model):
    enabled: bool
    effective_from: date
    applies_to: Literal["fno_stocks", "all", "none"]
    continuous_end: time
    auction_end: time


class SessionsCfg(_Model):
    preopen_start: time
    preopen_order_entry_end: time
    preopen_iep_final_after: time
    market_open: time
    market_close: time
    cas: CasCfg


# --------------------------------------------------------------------------- corporate actions / ticks / bands
class CorporateActionsCfg(_Model):
    mode: Literal["auto", "always", "never"]
    verify_min_jump_pct: float
    adjust_rights: bool
    adjust_dividends: Literal["none", "all", "large_only"]
    large_dividend_min_pct: float
    adjust_volume: bool
    unexplained_gap_warn_pct: float
    actions_file: str
    manual_actions_file: str


class TickSlab(_Model):
    max_price: Decimal | None
    max_inclusive: bool
    tick: Decimal


class TickRoundingCfg(_Model):
    entry: Literal["up", "down", "nearest"]
    stop: Literal["up", "down", "nearest"]
    target: Literal["up", "down", "nearest"]
    zone: Literal["up", "down", "nearest"]


class TicksCfg(_Model):
    table: list[TickSlab]
    prefer_instrument_master: bool
    rounding: TickRoundingCfg

    @model_validator(mode="after")
    def _check_table(self) -> "TicksCfg":
        if not self.table:
            raise ValueError("ticks.table must not be empty")
        if self.table[-1].max_price is not None:
            raise ValueError("the last ticks.table slab must have max_price: null")
        prev = Decimal("0")
        for slab in self.table[:-1]:
            if slab.max_price is None or slab.max_price <= prev:
                raise ValueError("ticks.table max_price values must increase")
            prev = slab.max_price
        return self


class PriceBandsCfg(_Model):
    sec_list_file: str
    fo_list_file: str
    fno_band_pct: float
    default_band_pct: float
    near_circuit_pct: float
    locked_tolerance_ticks: int


class SurveillanceCfg(_Model):
    asm_file: str
    gsm_file: str
    manual_flags: dict[str, str]
    t2t_series: list[str]
    exclude_asm: bool
    exclude_gsm: bool
    exclude_t2t: bool


# --------------------------------------------------------------------------- screener (Stage 1)
class ReturnWindowsCfg(_Model):
    m1: int
    m3: int
    m6: int


class MomentumCfg(_Model):
    min_return_pct: float
    top_rank_pct: float
    both_conditions_scope: Literal["same_window", "any_window"]


class LiquidityCfg(_Model):
    avg_days: int
    min_avg_turnover_cr: float
    min_avg_volume: float
    min_price: float


class ThemesCfg(_Model):
    min_members_hot: int
    use_industry: bool
    index_themes: dict[str, str]
    custom_themes_file: str
    fastest_metric: Literal["ret_m1", "ret_m3", "ret_m6", "best_rank"]


class WeeklyCfg(_Model):
    lookback_weeks: int
    advance_lookback_weeks: int
    min_advance_pct: float
    min_pullback_pct: float
    min_weeks_since_peak: int
    max_pullback_of_advance_pct: float | None


class ContractionCfg(_Model):
    method: Literal["atr_ratio", "swing_amplitude"]
    recent_days: int
    max_ratio: float
    min_earlier_days: int
    fallback_earlier_days: int


class EmaReclaimCfg(_Model):
    emas: list[int]
    require_prior_close_below: bool
    reclaim_lookback_days: int
    slope_days: int
    min_slope_pct_per_day: float


class DailyChecksCfg(_Model):
    consolidation_lookback_days: int
    min_base_days: int
    swing_lookaround: int
    use_provisional_last_low: bool
    contraction: ContractionCfg
    ema_reclaim: EmaReclaimCfg


class DisqualifyCfg(_Model):
    swing_count: int
    emas: list[int]
    below_days: int


class ScreenerCfg(_Model):
    return_windows: ReturnWindowsCfg
    momentum: MomentumCfg
    liquidity: LiquidityCfg
    themes: ThemesCfg
    weekly: WeeklyCfg
    daily: DailyChecksCfg
    disqualify: DisqualifyCfg
    rank_order: list[Literal["all_checks_pass", "in_hot_group", "both_conditions", "fastest"]]


# --------------------------------------------------------------------------- levels (Stage 2) / pre-open / intraday
class GapsCfg(_Model):
    min_gap_pct: float
    lookback_days: int


class AvwapCfg(_Model):
    price: Literal["hlc3", "ohlc4", "close"]
    manual_anchors_file: str
    pullback_near_pct: float


class ConfluenceCfg(_Model):
    tolerance_pct: float
    zone_pad_pct: float
    search_below_pct: float
    search_above_pct: float
    high_priority_requires: list[str]
    medium_priority_min_types: int


class RetestCfg(_Model):
    touch_tolerance_pct: float
    important_swing_lookaround: int


class LevelsCfg(_Model):
    emas: list[int]
    swing_lookaround: int
    lookback_days: int
    gaps: GapsCfg
    avwap: AvwapCfg
    confluence: ConfluenceCfg
    retest: RetestCfg
    broken_zone_memory_days: int


class PreopenCfg(_Model):
    expected_open_source: Literal["upstox_quote", "nse_preopen", "prev_close"]
    near_zone_pct: float
    gap_below_zone_marks_broken: bool


class ZoneBreakCfg(_Model):
    rule: Literal["close_below", "trade_below"]
    tolerance_pct: float


class IntradayCfg(_Model):
    timeframe_minutes: Literal[1, 5, 15]
    approach_pct: float
    trigger_offset_ticks: int
    evaluation: Literal["intrabar", "bar_close"]
    at_zone_max_distance_pct: float
    zone_break: ZoneBreakCfg
    max_entries_per_symbol_per_day: int
    no_new_entries_after: time | None
    poll_seconds: float


# --------------------------------------------------------------------------- risk / targets / management
class RiskCfg(_Model):
    stop_method: Literal["day_low", "trigger_bar_low", "prior_bar_low"]
    stop_buffer_ticks: int
    max_stop_pct_warn: Decimal
    typical_stop_pct_min: Decimal
    cap_to_available_cash: bool
    max_position_pct: Decimal | None


class TargetsCfg(_Model):
    r_multiples: list[Decimal]


class PartialCfg(_Model):
    at_r: Decimal
    fraction: Decimal


class TrailCfg(_Model):
    rule: Literal["close_below_ema", "close_below_prev_low"]
    ema: int
    activation: Literal["first_target", "entry"]
    exit_timing: Literal["next_open", "same_close"]


class ExtensionCfg(_Model):
    pct_above_ema: float
    atr_multiple: float
    atr_period: int
    ema: int
    mode: Literal["either", "both", "pct", "atr"]


class HoldFullCfg(_Model):
    allowed: bool
    benchmark: str
    market_strong_ema: int
    min_cushion_r: float


class OvernightStopCfg(_Model):
    method: Literal["gtt", "replace_next_morning"]
    replace_time: time
    gtt_unfilled_alert: bool


class TradeManagementCfg(_Model):
    partials: list[PartialCfg]
    near_target_pct: float
    trail: TrailCfg
    extension: ExtensionCfg
    upper_circuit_near_pct: float
    hold_full_override: HoldFullCfg
    move_stop_to_breakeven_after_first_partial: bool
    overnight_stop: OvernightStopCfg

    @model_validator(mode="after")
    def _check_partials(self) -> "TradeManagementCfg":
        total = sum((p.fraction for p in self.partials), Decimal("0"))
        if total >= 1:
            raise ValueError("trade_management.partials fractions must leave shares to trail (sum < 1)")
        return self


# --------------------------------------------------------------------------- costs / settlement
class BrokerageCfg(_Model):
    delivery_per_order_inr: Decimal
    delivery_pct_cap: Decimal
    intraday_per_order_inr: Decimal
    intraday_pct_cap: Decimal


class SttCfg(_Model):
    delivery_buy_pct: Decimal
    delivery_sell_pct: Decimal
    intraday_sell_pct: Decimal
    round_to_rupee: bool


class StampCfg(_Model):
    delivery_buy_pct: Decimal
    intraday_buy_pct: Decimal


class CostsCfg(_Model):
    brokerage: BrokerageCfg
    stt: SttCfg
    exchange_txn_pct: Decimal
    sebi_fee_per_crore_inr: Decimal
    ipft_per_crore_inr: Decimal
    stamp_duty: StampCfg
    gst_pct: Decimal
    gst_applies_to: list[Literal["brokerage", "exchange_txn", "sebi_fee", "ipft", "dp"]]
    dp_charge_per_scrip_per_sell_day_inr: Decimal
    dp_on_btst_sell: bool


class SettlementCfg(_Model):
    cycle_days: int


# --------------------------------------------------------------------------- execution / output
class PaperCfg(_Model):
    slippage_ticks: int
    gtt_fill_model: Literal["limit_at_trigger", "market"]
    state_file: str


class LiveCfg(_Model):
    enabled: bool
    confirm_live_trading: bool
    static_ip_registered: bool
    max_orders_per_second: float
    entry_order_type: Literal["LIMIT", "MARKET"]
    entry_limit_buffer_ticks: int
    stop_order_type: Literal["SL-M", "SL"]
    stop_limit_buffer_pct: Decimal
    order_tag: str
    on_stop_failure: Literal["alert_and_flatten", "alert"]
    algo_header_name: str | None
    algo_header_value: str | None
    use_hft_endpoint: bool


class ExecutionCfg(_Model):
    mode: Literal["alerts", "paper", "live"]
    paper: PaperCfg
    live: LiveCfg


class TelegramCfg(_Model):
    enabled: bool
    bot_token_env: str
    chat_id_env: str


class AlertsCfg(_Model):
    console: bool
    file: str
    min_level: Literal["debug", "info", "warning", "critical"]
    telegram: TelegramCfg


class ChartsCfg(_Model):
    output_dir: str
    plotly_js: Literal["cdn", "inline"]
    daily_bars_shown: int
    weekly_bars_shown: int
    height_px: int


class JournalCfg(_Model):
    db_path: str


class Settings(_Model):
    account: AccountCfg
    universe: UniverseCfg
    data: DataCfg
    upstox: UpstoxCfg
    nse: NseCfg
    calendar: CalendarCfg
    sessions: SessionsCfg
    corporate_actions: CorporateActionsCfg
    ticks: TicksCfg
    price_bands: PriceBandsCfg
    surveillance: SurveillanceCfg
    screener: ScreenerCfg
    levels: LevelsCfg
    preopen: PreopenCfg
    intraday: IntradayCfg
    risk: RiskCfg
    targets: TargetsCfg
    trade_management: TradeManagementCfg
    costs: CostsCfg
    settlement: SettlementCfg
    execution: ExecutionCfg
    alerts: AlertsCfg
    charts: ChartsCfg
    journal: JournalCfg

    # ------------------------------------------------------------------ helpers
    def path(self, relative: str | Path) -> Path:
        """Resolve a path from the config relative to the project root."""
        p = Path(relative)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def live_orders_allowed(self) -> bool:
        """True only when every live switch is on. Paper/alerts never send orders."""
        live = self.execution.live
        return self.execution.mode == "live" and live.enabled and live.confirm_live_trading


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load_raw(path: str | Path | None = None) -> dict[str, Any]:
    """Read the YAML file as a plain nested dict (used by the docs test)."""
    p = Path(path) if path else DEFAULT_SETTINGS_PATH
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{p} does not contain a mapping")
    return data


def load_settings(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Settings:
    """Load and validate settings; ``overrides`` is deep-merged before validation."""
    raw = load_raw(path)
    if overrides:
        raw = _deep_merge(raw, overrides)
    return Settings.model_validate(raw)


def leaf_keys(data: dict[str, Any], prefix: str = "") -> list[str]:
    """Dotted paths of every leaf key (lists and empty mappings count as leaves).

    Mappings of user data (e.g. ``index_themes``) are listed as one leaf so the
    documentation test asks for the setting, not every theme name in it.
    """
    data_maps = {"index_keys", "index_themes", "manual_flags"}
    keys: list[str] = []
    for key, value in data.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict) and value and key not in data_maps:
            keys.extend(leaf_keys(value, dotted + "."))
        else:
            keys.append(dotted)
    return keys
