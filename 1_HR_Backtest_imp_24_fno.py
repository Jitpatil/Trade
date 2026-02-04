from __future__ import annotations

# NOTE: This version adds a DAILY trend gate for HOURLY entries:
# - Daily EMAs (EMA20_D/EMA50_D) are computed from the daily close derived from your 1H data.
# - Hourly Entry_Exec signals are allowed ONLY when EMA20_D > EMA50_D (DailyTrend == BULL).


import warnings
warnings.filterwarnings("ignore")

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text


# ============================================================
# CONFIG
# ============================================================

SERVER = r"DESKTOP-5PSSH78\SQLEXPRESS"
DATABASE = "trade"
SRC_SCHEMA = "dbo"
SRC_TABLE = "all_fno_1HR"

# Optional date filter (recommended if table is huge)
START_DATE = "2025-01-01"      # e.g. "2024-01-01"  or None
END_DATE   = None      # e.g. "2026-01-09"  or None

OUT_DIR = Path(r"C:\00\fno\1HR\result")
OUT_DIR.mkdir(parents=True, exist_ok=True)

OUT_A_L = OUT_DIR / "all_fno_1H_A-L.csv"
OUT_M_Z = OUT_DIR / "all_fno_1H_M-Z.csv"
OUT_BACKTEST_XLSX = OUT_DIR / "rotation_backtest_results_fno.xlsx"
OUT_SUGGESTIONS_CSV = OUT_DIR / "suggestions_latest_1H_fno.csv"

# Indicator params
RSI_PERIOD = 14
DI_PERIOD = 14
MOM_BARS = 20

# Backtest (rotation)
START_EQUITY = 10000.0
MAX_POSITIONS = 8
MAX_POS_PCT = 0.25

BASE_TARGET_EXPOSURE = 0.70
MAX_TARGET_EXPOSURE = 0.95

RISK_STRONG = 0.01
RISK_VSTRONG = 0.015
ABS_MAX_RISK = 0.02

ATR_PERIOD = 20
SWING_LOW_LOOKBACK = 10
TRAIL_LOW_LOOKBACK = 6
ATR_STOP_MULT = 1.5
TRAIL_ATR_MULT = 1.0
TIME_STOP_BARS = 35

# Entry refinement (kept same)
MIN_RSI = 55
MIN_VOL_PCTCHG_1D = 50
MIN_MOMENTUM = 0

# Option-2 Liquidity filter (20-bar SMA of 1H volume)
USE_VOL_SMA20_FILTER = True
MIN_VOL_SMA20 = 50000   # adjust for your market/data (shares/contracts)

# Backtest entry quality gate
MIN_ENTRY_TRADE_SCORE = 99

USE_WEAKENING_EXIT = True
WEAKENING_BARS = 2
TOP_CANDIDATES_MULT = 3

# On-screen suggestions
SUGGEST_TOP_N = 20

# Suggestion sizing (recommended qty/position)
SUGGEST_ACCOUNT_EQUITY = 10000.0
SUGGEST_MAX_POS_PCT = 0.25
SUGGEST_RISK_STRONG = 0.01
SUGGEST_RISK_VSTRONG = 0.015


# ============================================================
# SQLAlchemy engine
# ============================================================

def make_sql_engine(server: str, database: str):
    drivers = [
        "ODBC+Driver+18+for+SQL+Server",
        "ODBC+Driver+17+for+SQL+Server",
    ]
    last_err = None
    for drv in drivers:
        try:
            conn_str = (
                "mssql+pyodbc://@"
                + server
                + "/"
                + database
                + "?driver="
                + drv
                + "&trusted_connection=yes"
                + "&TrustServerCertificate=yes"
            )
            engine = create_engine(conn_str, fast_executemany=True)
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return engine
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Could not create SQL engine. Last error: {last_err}")


