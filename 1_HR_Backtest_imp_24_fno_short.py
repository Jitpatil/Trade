from __future__ import annotations

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
# CONFIG (keep same style)
# ============================================================

SERVER = r"DESKTOP-5PSSH78\SQLEXPRESS"
DATABASE = "trade"
SRC_SCHEMA = "dbo"
SRC_TABLE = "all_fno_1HR"

START_DATE = "2025-01-01"
END_DATE   = None

OUT_DIR = Path(r"C:\00\fno\1HR\result")
OUT_DIR.mkdir(parents=True, exist_ok=True)

OUT_A_L = OUT_DIR / "all_fno_1H_SHORT_A-L.csv"
OUT_M_Z = OUT_DIR / "all_fno_1H_SHORT_M-Z.csv"
OUT_BACKTEST_XLSX = OUT_DIR / "rotation_backtest_results_fno_SHORT.xlsx"
OUT_SUGGESTIONS_CSV = OUT_DIR / "suggestions_latest_1H_fno_SHORT.csv"

# Indicators
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
SWING_LOOKBACK = 10
TRAIL_LOOKBACK = 6
ATR_STOP_MULT = 1.5
TRAIL_ATR_MULT = 1.0
TIME_STOP_BARS = 35

# Entry refinement (mirror of BUY thresholds)
MIN_RSI = 55                 # SHORT condition uses RSI <= (100-MIN_RSI) => <=45
MIN_VOL_PCTCHG_1D = 50
MIN_MOMENTUM = 0

USE_VOL_SMA20_FILTER = True
MIN_VOL_SMA20 = 50000

# Quality gate (start less strict than 99 for shorts)
MIN_ENTRY_BEAR_SCORE = 97

USE_WEAKENING_EXIT = True
WEAKENING_BARS = 2
TOP_CANDIDATES_MULT = 3

SUGGEST_TOP_N = 20

SUGGEST_ACCOUNT_EQUITY = 10000.0
SUGGEST_MAX_POS_PCT = 0.25
SUGGEST_RISK_STRONG = 0.01
SUGGEST_RISK_VSTRONG = 0.015


# ============================================================
# SQLAlchemy engine (same approach)
# ============================================================

def make_sql_engine(server: str, database: str):
    drivers = ["ODBC+Driver+18+for+SQL+Server", "ODBC+Driver+17+for+SQL+Server"]
    last_err = None
    for drv in drivers:
        try:
            conn_str = (
                "mssql+pyodbc://@" + server + "/" + database +
                "?driver=" + drv +
                "&trusted_connection=yes&TrustServerCertificate=yes"
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

    before = len(df)
    df = df.drop_duplicates(subset=["ticker", "Date"], keep="last").copy()
    dropped = before - len(df)
    if dropped > 0:
        print(f"✅ Dropped duplicates from SQL load: {dropped:,} rows (same ticker+Date)")
    return df


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
    s = pd.to_numeric(series, errors="coerce").astype(float)
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-12)
    return 100 - (100 / (1 + rs))


# ============================================================
# INDICATORS (same as buy script style)
# ============================================================

