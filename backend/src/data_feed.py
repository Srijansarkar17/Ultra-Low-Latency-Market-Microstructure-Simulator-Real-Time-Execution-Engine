import asyncio #allows asynchronous code to run
import json
import time
import websockets
from market_handler import MarketDecoder, DepthDiff, Trade
from asyncio import Queue
from order_book_engine import OrderBookEngine


SYMBOL = "btcusdt"  # lowercase for WebSockets
WS_URL = f"wss://stream.binance.com:9443/stream?streams={SYMBOL}@depth@100ms/{SYMBOL}@trade&timeUnit=MICROSECOND"

# Consumer: Order Book Updater
async def book_consumer(q: Queue, book: OrderBookEngine): # This function reads events from the queue and updates the order book
    """
    Consumes decoded market events and updates the order book.
    """
    while True:
        ev = await q.get() # await q.get() → wait until new data arrives

        if isinstance(ev, DepthDiff):
            book.on_depth_diff(ev)

        elif isinstance(ev, Trade): # Trades do not change the book structure
            pass

#we create a single websocket that listens to two streams at once ,  we are getting both trade events and depth events in one socket
async def main(): # A coroutine that will run asynchronously (non-blocking).
    q: Queue = Queue(maxsize=10000) # This queue will store clean events (Trade or DepthDiff). Later, your order book or strategy will read from this queue. maxsize=10000 → protects you from memory exploding.

    decoder = MarketDecoder(expect_microseconds=True) #creates the decoder object, uses the class MarketDecoder from market_handler.py

    # Create order book engine
    book = OrderBookEngine(symbol="BTCUSDT")

    # IMPORTANT: snapshot before consuming diffs
    book.load_snapshot()

    # Start consumer task
    consumer_task = asyncio.create_task(book_consumer(q, book))

    async with websockets.connect(WS_URL, ping_interval=15, ping_timeout=10) as ws: # Opens the WebSocket connection to Binance. , pinginterval means sending an intenval every 15 seconds, ping_timeout means if Binance doesn't respond within 10 seconds, the connection closes.

        print(f"Successful Connection {WS_URL}") # prints if connection is successful
        while True: #Loop forever to continuously recieve incoming messages
            raw = await ws.recv() # Raw text frame, Asynchronously wait for the next message from Binance.This is the real-time data.

            ts_recv_us = int(time.time() * 1_000_000) # Capture the local time (in milliseconds) at the exact moment you received the message, Useful for latency calculations and logging.
            msg = json.loads(raw) # Convert the raw JSON string into a Python dict so you can access fields easily.

            ev = decoder.parse_combined(msg, ts_recv_us) #sends to the function parse_combined in market_handler.py under class MarketDecoder. uses the oject decoder created above

            if ev is None:
                continue  # Skip unknown / heartbeat / unexpected messages.

            await q.put(ev) # This hands off the event to the next stage (order book, strategy, logger, etc).

            if book.synced:
                bb = book.best_bid()
                ba = book.best_ask()
                sp = book.spread()
                print(
                    f"[BOOK] BB={bb} BA={ba} Spread={sp}"
                )


if __name__ == "__main__":
    asyncio.run(main())

            