def load_1h(engine) -> pd.DataFrame:
    where = []
    params = {}

    # Validate source table exists (friendly error if not)
    if not table_exists(engine, SRC_SCHEMA, SRC_TABLE):
        suggestions = suggest_tables(engine, pattern='IN')
        msg = (
            f"Invalid object name '{SRC_SCHEMA}.{SRC_TABLE}'.\n"
            f"Check DATABASE/SRC_SCHEMA/SRC_TABLE.\n"
            f"Tables that look relevant (sample): {', '.join(suggestions[:20]) if suggestions else 'None found'}"
        )
        raise RuntimeError(msg)

    if START_DATE:
        where.append("[Date] >= :start_date")
        params["start_date"] = START_DATE
    if END_DATE:
        where.append("[Date] <= :end_date")
        params["end_date"] = END_DATE
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
    SELECT
        ticker,
        [Date],
        [Open],
        High,
        Low,
        [Close],
        Adj_Close,
        Volume
    FROM [{SRC_SCHEMA}].[{SRC_TABLE}]
    {where_sql}
    ORDER BY ticker, [Date];
    """
    with engine.connect() as conn:
        df = pd.read_sql(text(sql), conn, params=params)

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    for c in ["Open", "High", "Low", "Close", "Adj_Close", "Volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["ticker", "Date", "Adj_Close"]).copy()
    df = df.sort_values(["ticker", "Date"]).reset_index(drop=True)
    df.replace([np.inf, -np.inf], np.nan, inplace=True)

    # ✅ HARD DEDUPE (this removes the duplicates you asked for)
    before = len(df)
    df = df.drop_duplicates(subset=["ticker", "Date"], keep="last").copy()
    dropped = before - len(df)
    if dropped > 0:
        print(f"✅ Dropped duplicates from SQL load: {dropped:,} rows (same ticker+Date)")

    return df



# ============================================================
# SQL helpers (table existence + suggestions)
# ============================================================

def table_exists(engine, schema: str, table: str) -> bool:
    sql = text("""
        SELECT 1
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = :schema AND TABLE_NAME = :table
    """)
    with engine.connect() as conn:
        r = conn.execute(sql, {"schema": schema, "table": table}).fetchone()
    return r is not None

def suggest_tables(engine, pattern: str = "IN", limit: int = 50) -> list[str]:
    sql = text("""
        SELECT TOP (:limit) TABLE_SCHEMA, TABLE_NAME
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE='BASE TABLE'
          AND (TABLE_NAME LIKE :p1 OR TABLE_NAME LIKE :p2 OR TABLE_NAME LIKE :p3)
        ORDER BY TABLE_SCHEMA, TABLE_NAME
    """)
    like1 = f"%{pattern}%"
    like2 = "%1HR%"
    like3 = "%1H%"
    with engine.connect() as conn:
        rows = conn.execute(sql, {"limit": limit, "p1": like1, "p2": like2, "p3": like3}).fetchall()
    return [f"{s}.{t}" for s, t in rows]

# ============================================================
# Helpers
# ============================================================

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def safe_float(x) -> float:
    try:
        if pd.isna(x):
            return float("nan")
        return float(x)
    except Exception:
        return float("nan")

def strength_upper(x) -> str:
    return str(x).strip().upper()

def suggestion_risk_pct(s: str) -> float:
    s = strength_upper(s)
    if s == "VERY STRONG":
        return SUGGEST_RISK_VSTRONG
    return SUGGEST_RISK_STRONG

def rsi_wilder(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder-style RSI using EWM alpha=1/period (same style as RSI14)."""
    s = pd.to_numeric(series, errors="coerce").astype(float)
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-12)
    return 100 - (100 / (1 + rs))


# ============================================================
# INDICATORS
# ============================================================

