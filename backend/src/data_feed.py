import asyncio #allows asynchronous code to run
import json
import time
import websockets
from market_handler import MarketDecoder, DepthDiff, Trade
from asyncio import Queue

SYMBOL = "btcusdt"  # lowercase for WebSockets
WS_URL = f"wss://stream.binance.com:9443/stream?streams={SYMBOL}@depth@100ms/{SYMBOL}@trade&timeUnit=MICROSECOND"
#we create a single websocket that listens to two streams at once ,  we are getting both trade events and depth events in one socket
async def main(): # A coroutine that will run asynchronously (non-blocking).
    q: Queue = Queue(maxsize=10000) # This queue will store clean events (Trade or DepthDiff). Later, your order book or strategy will read from this queue. maxsize=10000 → protects you from memory exploding.

    decoder = MarketDecoder(expect_microseconds=True) #creates the decoder object, uses the class MarketDecoder from market_handler.py


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

            # OPTION B: quick demo — print, this prints the message
            if isinstance(ev, DepthDiff):
                # just show best fields
                print(f"[DEPTH] {ev.symbol} U..u={ev.U}..{ev.u} recv_us={ev.ts_recv_us}")
            elif isinstance(ev, Trade):
                print(f"[TRADE] {ev.symbol} {ev.price} x {ev.qty} taker={ev.taker_side}")


if __name__ == "__main__":
    asyncio.run(main())

            






