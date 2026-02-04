import csv
import requests
from io import StringIO

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