def add_indicators_one_ticker(g: pd.DataFrame) -> pd.DataFrame:
    # ✅ ensure per-ticker unique timestamps before any merges/rollings
    g = g.sort_values("Date").drop_duplicates(subset=["Date"], keep="last").copy()

    adj = g["Adj_Close"].astype(float)
    high = g["High"].astype(float)
    low = g["Low"].astype(float)
    close = g["Close"].astype(float)
    vol = g["Volume"].astype(float)

    # Volume % changes
    g["Volume_PctChg_1H"] = vol.pct_change() * 100.0
    # Option-2 liquidity metric: 20-bar SMA of hourly volume
    g["Vol_SMA20"] = vol.rolling(20, min_periods=20).mean()
    g["_hour"] = g["Date"].dt.hour.astype("int16")
    g["PrevDaySameHour_Vol"] = g.groupby("_hour")["Volume"].shift(1)
    g["Volume_PctChg_1D"] = np.where(
        (g["PrevDaySameHour_Vol"].isna()) | (g["PrevDaySameHour_Vol"] == 0),
        np.nan,
        (g["Volume"] - g["PrevDaySameHour_Vol"]) * 100.0 / g["PrevDaySameHour_Vol"],
    )

    # EMAs (Adj_Close)
    g["EMA5"] = adj.ewm(span=5, adjust=False).mean()
    g["EMA9"] = adj.ewm(span=9, adjust=False).mean()
    g["EMA20"] = adj.ewm(span=20, adjust=False).mean()
    g["EMA50"] = adj.ewm(span=50, adjust=False).mean()
    g["EMA150"] = adj.ewm(span=150, adjust=False).mean()

    # Crosses
    g["CrossUp_20_50"] = ((g["EMA20"] > g["EMA50"]) & (g["EMA20"].shift(1) <= g["EMA50"].shift(1))).astype("int8")
    g["CrossDn_20_50"] = ((g["EMA20"] < g["EMA50"]) & (g["EMA20"].shift(1) >= g["EMA50"].shift(1))).astype("int8")
    g["CrossUp_20_150"] = ((g["EMA20"] > g["EMA150"]) & (g["EMA20"].shift(1) <= g["EMA150"].shift(1))).astype("int8")
    g["CrossDn_20_150"] = ((g["EMA20"] < g["EMA150"]) & (g["EMA20"].shift(1) >= g["EMA150"].shift(1))).astype("int8")

    # Momentum
    g["Momentum_20H_Pct"] = adj.pct_change(MOM_BARS) * 100.0

    # RSI14 (1H)
    g["RSI14"] = rsi_wilder(adj, RSI_PERIOD)

    # RSI14_4H and RSI14_D (computed on 4H/D closes, mapped back to 1H rows)
    g["Date_4H"] = g["Date"].dt.floor("4H")
    close_4h = g.groupby("Date_4H", sort=False)["Adj_Close"].last()
    rsi_4h = rsi_wilder(close_4h, RSI_PERIOD).rename("RSI14_4H")
    g = g.merge(rsi_4h, left_on="Date_4H", right_index=True, how="left")

    g["Date_D"] = g["Date"].dt.floor("D")
    close_d = g.groupby("Date_D", sort=False)["Adj_Close"].last()
    rsi_d = rsi_wilder(close_d, RSI_PERIOD).rename("RSI14_D")
    g = g.merge(rsi_d, left_on="Date_D", right_index=True, how="left")

    # Daily EMA20/EMA50 (NEW for daily direction filter)
    ema20_d = close_d.ewm(span=20, adjust=False).mean().rename("EMA20_D")
    ema50_d = close_d.ewm(span=50, adjust=False).mean().rename("EMA50_D")
    g = g.merge(ema20_d, left_on="Date_D", right_index=True, how="left")
    g = g.merge(ema50_d, left_on="Date_D", right_index=True, how="left")

    # Daily trend direction (NEW): BULL if EMA20_D > EMA50_D else BEAR
    g["DailyTrend"] = np.where(g["EMA20_D"] > g["EMA50_D"], "BULL", "BEAR")

    # DI+ / DI-
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = np.maximum.reduce([
        (high - low).values,
        (high - close.shift(1)).abs().values,
        (low - close.shift(1)).abs().values
    ])
    atr_w = pd.Series(tr, index=g.index).ewm(alpha=1 / DI_PERIOD, adjust=False).mean()
    g["DI_Plus"] = 100 * pd.Series(plus_dm, index=g.index).ewm(alpha=1 / DI_PERIOD, adjust=False).mean() / (atr_w + 1e-12)
    g["DI_Minus"] = 100 * pd.Series(minus_dm, index=g.index).ewm(alpha=1 / DI_PERIOD, adjust=False).mean() / (atr_w + 1e-12)

    # “Run candidate” (reference)
    g["Run_Candidate"] = (
        (g["Momentum_20H_Pct"] >= 30) &
        (g["Volume_PctChg_1D"] >= 100) &
        (g["EMA20"] > g["EMA50"])
    ).astype("int8")

    # Backtest helpers (ATR20 simple + swing/trail lows)
    g["TR_calc"] = tr
    g["ATR20_calc"] = pd.Series(tr, index=g.index).rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()
    g["SwingLow10"] = low.rolling(SWING_LOW_LOOKBACK, min_periods=SWING_LOW_LOOKBACK).min()
    g["Low6"] = low.rolling(TRAIL_LOW_LOOKBACK, min_periods=TRAIL_LOW_LOOKBACK).min()

    return g.drop(columns=["_hour", "PrevDaySameHour_Vol", "Date_4H", "Date_D"], errors="ignore")


# ============================================================
# SCORE + STRENGTH
# ============================================================

def trade_score_row(r) -> float:
    must = [r.EMA5, r.EMA9, r.EMA20, r.EMA50, r.EMA150, r.RSI14, r.DI_Plus, r.DI_Minus]
    if any(pd.isna(x) for x in must):
        return np.nan

    score = 0.0
    score += 25.0 if r.EMA20 > r.EMA50 else -25.0
    score += 20.0 if r.EMA20 > r.EMA150 else -20.0
    score += 10.0 if r.EMA5 > r.EMA9 else -10.0
    score += 10.0 if r.EMA9 > r.EMA20 else -10.0

    score += max(-15.0, min(15.0, (r.RSI14 - 50.0) * 0.6))

    di_diff = r.DI_Plus - r.DI_Minus
    score += max(-20.0, min(20.0, di_diff * 0.8))

    if not pd.isna(r.Momentum_20H_Pct):
        score += max(-15.0, min(15.0, r.Momentum_20H_Pct * 0.3))

    return float(clamp(score, -100.0, 100.0))


