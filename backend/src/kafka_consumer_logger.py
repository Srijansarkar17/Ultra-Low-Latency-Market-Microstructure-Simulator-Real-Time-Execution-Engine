"""
kafka_consumer_logger.py
------------------------
CONSUMER 2 — ML Feature Logger (independent fan-out consumer)

Responsibilities:
  1. Subscribe to market.depth and market.trades with its OWN consumer group
     ("feature-logger-group"), giving it a completely independent read cursor.
  2. Reconstruct order book state locally (needed to compute imbalance and
     best bid/ask for the feature row).
  3. Every 1 second, write one feature row to features.csv:
       timestamp, mid, spread, spread_change, imbalance, trade_count

This service is the direct replacement for the logging_loop() coroutine that
previously ran inside the same process as the order book consumer.

Key property: because this consumer is in a different consumer group, it reads
ALL the same Kafka messages as Consumer 1 independently. You can kill or
restart this logger without affecting the order book / market maker at all.

Run:
    python kafka_consumer_logger.py

Topics consumed:
    market.depth   — to maintain a local book copy for imbalance calculation
    market.trades  — to count trades per second

Consumer group: feature-logger-group
"""

import time
from confluent_kafka import Consumer, KafkaError

from order_book_engine import OrderBookEngine
from serializers import decode_depth, decode_trade
from volatility_prediction_ml.feature_logger import FeatureLogger

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

KAFKA_CONF = {
    "bootstrap.servers": "localhost:9092",
    # CRITICAL: different group.id from Consumer 1 — this gives us an
    # independent offset cursor so we read ALL messages from the start,
    # not just the ones Consumer 1 hasn't processed yet.
    "group.id": "feature-logger-group",
    "auto.offset.reset": "latest",
    "enable.auto.commit": True,
    "auto.commit.interval.ms": 5000,
}

TOPICS  = ["market.depth", "market.trades"]
LOG_FILE = "features.csv"


# ---------------------------------------------------------------------------
# Imbalance helper
# ---------------------------------------------------------------------------

def compute_imbalance(book: OrderBookEngine, depth: int = 5) -> float:
    bids = sorted(book.bids.items(), reverse=True)[:depth]
    asks = sorted(book.asks.items())[:depth]
    bid_qty = sum(qty for _, qty in bids)
    ask_qty = sum(qty for _, qty in asks)
    if bid_qty + ask_qty == 0:
        return 0.0
    return (bid_qty - ask_qty) / (bid_qty + ask_qty)


# ---------------------------------------------------------------------------
# Main poll loop
# ---------------------------------------------------------------------------

def run_logger():
    consumer = Consumer(KAFKA_CONF)
    consumer.subscribe(TOPICS)

    # This consumer keeps its own lightweight order book replica just to
    # compute imbalance and best bid/ask — it does NOT run a market maker
    book = OrderBookEngine(symbol="BTCUSDT")
    book.load_snapshot()

    logger      = FeatureLogger(LOG_FILE)
    trade_count = 0
    last_mid    = None
    last_log_ts = time.time()

    print(f"[LOGGER CONSUMER] Subscribed — writing features to {LOG_FILE} …")

    try:
        while True:
            msg = consumer.poll(timeout=0.001)  # 1ms poll

            # -----------------------------------------------------------------
            # 1-second feature logging (wall-clock driven)
            # -----------------------------------------------------------------
            now = time.time()
            if now - last_log_ts >= 1.0:
                if book.synced:
                    bb = book.best_bid()
                    ba = book.best_ask()
                    if bb and ba:
                        mid    = (bb + ba) / 2.0
                        spread = ba - bb

                        # spread_change relative to last logged spread
                        spread_change = 0.0 if last_mid is None else (
                            spread - (ba - bb)
                        )

                        imbalance = compute_imbalance(book)
                        ts_us     = int(time.time() * 1_000_000)

                        logger.log(ts_us, mid, spread, spread_change,
                                   imbalance, trade_count)

                        print(
                            f"[LOGGER] ts={ts_us}  mid={mid:.3f}  "
                            f"spread={spread:.5f}  imbalance={imbalance:.4f}  "
                            f"trades={trade_count}  → logged to {LOG_FILE}"
                        )
                        last_mid = mid

                trade_count  = 0
                last_log_ts  = now

            # -----------------------------------------------------------------
            # No message in this poll window — loop back
            # -----------------------------------------------------------------
            if msg is None:
                continue

            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                print(f"[LOGGER CONSUMER][ERROR] {msg.error()}")
                continue

            topic = msg.topic()

            if topic == "market.depth":
                ev = decode_depth(msg.value())
                book.on_depth_diff(ev)          # keep local book in sync

            elif topic == "market.trades":
                decode_trade(msg.value())        # only need the count
                trade_count += 1

    except KeyboardInterrupt:
        print("\n[LOGGER CONSUMER] Shutting down …")
    finally:
        consumer.close()
        print("[LOGGER CONSUMER] Consumer closed cleanly.")


if __name__ == "__main__":
    run_logger()
