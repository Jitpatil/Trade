import csv
import requests
from io import StringIO

url = "https://api.kite.trade/instruments"

csv_data = requests.get(url, timeout=30).text

print(csv_data)

# print("STATUS:", r.status_code)
# print("FIRST 200 CHARS:", r.text[:200])

# reader = csv.DictReader(StringIO(r.text))
# print("FIELDNAMES:", reader.fieldnames)

# # print first parsed row (if any)
# for i, row in enumerate(reader):
#     print("ROW:", row)
#     break