def add_indicators_one_ticker(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("Date").drop_duplicates(subset=["Date"], keep="last").copy()

    adj = g["Adj_Close"].astype(float)
    high = g["High"].astype(float)
    low = g["Low"].astype(float)
    close = g["Close"].astype(float)
    vol = g["Volume"].astype(float)

    g["Volume_PctChg_1H"] = vol.pct_change() * 100.0
    g["Vol_SMA20"] = vol.rolling(20, min_periods=20).mean()

    g["_hour"] = g["Date"].dt.hour.astype("int16")
    g["PrevDaySameHour_Vol"] = g.groupby("_hour")["Volume"].shift(1)
    g["Volume_PctChg_1D"] = np.where(
        (g["PrevDaySameHour_Vol"].isna()) | (g["PrevDaySameHour_Vol"] == 0),
        np.nan,
        (g["Volume"] - g["PrevDaySameHour_Vol"]) * 100.0 / g["PrevDaySameHour_Vol"],
    )

    g["EMA5"] = adj.ewm(span=5, adjust=False).mean()
    g["EMA9"] = adj.ewm(span=9, adjust=False).mean()
    g["EMA20"] = adj.ewm(span=20, adjust=False).mean()
    g["EMA50"] = adj.ewm(span=50, adjust=False).mean()
    g["EMA150"] = adj.ewm(span=150, adjust=False).mean()

    g["Momentum_20H_Pct"] = adj.pct_change(MOM_BARS) * 100.0
    g["RSI14"] = rsi_wilder(adj, RSI_PERIOD)

    # RSI 4H / D mapped back
    g["Date_4H"] = g["Date"].dt.floor("4H")
    close_4h = g.groupby("Date_4H", sort=False)["Adj_Close"].last()
    rsi_4h = rsi_wilder(close_4h, RSI_PERIOD).rename("RSI14_4H")
    g = g.merge(rsi_4h, left_on="Date_4H", right_index=True, how="left")

    g["Date_D"] = g["Date"].dt.floor("D")
    close_d = g.groupby("Date_D", sort=False)["Adj_Close"].last()
    rsi_d = rsi_wilder(close_d, RSI_PERIOD).rename("RSI14_D")
    g = g.merge(rsi_d, left_on="Date_D", right_index=True, how="left")

    # Daily EMA gate
    ema20_d = close_d.ewm(span=20, adjust=False).mean().rename("EMA20_D")
    ema50_d = close_d.ewm(span=50, adjust=False).mean().rename("EMA50_D")
    g = g.merge(ema20_d, left_on="Date_D", right_index=True, how="left")
    g = g.merge(ema50_d, left_on="Date_D", right_index=True, how="left")
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

    # ATR + swing/trail for shorts
    g["TR_calc"] = tr
    g["ATR20_calc"] = pd.Series(tr, index=g.index).rolling(ATR_PERIOD, min_periods=ATR_PERIOD).mean()
    g["SwingHigh10"] = high.rolling(SWING_LOOKBACK, min_periods=SWING_LOOKBACK).max()
    g["High6"] = high.rolling(TRAIL_LOOKBACK, min_periods=TRAIL_LOOKBACK).max()

    return g.drop(columns=["_hour", "PrevDaySameHour_Vol", "Date_4H", "Date_D"], errors="ignore")


# ============================================================
# SCORE + BEAR SCORE
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

def label_from_score(score: pd.Series) -> pd.Series:
    return np.where(
        score.isna(),
        "HOLD",
        np.where(score >= 70, "VERY STRONG",
                 np.where(score >= 40, "STRONG",
                          np.where(score >= 10, "HOLD", "WEAK")))
    )

def score_label_with_bear(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("Date").copy()
    g["TRADE_SCORE"] = [trade_score_row(r) for r in g.itertuples(index=False)]
    g["TRADE_STRENGTH"] = label_from_score(g["TRADE_SCORE"])

    g["BEAR_SCORE"] = -pd.to_numeric(g["TRADE_SCORE"], errors="coerce")
    g["BEAR_STRENGTH"] = label_from_score(g["BEAR_SCORE"])
    return g


# ============================================================
# Trade action + commentary (same style)
# ============================================================

def add_trade_logic(g: pd.DataFrame) -> pd.DataFrame:
    actions = []
    comments = []

    for r in g.itertuples(index=False):
        if r.EMA20 > r.EMA50 and r.RSI14 > 55 and r.DI_Plus > r.DI_Minus:
            actions.append("BUY")
            comments.append("Bullish trend (BUY)")
        elif r.EMA20 < r.EMA50 and r.RSI14 < 45 and r.DI_Minus > r.DI_Plus:
            actions.append("SELL")
            comments.append("Bearish trend (SELL)")
        else:
            actions.append("HOLD")
            comments.append("No clear edge")

    g = g.copy()
    g["Trade_Action"] = actions
    g["Trade_Commentary"] = comments
    return g


# ============================================================
# SHORT Entry/Exit exec flags (true mirror)
# ============================================================

def add_entry_exit_flags_short_only(g: pd.DataFrame) -> pd.DataFrame:
    g = g.sort_values("Date").copy()

    action = g["Trade_Action"].astype(str).str.upper()
    bear_strength = g["BEAR_STRENGTH"].astype(str).str.upper()

    daily_bear = (g["EMA20_D"] < g["EMA50_D"])

    entry_short = (
        (action == "SELL") &
        (bear_strength.isin(["STRONG", "VERY STRONG"])) &
        (g["EMA5"] < g["EMA9"]) & (g["EMA9"] < g["EMA20"]) &
        (g["EMA20"] < g["EMA50"]) & (g["EMA20"] < g["EMA150"]) &
        (g["RSI14"] <= (100 - MIN_RSI)) &
        (g["DI_Minus"] > g["DI_Plus"]) &
        (g["Momentum_20H_Pct"] < -MIN_MOMENTUM) &
        (g["Volume_PctChg_1D"].fillna(0) >= MIN_VOL_PCTCHG_1D) &
        ((not USE_VOL_SMA20_FILTER) | (g["Vol_SMA20"] >= MIN_VOL_SMA20)) &
        daily_bear
    )

    # Mirror exit
    exit_short = (
        (g["EMA5"] > g["EMA9"]) |
        (g["Adj_Close"] > g["EMA20"]) |
        (g["DI_Minus"] < g["DI_Plus"])
    )

    g["Entry_Exec"] = entry_short.shift(1).fillna(False)
    g["Exit_Exec"] = exit_short.shift(1).fillna(False)
    return g


# ============================================================
# SHORT stop + trailing stop (mirror)
# ============================================================

def initial_stop_short(entry: float, atr20: float, swing_high: float) -> float:
    stop_atr = entry + ATR_STOP_MULT * atr20 if not pd.isna(atr20) else entry * 1.05
    stop_swing = swing_high if not pd.isna(swing_high) else stop_atr
    return float(min(stop_atr, stop_swing))

def update_trailing_stop_short(entry: float, r_per_share: float, current_stop: float, row: pd.Series) -> float:
    c = safe_float(row["Adj_Close"])
    if r_per_share <= 0:
        return current_stop
    r_mult = (entry - c) / r_per_share
    if r_mult < 1.0:
        return current_stop

    cand = []
    atr20 = safe_float(row.get("ATR20_calc", np.nan))
    ema20 = safe_float(row.get("EMA20", np.nan))
    if not pd.isna(atr20) and not pd.isna(ema20):
        cand.append(ema20 + TRAIL_ATR_MULT * atr20)

    high6 = safe_float(row.get("High6", np.nan))
    if not pd.isna(high6):
        cand.append(high6)

    if cand:
        return float(min(current_stop, min(cand)))
    return current_stop


# ============================================================
# Suggestions (short)
# ============================================================

def suggest_latest_short_only(out: pd.DataFrame, top_n: int = 20) -> pd.DataFrame:
    if out.empty:
        return pd.DataFrame()

    latest_ts = pd.to_datetime(out["Date"]).max()
    sl = out[out["Date"] == latest_ts].copy()

    sl = sl[
        (sl["Trade_Action"].astype(str).str.upper() == "SELL") &
        (sl["BEAR_STRENGTH"].astype(str).str.upper().isin(["STRONG", "VERY STRONG"]))
    ].copy()

    if sl.empty:
        return pd.DataFrame()

    sl["Side"] = "SHORT"
    sl["Recommended_Entry"] = sl["Open"]

    rec_stop, rec_qty, rec_pos = [], [], []
    for r in sl.itertuples(index=False):
        entry = safe_float(getattr(r, "Recommended_Entry"))
        atr20 = safe_float(getattr(r, "ATR20_calc", np.nan))
        swing_h = safe_float(getattr(r, "SwingHigh10", np.nan))
        stop = initial_stop_short(entry, atr20, swing_h)
        rps = stop - entry

        risk_pct = suggestion_risk_pct(getattr(r, "BEAR_STRENGTH", "STRONG"))
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

    sl["Recommended_Stop"] = rec_stop
    sl["Recommended_Qty"] = rec_qty
    sl["Recommended_Position_$"] = rec_pos

    sl = sl.sort_values(["BEAR_SCORE", "Volume_PctChg_1D", "Momentum_20H_Pct"], ascending=False, na_position="last")

    cols = [
        "ticker","Date","Side","Adj_Close",
        "BEAR_SCORE","BEAR_STRENGTH",
        "Trade_Action","Trade_Commentary",
        "EMA5","EMA9","EMA20","EMA50","EMA150",
        "RSI14","RSI14_4H","RSI14_D","EMA20_D","EMA50_D","DailyTrend",
        "DI_Plus","DI_Minus",
        "Momentum_20H_Pct","Volume_PctChg_1D",
        "Recommended_Entry","Recommended_Stop","Recommended_Qty","Recommended_Position_$"
    ]
    cols = [c for c in cols if c in sl.columns]
    return sl.head(top_n)[cols].copy()


# ============================================================
# ROTATION BACKTEST (SHORT accounting fixed)
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
    bear_score: float
    bear_strength: str
    trade_commentary_entry: str

def risk_pct_for_strength(s: str) -> float:
    s = strength_upper(s)
    if s == "VERY STRONG":
        return min(RISK_VSTRONG, ABS_MAX_RISK)
    return min(RISK_STRONG, ABS_MAX_RISK)

def backtest_short_only(out: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    df_time = out.sort_values(["Date", "ticker"]).reset_index(drop=True)
    timestamps = np.sort(df_time["Date"].unique())

    cash = START_EQUITY
    positions: Dict[str, Position] = {}
    equity_rows: List[dict] = []
    trades: List[dict] = []

    def gross_short_value(sl_rows: pd.DataFrame | None = None) -> float:
        # use last_close stored in positions (already updated each bar)
        return float(sum(p.qty * p.last_close for p in positions.values()))

    def equity() -> float:
        # ✅ correct short equity model
        return float(cash - gross_short_value())

    for ts in timestamps:
        sl = df_time[df_time["Date"] == ts]

        # mark-to-market update last_close
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
            h = safe_float(row["High"])

            exit_reason = None
            exit_price = None

            # stop for shorts: if High >= stop
            if not pd.isna(h) and h >= pos.stop:
                exit_reason = "STOP_LOSS"
                exit_price = pos.stop

            if exit_reason is None and pos.bars_held >= TIME_STOP_BARS:
                exit_reason = "TIME_STOP"
                exit_price = o

            if exit_reason is None and bool(row.get("Exit_Exec", False)):
                exit_reason = "SIGNAL_EXIT"
                exit_price = o

            if exit_reason is None:
                pos.stop = update_trailing_stop_short(pos.entry_price, pos.r_per_share, pos.stop, row)

            if exit_reason is not None:
                qty = pos.qty

                # ✅ cover: cash decreases by cover cost
                cash -= qty * exit_price

                pnl = qty * (pos.entry_price - exit_price)
                r_mult = (pos.entry_price - exit_price) / pos.r_per_share if pos.r_per_share > 0 else np.nan

                trades.append({
                    "ticker": t,
                    "side": "SHORT",
                    "entry_time": pos.entry_time,
                    "exit_time": ts,
                    "entry_price": pos.entry_price,
                    "exit_price": float(exit_price),
                    "qty": qty,
                    "short_value_entry": qty * pos.entry_price,
                    "RR_at_exit": r_mult,
                    "Profit_$": pnl,
                    "bars_held": pos.bars_held,
                    "exit_reason": exit_reason,
                    "stop_at_exit": pos.stop,
                    "BEAR_SCORE_at_entry": pos.bear_score,
                    "BEAR_STRENGTH_at_entry": pos.bear_strength,
                    "Trade_Commentary_at_entry": pos.trade_commentary_entry,
                    "Trade_Commentary_at_exit": str(row.get("Trade_Commentary", "")),
                })
                del positions[t]

        # entries
        cands = sl[
            (sl["Entry_Exec"] == True) &
            (pd.to_numeric(sl["BEAR_SCORE"], errors="coerce") >= MIN_ENTRY_BEAR_SCORE)
        ].copy()

        if positions:
            cands = cands[~cands["ticker"].isin(list(positions.keys()))]

        cands["StrengthRank"] = cands["BEAR_STRENGTH"].astype(str).str.upper().map({"VERY STRONG": 2, "STRONG": 1}).fillna(0)
        cands = cands.sort_values(["BEAR_SCORE","StrengthRank","Volume_PctChg_1D","Momentum_20H_Pct"],
                                  ascending=[False,False,False,False], na_position="last")

        eq_now = equity()
        if eq_now <= 0:
            # avoid blowups
            eq_now = 1e-6

        # exposure = gross short / equity
        gross_now = gross_short_value()
        current_exposure = gross_now / eq_now

        # target exposure based on candidate quality (simple)
        target = BASE_TARGET_EXPOSURE
        if not cands.empty:
            top_scores = pd.to_numeric(cands.head(MAX_POSITIONS)["BEAR_SCORE"], errors="coerce").dropna()
            if len(top_scores):
                q = float(top_scores.mean())
                target = clamp(BASE_TARGET_EXPOSURE + (clamp(q, 0, 100)/100.0)*(MAX_TARGET_EXPOSURE-BASE_TARGET_EXPOSURE),
                               BASE_TARGET_EXPOSURE, MAX_TARGET_EXPOSURE)

        slots = MAX_POSITIONS - len(positions)
        if slots > 0 and current_exposure < target and not cands.empty:
            pool = cands.head(max(1, slots * TOP_CANDIDATES_MULT)).copy()

            w = pd.to_numeric(pool["BEAR_SCORE"], errors="coerce").clip(lower=0).fillna(0)
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

                atr20 = safe_float(row.get("ATR20_calc", np.nan))
                swing_h = safe_float(row.get("SwingHigh10", np.nan))
                stop = initial_stop_short(entry, atr20, swing_h)
                rps = stop - entry
                if rps <= 0:
                    continue

                strength = strength_upper(row.get("BEAR_STRENGTH", ""))
                risk_amt = eq_now * risk_pct_for_strength(strength)

                # sizing like BUY, but constrained by equity exposure (no fake “cash”)
                alloc_dollars = float(eq_now * MAX_POS_PCT)  # per-position cap
                alloc_dollars = min(alloc_dollars, float(eq_now * target * w.loc[idx]))

                qty_alloc = int(math.floor(alloc_dollars / entry))
                qty_risk = int(math.floor(risk_amt / rps))
                qty = min(qty_alloc, qty_risk)
                if qty <= 0:
                    continue

                # check exposure limit before opening
                new_gross = gross_short_value() + qty * entry
                if (new_gross / eq_now) > MAX_TARGET_EXPOSURE:
                    continue

                # ✅ short sell proceeds increase cash
                cash += qty * entry

                positions[t] = Position(
                    ticker=t,
                    entry_time=ts,
                    entry_price=float(entry),
                    qty=int(qty),
                    stop=float(stop),
                    r_per_share=float(rps),
                    bars_held=0,
                    last_close=safe_float(row["Adj_Close"]),
                    bear_score=safe_float(row.get("BEAR_SCORE", np.nan)),
                    bear_strength=strength,
                    trade_commentary_entry=str(row.get("Trade_Commentary", "")),
                )
                opened += 1

        # record equity curve
        eq_now2 = equity()
        if eq_now2 <= 0:
            eq_now2 = 1e-6
        gross2 = gross_short_value()
        exposure_now = gross2 / eq_now2

        equity_rows.append({
            "Date": ts,
            "Equity": eq_now2,
            "Cash": cash,
            "GrossShort": gross2,
            "OpenPositions": len(positions),
            "Exposure": exposure_now,
            "TargetExposure": target
        })

    eq_df = pd.DataFrame(equity_rows)
    trades_df = pd.DataFrame(trades)

    if not eq_df.empty:
        eq_df["Peak"] = eq_df["Equity"].cummax()
        eq_df["DrawdownPct"] = (eq_df["Equity"] - eq_df["Peak"]) / eq_df["Peak"] * 100

    summary_df = build_summary(eq_df, trades_df)
    return summary_df, trades_df, eq_df


def build_summary(eq_df: pd.DataFrame, trades_df: pd.DataFrame) -> pd.DataFrame:
    start = START_EQUITY
    end = float(eq_df["Equity"].iloc[-1]) if not eq_df.empty else start
    net_profit = end - start
    net_growth_pct = (end / start - 1) * 100 if start > 0 else np.nan
    max_dd = float(eq_df["DrawdownPct"].min()) if (not eq_df.empty and "DrawdownPct" in eq_df.columns) else np.nan

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

    # safety dedupe
    out = out.drop_duplicates(subset=["ticker", "Date"], keep="last").copy()

    out = add_trade_logic(out)
    out = out.groupby("ticker", group_keys=False, sort=False).apply(score_label_with_bear).reset_index(drop=True)
    out = out.groupby("ticker", group_keys=False, sort=False).apply(add_entry_exit_flags_short_only).reset_index(drop=True)

    # Save split A–L / M–Z
    first = out["ticker"].astype(str).str[0].str.upper()
    al_mask = first.between("A", "L", inclusive="both")
    out.loc[al_mask].to_csv(OUT_A_L, index=False)
    out.loc[~al_mask].to_csv(OUT_M_Z, index=False)
    print(f"\n✅ Wrote: {OUT_A_L}")
    print(f"✅ Wrote: {OUT_M_Z}")

    # Suggestions
    print("\n================ 1H SHORT SUGGESTIONS (LATEST BAR) ================")
    sugg = suggest_latest_short_only(out, top_n=SUGGEST_TOP_N)
    if sugg.empty:
        print("No STRONG / VERY STRONG SELL candidates on the latest hour.")
    else:
        print(sugg.to_string(index=False))
        sugg.to_csv(OUT_SUGGESTIONS_CSV, index=False)
        print(f"\n✅ Wrote: {OUT_SUGGESTIONS_CSV}")

    # Backtest
    print("\nRunning SHORT rotation backtest...")
    summary_df, trades_df, eq_df = backtest_short_only(out)

    with pd.ExcelWriter(OUT_BACKTEST_XLSX, engine="openpyxl") as writer:
        summary_df.to_excel(writer, sheet_name="Summary", index=False)
        trades_df.to_excel(writer, sheet_name="Trades", index=False)
        eq_df.to_excel(writer, sheet_name="EquityCurve", index=False)

    print("\n================ SHORT BACKTEST SUMMARY ================")
    print(summary_df.to_string(index=False))
    print(f"\n✅ Backtest saved: {OUT_BACKTEST_XLSX}")


if __name__ == "__main__":
    main()
