"""
kafka_producer.py
-----------------
PRODUCER SERVICE — the entry point for live market data.

Responsibilities:
  1. Open a persistent WebSocket connection to Binance combined streams.
  2. Decode every frame into a typed DepthDiff or Trade event (via MarketDecoder).
  3. Serialise the event to JSON bytes (via serializers.encode_event).
  4. Publish to the appropriate Kafka topic:
       DepthDiff  →  market.depth
       Trade      →  market.trades

This service knows NOTHING about the order book, market maker, or ML logger.
It is a pure ingest pipeline. Consumers are fully decoupled — you can add,
remove, or restart them without touching this file.

Run:
    python kafka_producer.py

Topics produced to:
    market.depth   — DepthDiff events (L2 order-book diffs at 100ms cadence)
    market.trades  — Trade events    (individual matched trades)
"""

import asyncio
import json
import time
import websockets
from confluent_kafka import Producer

from market_handler import MarketDecoder, DepthDiff, Trade
from serializers import encode_event

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SYMBOL = "btcusdt"
WS_URL = (
    f"wss://stream.binance.com:9443/stream"
    f"?streams={SYMBOL}@depth@100ms/{SYMBOL}@trade"
    f"&timeUnit=MICROSECOND"
)

KAFKA_CONF = {
    "bootstrap.servers": "localhost:9092",
    # Compression saves ~60% bandwidth at negligible CPU cost for JSON payloads
    "compression.type": "lz4",
    # Batch up to 5ms of messages before sending — reduces syscalls, still
    # well within our sub-200ms end-to-end latency budget
    "linger.ms": 5,
    # In-flight queue: 100k messages before we block. At ~200 msgs/s this is
    # ~500 seconds of back-pressure headroom
    "queue.buffering.max.messages": 100000,
}

TOPIC_DEPTH  = "market.depth"
TOPIC_TRADES = "market.trades"


# ---------------------------------------------------------------------------
# Delivery callback (called by librdkafka in producer.poll())
# ---------------------------------------------------------------------------

def _on_delivery(err, msg):
    """
    Log failed deliveries. Successful deliveries are silent to avoid I/O
    overhead on the hot path — we publish ~200 messages/second.
    """
    if err:
        print(f"[PRODUCER][ERROR] Delivery failed for {msg.topic()}: {err}")


# ---------------------------------------------------------------------------
# Main async loop
# ---------------------------------------------------------------------------

async def run_producer():
    producer = Producer(KAFKA_CONF)
    decoder  = MarketDecoder(expect_microseconds=True)

    print(f"[PRODUCER] Connecting to {WS_URL}")

    while True:  # outer reconnect loop
        try:
            async with websockets.connect(
                WS_URL, ping_interval=15, ping_timeout=10
            ) as ws:
                print("[PRODUCER] WebSocket connected — streaming to Kafka …")

                async for raw in ws:
                    ts_recv_us = int(time.time() * 1_000_000)
                    msg = json.loads(raw)
                    ev  = decoder.parse_combined(msg, ts_recv_us)

                    if ev is None:
                        continue  # heartbeat / unknown stream type

                    payload = encode_event(ev)

                    if isinstance(ev, DepthDiff):
                        producer.produce(
                            TOPIC_DEPTH,
                            key=ev.symbol.encode(),
                            value=payload,
                            on_delivery=_on_delivery,
                        )
                    elif isinstance(ev, Trade):
                        producer.produce(
                            TOPIC_TRADES,
                            key=ev.symbol.encode(),
                            value=payload,
                            on_delivery=_on_delivery,
                        )

                    # poll(0) = non-blocking: triggers delivery callbacks and
                    # flushes the internal send buffer without adding latency
                    producer.poll(0)

        except (websockets.ConnectionClosed, OSError) as exc:
            print(f"[PRODUCER] WebSocket disconnected ({exc}), reconnecting in 2s …")
            producer.flush()          # drain any buffered messages before reconnect
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(run_producer())