def score_label(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("Date").copy()
    g["TRADE_SCORE"] = [trade_score_row(r) for r in g.itertuples(index=False)]

    score = g["TRADE_SCORE"]
    prev = score.shift(1)

    label = np.where(
        score.isna(),
        "HOLD",
        np.where(
            score >= 70, "VERY STRONG",
            np.where(score >= 40, "STRONG",
                     np.where(score >= 10, "HOLD", "WEAK"))
        )
    )

    weakening = (score >= 20) & prev.notna() & ((score - prev) <= -10)
    label = np.where(weakening, "WEAKENING", label)

    g["TRADE_STRENGTH"] = label
    return g


# ============================================================
# Trade action + commentary (kept same)
# ============================================================

def add_trade_logic(g: pd.DataFrame) -> pd.DataFrame:
    actions = []
    comments = []

    for r in g.itertuples(index=False):
        trend_150_bull = "EMA20 > EMA150" if r.EMA20 > r.EMA150 else "EMA20 ≤ EMA150"
        trend_150_bear = "EMA20 < EMA150" if r.EMA20 < r.EMA150 else "EMA20 ≥ EMA150"

        if r.EMA20 > r.EMA50 and r.RSI14 > 55 and r.DI_Plus > r.DI_Minus:
            actions.append("BUY")
            comments.append(
                f"Bullish trend: EMA20 > EMA50, {trend_150_bull}, "
                f"RSI14={r.RSI14:.1f} (>55), DI+({r.DI_Plus:.1f}) > DI-({r.DI_Minus:.1f})"
            )
        elif r.EMA20 < r.EMA50 and r.RSI14 < 45 and r.DI_Minus > r.DI_Plus:
            actions.append("SELL")
            comments.append(
                f"Bearish trend: EMA20 < EMA50, {trend_150_bear}, "
                f"RSI14={r.RSI14:.1f} (<45), DI-({r.DI_Minus:.1f}) > DI+({r.DI_Plus:.1f})"
            )
        else:
            trend_50 = "EMA20 > EMA50" if r.EMA20 > r.EMA50 else "EMA20 < EMA50"
            trend_150 = "EMA20 > EMA150" if r.EMA20 > r.EMA150 else "EMA20 < EMA150"
            actions.append("HOLD")
            comments.append(
                f"No clear edge: {trend_50}, {trend_150}, RSI14={r.RSI14:.1f}, "
                f"DI+={r.DI_Plus:.1f}, DI-={r.DI_Minus:.1f}"
            )

    g = g.copy()
    g["Trade_Action"] = actions
    g["Trade_Commentary"] = comments
    return g


# ============================================================
# Entry/Exit exec flags (kept same)
# ============================================================

def add_entry_exit_flags(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("Date").copy()

    strength = g["TRADE_STRENGTH"].astype(str).str.upper()
    action = g["Trade_Action"].astype(str).str.upper()

    # ✅ Hourly entries only in the DAILY direction (EMA20_D > EMA50_D)
    # Daily EMAs are derived from the daily close built from 1H data per ticker.
    daily_bull = (g["EMA20_D"] > g["EMA50_D"])

    entry_cond = (
        (action == "BUY") &
        (strength.isin(["STRONG", "VERY STRONG"])) &
        (g["EMA5"] > g["EMA9"]) & (g["EMA9"] > g["EMA20"]) &
        (g["EMA20"] > g["EMA50"]) & (g["EMA20"] > g["EMA150"]) &
        (g["RSI14"] >= MIN_RSI) &
        (g["DI_Plus"] > g["DI_Minus"]) &
        (g["Momentum_20H_Pct"] > MIN_MOMENTUM) &
        (g["Volume_PctChg_1D"].fillna(0) >= MIN_VOL_PCTCHG_1D)
        & ((not USE_VOL_SMA20_FILTER) | (g["Vol_SMA20"] >= MIN_VOL_SMA20))
        & daily_bull
    )

    weak2 = False
    if USE_WEAKENING_EXIT:
        weak = (strength == "WEAKENING").astype(int)
        weak2 = weak.rolling(WEAKENING_BARS, min_periods=WEAKENING_BARS).sum() >= WEAKENING_BARS

    exit_cond = (
        (g["EMA5"] < g["EMA9"]) |
        (g["Adj_Close"] < g["EMA20"]) |
        (g["DI_Plus"] < g["DI_Minus"])
    )
    if USE_WEAKENING_EXIT:
        exit_cond = exit_cond | weak2

    g["Entry_Exec"] = entry_cond.shift(1).fillna(False)
    g["Exit_Exec"] = exit_cond.shift(1).fillna(False)
    return g


# ============================================================
# Suggestions (same)
# ============================================================

def initial_stop(entry: float, atr20: float, swing: float) -> float:
    stop_atr = entry - ATR_STOP_MULT * atr20 if not pd.isna(atr20) else entry * 0.95
    stop_swing = swing if not pd.isna(swing) else stop_atr
    return float(max(stop_atr, stop_swing))

def suggest_latest(out: pd.DataFrame, top_n: int = 20) -> pd.DataFrame:
    if out.empty:
        return pd.DataFrame()
    latest_ts = pd.to_datetime(out["Date"]).max()
    sl = out[out["Date"] == latest_ts].copy()

    sl = sl[
        (sl["Trade_Action"].astype(str).str.upper() == "BUY") &
        (sl["TRADE_STRENGTH"].astype(str).str.upper().isin(["STRONG", "VERY STRONG"]))
    ].copy()

    if sl.empty:
        return pd.DataFrame()

    sl["DI_Diff"] = sl["DI_Plus"] - sl["DI_Minus"]
    sl["Recommended_Entry"] = sl["Open"]

    rec_stop, rec_qty, rec_pos, rec_risk = [], [], [], []

    for r in sl.itertuples(index=False):
        entry = safe_float(getattr(r, "Recommended_Entry"))
        atr20 = safe_float(getattr(r, "ATR20_calc", np.nan))
        swing = safe_float(getattr(r, "SwingLow10", np.nan))
        stop = initial_stop(entry, atr20, swing)
        rps = entry - stop

        risk_pct = suggestion_risk_pct(getattr(r, "TRADE_STRENGTH", "STRONG"))
        risk_amt = SUGGEST_ACCOUNT_EQUITY * risk_pct

        max_pos_dollars = SUGGEST_ACCOUNT_EQUITY * SUGGEST_MAX_POS_PCT

        if pd.isna(entry) or entry <= 0 or pd.isna(rps) or rps <= 0:
            qty = 0
        else:
            qty_risk = int(math.floor(risk_amt / rps))
            qty_cap = int(math.floor(max_pos_dollars / entry))
            qty = max(0, min(qty_risk, qty_cap))

        rec_stop.append(stop)
        rec_qty.append(qty)
        rec_pos.append(qty * entry if qty > 0 else 0.0)
        rec_risk.append(risk_amt)

    sl["Recommended_Stop"] = rec_stop
    sl["Risk_$"] = rec_risk
    sl["Recommended_Qty"] = rec_qty
    sl["Recommended_Position_$"] = rec_pos

    sl = sl.sort_values(["TRADE_SCORE", "DI_Diff", "Volume_PctChg_1D", "Momentum_20H_Pct"],
                        ascending=False, na_position="last")

    cols = [
        "ticker","Date","Adj_Close",
        "TRADE_SCORE","TRADE_STRENGTH",
        "Trade_Action","Trade_Commentary",
        "EMA5","EMA9","EMA20","EMA50","EMA150",
        "RSI14","RSI14_4H","RSI14_D","EMA20_D","EMA50_D","DailyTrend",
        "DI_Plus","DI_Minus","DI_Diff",
        "Momentum_20H_Pct","Volume_PctChg_1D",
        "Recommended_Entry","Recommended_Stop","Risk_$","Recommended_Qty","Recommended_Position_$"
    ]
    cols = [c for c in cols if c in sl.columns]
    return sl.head(top_n)[cols].copy()


# ============================================================
# Rotation backtest (unchanged)
# ============================================================

@dataclass
class Position:
    ticker: str
    entry_time: pd.Timestamp
    entry_price: float
    qty: int
    stop: float
    r_per_share: float
    bars_held: int
    last_close: float
    trade_score: float
    trade_strength: str
    trade_commentary_entry: str


def exposure_target_from_quality(cands: pd.DataFrame) -> float:
    if cands.empty:
        return BASE_TARGET_EXPOSURE
    top = cands.head(MAX_POSITIONS)
    scores = pd.to_numeric(top["TRADE_SCORE"], errors="coerce")
    if scores.notna().sum() == 0:
        strengths = top["TRADE_STRENGTH"].astype(str).str.upper()
        v = (strengths == "VERY STRONG").mean()
        raw = BASE_TARGET_EXPOSURE + 0.20 * v
        return clamp(raw, BASE_TARGET_EXPOSURE, MAX_TARGET_EXPOSURE)
    q = float(scores.dropna().mean())
    raw = BASE_TARGET_EXPOSURE + (clamp(q, 0, 100) / 100.0) * (MAX_TARGET_EXPOSURE - BASE_TARGET_EXPOSURE)
    return clamp(raw, BASE_TARGET_EXPOSURE, MAX_TARGET_EXPOSURE)


def risk_pct_for_strength(s: str) -> float:
    s = strength_upper(s)
    if s == "VERY STRONG":
        return min(RISK_VSTRONG, ABS_MAX_RISK)
    return min(RISK_STRONG, ABS_MAX_RISK)


def update_trailing_stop(pos: Position, row: pd.Series) -> None:
    c = safe_float(row["Adj_Close"])
    if pos.r_per_share <= 0:
        return
    r_mult = (c - pos.entry_price) / pos.r_per_share
    if r_mult < 1.0:
        return
    cand = []
    atr20 = safe_float(row.get("ATR20_calc", np.nan))
    ema20 = safe_float(row.get("EMA20", np.nan))
    if not pd.isna(atr20) and not pd.isna(ema20):
        cand.append(ema20 - TRAIL_ATR_MULT * atr20)
    low6 = safe_float(row.get("Low6", np.nan))
    if not pd.isna(low6):
        cand.append(low6)
    if cand:
        pos.stop = max(pos.stop, float(max(cand)))


def backtest(out: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    df_time = out.sort_values(["Date","ticker"]).reset_index(drop=True)
    timestamps = np.sort(df_time["Date"].unique())

    cash = START_EQUITY
    positions: Dict[str, Position] = {}
    equity_rows: List[dict] = []
    trades: List[dict] = []

    def equity() -> float:
        e = cash
        for p in positions.values():
            e += p.qty * p.last_close
        return float(e)

    for ts in timestamps:
        sl = df_time[df_time["Date"] == ts]

        # mark-to-market
        for _, row in sl.iterrows():
            t = row["ticker"]
            if t in positions:
                positions[t].last_close = safe_float(row["Adj_Close"])

        # exits
        for _, row in sl.iterrows():
            t = row["ticker"]
            if t not in positions:
                continue

            pos = positions[t]
            pos.bars_held += 1

            o = safe_float(row["Open"])
            l = safe_float(row["Low"])

            exit_reason = None
            exit_price = None

            if not pd.isna(l) and l <= pos.stop:
                exit_reason = "STOP_LOSS"
                exit_price = pos.stop

            if exit_reason is None and pos.bars_held >= TIME_STOP_BARS:
                exit_reason = "TIME_STOP"
                exit_price = o

            if exit_reason is None and bool(row.get("Exit_Exec", False)):
                exit_reason = "SIGNAL_EXIT"
                exit_price = o

            if exit_reason is None:
                update_trailing_stop(pos, row)

            if exit_reason is not None:
                qty = pos.qty
                cash += qty * exit_price
                pnl = qty * (exit_price - pos.entry_price)
                r_mult = (exit_price - pos.entry_price) / pos.r_per_share if pos.r_per_share > 0 else np.nan

                comm_exit = str(row.get("Trade_Commentary", ""))

                trades.append({
                    "ticker": t,
                    "entry_time": pos.entry_time,
                    "exit_time": ts,
                    "entry_price": pos.entry_price,
                    "exit_price": float(exit_price),
                    "qty": qty,
                    "position_value_entry": qty * pos.entry_price,
                    "Position_Risk_$": qty * pos.r_per_share,
                    "RR_at_exit": r_mult,
                    "Profit_$": pnl,
                    "bars_held": pos.bars_held,
                    "exit_reason": exit_reason,
                    "stop_at_exit": pos.stop,
                    "TRADE_SCORE_at_entry": pos.trade_score,
                    "TRADE_STRENGTH_at_entry": pos.trade_strength,
                    "Trade_Commentary_at_entry": pos.trade_commentary_entry,
                    "Trade_Commentary_at_exit": comm_exit,
                })
                del positions[t]

        # candidates
        cands = sl[(sl["Entry_Exec"] == True) & (pd.to_numeric(sl["TRADE_SCORE"], errors="coerce") > MIN_ENTRY_TRADE_SCORE)].copy()
        if positions:
            cands = cands[~cands["ticker"].isin(list(positions.keys()))]

        cands["StrengthRank"] = cands["TRADE_STRENGTH"].astype(str).str.upper().map({"VERY STRONG": 2, "STRONG": 1}).fillna(0)
        cands = cands.sort_values(["TRADE_SCORE","StrengthRank","Volume_PctChg_1D","Momentum_20H_Pct"],
                                  ascending=[False,False,False,False], na_position="last")

        eq_now = equity()
        target = exposure_target_from_quality(cands)
        current_exposure = 1.0 - (cash / eq_now if eq_now > 0 else 1.0)

        slots = MAX_POSITIONS - len(positions)
        if slots > 0 and cash > 0 and (current_exposure + 1e-9) < target and not cands.empty:
            desired_investment = target * eq_now
            current_investment = eq_now - cash
            additional_needed = max(0.0, desired_investment - current_investment)
            deploy_budget = min(cash, additional_needed)

            pool = cands.head(max(1, slots * TOP_CANDIDATES_MULT)).copy()

            w = pd.to_numeric(pool["TRADE_SCORE"], errors="coerce").clip(lower=0).fillna(0)
            if w.sum() <= 0:
                w = pd.Series(1.0, index=pool.index)
            w = w / w.sum()

            opened = 0
            for idx, row in pool.iterrows():
                if opened >= slots:
                    break
                t = row["ticker"]
                entry = safe_float(row["Open"])
                if pd.isna(entry) or entry <= 0:
                    continue

                alloc_dollars = float(deploy_budget * w.loc[idx])
                alloc_dollars = min(alloc_dollars, eq_now * MAX_POS_PCT)

                atr20 = safe_float(row.get("ATR20_calc", np.nan))
                swing = safe_float(row.get("SwingLow10", np.nan))
                stop = initial_stop(entry, atr20, swing)
                rps = entry - stop
                if rps <= 0:
                    continue

                strength = strength_upper(row.get("TRADE_STRENGTH", ""))
                risk_amt = eq_now * risk_pct_for_strength(strength)

                qty_alloc = int(math.floor(alloc_dollars / entry))
                qty_risk = int(math.floor(risk_amt / rps))
                qty_cash = int(math.floor(cash / entry))
                qty = min(qty_alloc, qty_risk, qty_cash)
                if qty <= 0:
                    continue

                cash -= qty * entry
                positions[t] = Position(
                    ticker=t,
                    entry_time=ts,
                    entry_price=float(entry),
                    qty=int(qty),
                    stop=float(stop),
                    r_per_share=float(rps),
                    bars_held=0,
                    last_close=safe_float(row["Adj_Close"]),
                    trade_score=safe_float(row.get("TRADE_SCORE", np.nan)),
                    trade_strength=strength,
                    trade_commentary_entry=str(row.get("Trade_Commentary", "")),
                )
                opened += 1
                if cash <= 0:
                    break

        eq_now2 = equity()
        exposure_now = 1.0 - (cash / eq_now2 if eq_now2 > 0 else 1.0)
        equity_rows.append({
            "Date": ts,
            "Equity": eq_now2,
            "Cash": cash,
            "OpenPositions": len(positions),
            "Exposure": exposure_now,
            "TargetExposure": target
        })

    eq_df = pd.DataFrame(equity_rows)
    trades_df = pd.DataFrame(trades)

    eq_df["Peak"] = eq_df["Equity"].cummax()
    eq_df["DrawdownPct"] = (eq_df["Equity"] - eq_df["Peak"]) / eq_df["Peak"] * 100

    summary_df = build_summary(eq_df, trades_df)
    return summary_df, trades_df, eq_df


def build_summary(eq_df: pd.DataFrame, trades_df: pd.DataFrame) -> pd.DataFrame:
    start = START_EQUITY
    end = float(eq_df["Equity"].iloc[-1]) if not eq_df.empty else start
    net_profit = end - start
    net_growth_pct = (end / start - 1) * 100 if start > 0 else np.nan
    max_dd = float(eq_df["DrawdownPct"].min()) if not eq_df.empty else np.nan

    if trades_df.empty:
        trades_n = 0
        win_rate = np.nan
        pf = np.nan
    else:
        trades_n = int(len(trades_df))
        wins = trades_df[trades_df["Profit_$"] > 0]
        losses = trades_df[trades_df["Profit_$"] <= 0]
        win_rate = (len(wins) / trades_n) * 100 if trades_n else np.nan
        loss_sum = float(abs(losses["Profit_$"].sum())) if len(losses) else 0.0
        pf = float(wins["Profit_$"].sum() / loss_sum) if loss_sum > 0 else np.nan

    avg_expo = float(eq_df["Exposure"].mean()) * 100 if not eq_df.empty else np.nan
    min_expo = float(eq_df["Exposure"].min()) * 100 if not eq_df.empty else np.nan
    max_expo = float(eq_df["Exposure"].max()) * 100 if not eq_df.empty else np.nan

    return pd.DataFrame([{
        "Start Equity": start,
        "End Equity": round(end, 2),
        "Net Profit ($)": round(net_profit, 2),
        "Net Growth (%)": round(net_growth_pct, 2),
        "Trades": trades_n,
        "Accuracy / Win Rate (%)": round(win_rate, 2) if not pd.isna(win_rate) else np.nan,
        "Profit Factor": round(pf, 3) if not pd.isna(pf) else np.nan,
        "Max Drawdown (%)": round(max_dd, 2) if not pd.isna(max_dd) else np.nan,
        "Max Open Positions (observed)": int(eq_df["OpenPositions"].max()) if not eq_df.empty else 0,
        "Avg Exposure (%)": round(avg_expo, 2),
        "Min Exposure (%)": round(min_expo, 2),
        "Max Exposure (%)": round(max_expo, 2),
    }])


# ============================================================
# MAIN
# ============================================================

def main():
    engine = make_sql_engine(SERVER, DATABASE)

    print("Loading SQL data...")
    df = load_1h(engine)
    print(f"Loaded rows: {len(df):,} | tickers: {df['ticker'].nunique():,}")
    print(f"Range: {df['Date'].min()} -> {df['Date'].max()}")

    print("\nBuilding indicators + trade logic...")
    out = (
        df.groupby("ticker", group_keys=False, sort=False)
          .apply(add_indicators_one_ticker)
          .reset_index(drop=True)
    )

    # ✅ FINAL DEDUPE SAFETY NET (in case anything slipped in during transforms)
    before = len(out)
    out = out.drop_duplicates(subset=["ticker","Date"], keep="last").copy()
    dropped = before - len(out)
    if dropped > 0:
        print(f"✅ Dropped duplicates after indicators: {dropped:,} rows (same ticker+Date)")

    out = add_trade_logic(out)
    out = out.groupby("ticker", group_keys=False, sort=False).apply(score_label).reset_index(drop=True)
    out = out.groupby("ticker", group_keys=False, sort=False).apply(add_entry_exit_flags).reset_index(drop=True)

    # Column order
    base = [
        "ticker","Date","Open","High","Low","Close","Adj_Close",
        "Volume","Volume_PctChg_1H","Volume_PctChg_1D",
        "EMA5","EMA9","EMA20","EMA50","EMA150",
        "Momentum_20H_Pct","RSI14","RSI14_4H","RSI14_D","EMA20_D","EMA50_D","DailyTrend","DI_Plus","DI_Minus",
        "TRADE_SCORE","TRADE_STRENGTH",
        "Trade_Action","Trade_Commentary",
        "Run_Candidate",
        "CrossUp_20_50","CrossDn_20_50","CrossUp_20_150","CrossDn_20_150",
        "TR_calc","ATR20_calc","SwingLow10","Low6",
        "Entry_Exec","Exit_Exec"
    ]
    rest = [c for c in out.columns if c not in base]
    out = out[base + rest]

    # Write two CSVs A–L / M–Z
    first = out["ticker"].astype(str).str[0].str.upper()
    al_mask = first.between("A", "L", inclusive="both")
    out.loc[al_mask].to_csv(OUT_A_L, index=False)
    out.loc[~al_mask].to_csv(OUT_M_Z, index=False)
    print(f"\n✅ Wrote: {OUT_A_L}")
    print(f"✅ Wrote: {OUT_M_Z}")

    # Suggestions block (screen + file)
    print("\n================ 1H SUGGESTIONS (LATEST BAR) ================")
    sugg = suggest_latest(out, top_n=SUGGEST_TOP_N)
    if sugg.empty:
        print("No STRONG / VERY STRONG BUY candidates on the latest hour.")
    else:
        print(sugg.to_string(index=False))
        sugg.to_csv(OUT_SUGGESTIONS_CSV, index=False)
        print(f"\n✅ Wrote: {OUT_SUGGESTIONS_CSV}")

    # Backtest
    print("\nRunning rotation backtest...")
    summary_df, trades_df, eq_df = backtest(out)

    with pd.ExcelWriter(OUT_BACKTEST_XLSX, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        trades_df.to_excel(writer, sheet_name="Trades", index=False)
        eq_df.to_excel(writer, sheet_name="EquityCurve", index=False)

    print("\n================ BACKTEST SUMMARY ================")
    print(summary_df.to_string(index=False))
    print(f"\n✅ Backtest saved: {OUT_BACKTEST_XLSX}")


if __name__ == "__main__":
    main()
