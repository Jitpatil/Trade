from datetime import datetime

def get_safe_historical_data(
    kite,
    instrument_token,
    from_date,
    to_date,
    interval,
    continuous=False,
    oi=False,
):
    empty_candle = {
        "date": None,
        "open": None,
        "high": None,
        "low": None,
        "close": None,
        "volume": None,
        "oi": None,
    }

    try:
        candles = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval=interval,
            continuous=continuous,
            oi=oi,
        )

        # Zerodha sometimes returns []
        if not candles:
            return [empty_candle]

        return candles

    except Exception:
        return [empty_candle]
