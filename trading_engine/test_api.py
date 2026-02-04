import logging
from kiteconnect import KiteConnect
import csv
import os
from datetime import datetime
from utils import read_lines_as_list, get_instrument_token, save_ohlcv_to_csv

INSTRUMENT_FILE_PATH = "futures_dec.txt"


logging.basicConfig(level=logging.DEBUG)
kite = KiteConnect(api_key="hh2qztxhmgzvzp9o")
# https://kite.zerodha.com/connect/login?v=3&api_key=hh2qztxhmgzvzp9o
data = kite.generate_session("hq5aMuyAWEWSwp8PTAIhfhmFsHoQ243z", api_secret="go73xe476rvueame58ppdk545f6h64vl")
kite.set_access_token(data["access_token"])

instrument_list = read_lines_as_list(INSTRUMENT_FILE_PATH)


for elements in instrument_list:
    
    instrument_id = get_instrument_token(elements)
    
    if instrument_id is None:
        pass
    
    else:

        candles = kite.historical_data(
                                    instrument_token=instrument_id,
                                    from_date="2026-01-01 09:00:00",
                                    to_date="2026-01-31 09:00:00",
                                    interval="60minute",
                                    continuous=False,
                                    oi=True
                                )
        
        print("I am here")

        save_ohlcv_to_csv(candles, instrument_id, elements, "here.csv")


# print(candles)





# save_ohlcv_to_csv(candles, instrument_id, instrument_name, "here.csv")

# Example:
# save_ohlcv_to_csv(output_list, instrument_id="ABC", instrument_number=123, csv_path="ohlcv.csv")



# curl https://kite.zerodha.com/connect/login?v=3&api_key=hh2qztxhmgzvzp9o

# D9U3Hnj0jPPqWk0aAEL6zdie7rd028Px


# curl "https://api.kite.trade/quote?i=NSE:INFY" \
#   -H "X-Kite-Version: 3" \
#   -H "Authorization: token api_key:D9U3Hnj0jPPqWk0aAEL6zdie7rd028Px"

# Place an order
# try:
#     order_id = kite.place_order(tradingsymbol="INFY",
#                                 exchange=kite.EXCHANGE_NSE,
#                                 transaction_type=kite.TRANSACTION_TYPE_BUY,
#                                 quantity=1,
#                                 variety=kite.VARIETY_AMO,
#                                 order_type=kite.ORDER_TYPE_MARKET,
#                                 product=kite.PRODUCT_CNC,
#                                 validity=kite.VALIDITY_DAY)

#     logging.info("Order placed. ID is: {}".format(order_id))
# except Exception as e:
#     logging.info("Order placement failed: {}".format(e.message))

# # Fetch all orders
# kite.orders()

# # Get instruments
# kite.instruments()

# # Place an mutual fund order
# kite.place_mf_order(
#     tradingsymbol="INF090I01239",
#     transaction_type=kite.TRANSACTION_TYPE_BUY,
#     amount=5000,
#     tag="mytag"
# )

# # Cancel a mutual fund order
# kite.cancel_mf_order(order_id="order_id")

# # Get mutual fund instruments
# kite.mf_instruments()