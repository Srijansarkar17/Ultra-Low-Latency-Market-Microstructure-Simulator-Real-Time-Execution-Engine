import asyncio #allows asynchronous code to run
import json
import time
import websockets

SYMBOL = "btcusdt"  # lowercase for WebSockets
WS_URL = f"wss://stream.binance.com:9443/stream?streams=${SYMBOL}@depth@100ms/${SYMBOL}@trade"
#we create a single websocket that listens to two streams at once ,  we are getting both trade events and depth events in one socket

async def recv_once(): # A coroutine that will run asynchronously (non-blocking).
    async with websockets.connect(WS_URL, ping_interval=15, ping_timeout=10) as ws: # Opens the WebSocket connection to Binance. , pinginterval means sending an intenval every 15 seconds, ping_timeout means if Binance doesn't respond within 10 seconds, the connection closes.

        print(f"Successful Connection {WS_URL}") # prints if connection is successful
        while True: #Loop forever to continuously recieve incoming messages
            raw = await ws.recv() # Raw text frame, Asynchronously wait for the next message from Binance.This is the real-time data.

            now_ms = int(time.time() * 1000) # Capture the local time (in milliseconds) at the exact moment you received the message, Useful for latency calculations and logging.
            msg = json.loads(raw) # Convert the raw JSON string into a Python dict so you can access fields easily.
            stream = msg.get("stream", "") # Extract the stream name (e.g., "btcusdt@depth@100ms" or "btcusdt@trade").
            # "btcusdt@depth@100ms" (order book updates), "btcusdt@trade" (trade updates)
            data = msg.get("data", {}) # Extract the payload part of the message (prices, qty, event time, etc.).


            evt_ms = int(data.get("E", now_ms)) # Binance includes a field "E" inside data → this is the timestamp (in milliseconds) when Binance created the event. 
            # data.get("E", now_ms) means: If "E" exists → use it. If "E" does NOT exist → use now_ms (so your code doesn’t break. So evt_ms = when Binance says the event happened.

            latency_ms = now_ms - evt_ms # now_ms = when YOU received the message, evt_ms = when BINANCE created the message, latency_ms = how many milliseconds the message took to reach you.

            out = {
                "ts_recv_ms": now_ms,
                "latency_ms": latency_ms,
                "stream": stream,
                "data": data
            }
            print(json.dumps(out, separators=(",", ":")))

async def main():
    # simple reconnect loop
    backoff = 1
    while True:
        try:
            await recv_once()
        except Exception as e:
            print(f"# disconnected: {e}; retrying in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

if __name__ == "__main__":
    asyncio.run(main())

            






