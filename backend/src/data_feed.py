import asyncio #allows asynchronous code to run
import json
import time
import websockets

SYMBOL = "btcusdt"  # lowercase for WebSockets
const WS_URL = `wss://stream.binance.com:9443/stream?streams=${SYMBOL}@depth@100ms/${SYMBOL}@trade`;
#we create a single websocket that listens to two streams at once ,  we are getting both trade events and depth events in one socket



