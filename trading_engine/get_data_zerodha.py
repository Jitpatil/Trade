import os
import json
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple, Optional

import pandas as pd
from kiteconnect import KiteConnect
from kiteconnect.exceptions import TokenException, KiteException


# ----------------------------
# ENV vars required:
#   export KITE_API_KEY="xxxx"
#   export KITE_ACCESS_TOKEN="xxxx"
# ----------------------------
API_KEY = " hh2qztxhmgzvzp9o "
ACCESS_TOKEN = "iCaPnpdCfGu1X2ez9XNvfQqAy57ljoOJ"

if not API_KEY or not ACCESS_TOKEN:
    raise RuntimeError(
        "Missing env vars. Set BOTH:\n"
        "  export KITE_API_KEY='your_api_key'\n"
        "  export KITE_ACCESS_TOKEN='your_access_token'\n"
    )


def normalize_symbols(symbols: List[str], default_exchange: str = "NSE") -> List[str]:
    out = []
    for s in symbols:
        s = s.strip().upper()
        if not s:
            continue
        out.append(s if ":" in s else f"{default_exchange}:{s}")
    return out


def build_instrument_map(kite: KiteConnect) -> Dict[Tuple[str, str], int]:
    """
    Map (exchange, tradingsymbol) -> instrument_token
    """
    instruments = kite.instruments()
    m: Dict[Tuple[str, str], int] = {}
    for inst in instruments:
        ex = (inst.get("exchange") or "").upper()
        ts = (inst.get("tradingsymbol") or "").upper()
        tok = inst.get("instrument_token")
        if ex and ts and tok is not None:
            m[(ex, ts)] = int(tok)
    return m


def fetch_latest_1h_candle(kite: KiteConnect, instrument_token: int) -> Optional[dict]:
    """
    Get latest available 60-minute candle by querying a small recent window.
    """
    now = datetime.now(timezone.utc)
    from_dt = now - timedelta(hours=8)

    candles = kite.historical_data(
        instrument_token=instrument_token,
        from_date=from_dt,
        to_date=now,
        interval="60minute",
        continuous=False,
        oi=False,
    )

    if not candles:
        return None

    return candles[-1]  # latest candle only


def save_outputs(symbol_key: str, candle: Optional[dict], out_dir: str = "data") -> None:
    os.makedirs(out_dir, exist_ok=True)
    safe_name = symbol_key.replace(":", "_")

    json_path = os.path.join(out_dir, f"{safe_name}_1h.json")
    csv_path = os.path.join(out_dir, f"{safe_name}_1h.csv")

    payload = {
        "symbol": symbol_key,
        "interval": "60minute",
        "candle": candle,
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)

    if candle:
        df = pd.DataFrame([candle])
    else:
        df = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "oi"])

    df.to_csv(csv_path, index=False)


def main(symbols: List[str]) -> None:
    kite = KiteConnect(api_key=API_KEY)
    kite.set_access_token(ACCESS_TOKEN)

    # ---- AUTH CHECK (this is the important part) ----
    try:
        prof = kite.profile()  # will fail here if api_key/access_token mismatch or expired
        # print a safe confirmation (no secrets)
        print(f"[AUTH OK] user_id={prof.get('user_id')} api_key_endswith=...{API_KEY[-4:]}")
    except TokenException as e:
        raise SystemExit(
            "\n[AUTH FAIL] Incorrect `api_key` or `access_token`.\n"
            "Fix:\n"
            "  1) Ensure KITE_API_KEY is for the SAME app you used to login.\n"
            "  2) Generate a FRESH access_token today and export KITE_ACCESS_TOKEN.\n"
            "  3) Do not reuse old access_token from previous day.\n"
            f"\nDetails: {e}\n"
        )

    norm = normalize_symbols(symbols)
    inst_map = build_instrument_map(kite)

    for sym in norm:
        exchange, tradingsymbol = sym.split(":", 1)
        token = inst_map.get((exchange, tradingsymbol))

        if not token:
            print(f"[SKIP] Not found: {sym}")
            continue

        try:
            candle = fetch_latest_1h_candle(kite, token)
        except KiteException as e:
            print(f"[ERR] {sym}: {e}")
            continue

        if candle:
            print(f"[OK] {sym} latest 1h candle: {candle.get('date')} close={candle.get('close')}")
        else:
            print(f"[EMPTY] {sym}: no 1h candle returned")

        save_outputs(sym, candle)


if __name__ == "__main__":
    tickers = ["RELIANCE", "TCS", "NSE:INFY"]  # default exchange NSE if no prefix
    main(tickers)
