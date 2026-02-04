import csv
import requests
from io import StringIO
from datetime import datetime
import os


def read_lines_as_list(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f]


def get_instrument_token(tradingsymbol, exchange="NSE"):
    url = "https://api.kite.trade/instruments"
    csv_data = requests.get(url, timeout=30).text

    reader = csv.DictReader(StringIO(csv_data))
    for row in reader:
        if (
            row["tradingsymbol"] == tradingsymbol
            and row["exchange"] == exchange
        ):
            return int(row["instrument_token"])

    return None


def save_ohlcv_to_csv(data, instrument_id, instrument_number, csv_path):
    """
    data: either a dict or a list of dicts like:
      {'date': datetime(...), 'open':..., 'high':..., 'low':..., 'close':..., 'volume':...}
    Writes/append rows to csv with columns:
      instrument_id, instrument_number, timestamp, open, high, low, close, volume
    """

    # Normalize to a list of dicts
    if isinstance(data, dict):
        data = [data]
    elif not isinstance(data, list):
        raise TypeError("data must be a dict or a list of dicts")

    fieldnames = [
        "instrument_id",
        "instrument_number",
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
    ]

    # Sort by time (optional but nice)
    def _get_dt(item):
        dt = item.get("date")
        if isinstance(dt, datetime):
            return dt
        raise ValueError("Each item must have a 'date' as a datetime object")

    data = sorted(data, key=_get_dt)

    file_exists = os.path.isfile(csv_path)
    write_header = (not file_exists) or (os.path.getsize(csv_path) == 0)

    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if write_header:
            writer.writeheader()

        for item in data:
            dt = item["date"]  # datetime with tzinfo is fine
            writer.writerow({
                "instrument_id": instrument_id,
                "instrument_number": instrument_number,
                "timestamp": dt.isoformat(),  # keeps timezone offset
                "open": item.get("open"),
                "high": item.get("high"),
                "low": item.get("low"),
                "close": item.get("close"),
                "volume": item.get("volume"),
            })