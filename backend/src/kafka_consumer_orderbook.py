"""
kafka_consumer_orderbook.py
---------------------------
CONSUMER 1 — Order Book + Market Maker

Responsibilities:
  1. Subscribe to market.depth and market.trades Kafka topics.
  2. Apply DepthDiff events to the live OrderBookEngine (with snapshot-sync
     and gap-detection logic preserved from the original data_feed.py).
  3. Fire MarketMaker.on_book_update() on every depth tick.
  4. Fire MarketMaker.on_trade() on every trade event to simulate fills.
  5. Print a 1-second status bar (inventory, PnL, spread, imbalance).

This process is fully independent of the logger consumer. If this process
crashes and restarts, Kafka replays from the last committed offset, so the
order book will re-sync from a fresh REST snapshot automatically.

Run:
    python kafka_consumer_orderbook.py

Topics consumed:
    market.depth   — DepthDiff events
    market.trades  — Trade events

Consumer group: orderbook-engine-group
  Changing this group.id gives you an independent read cursor — another
  consumer group can read the same topics from the beginning independently.
"""

import time
from confluent_kafka import Consumer, KafkaError

from order_book_engine import OrderBookEngine
from market_maker import MarketMaker
from serializers import decode_depth, decode_trade

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

KAFKA_CONF = {
    "bootstrap.servers": "localhost:9092",
    "group.id": "orderbook-engine-group",
    # Start from the latest offset so we don't replay old stale tick data
    # into the live order book on first start
    "auto.offset.reset": "latest",
    # Commit offsets every 5 seconds automatically so we can resume from
    # roughly the right point if this process is killed unexpectedly
    "enable.auto.commit": True,
    "auto.commit.interval.ms": 5000,
}

TOPICS = ["market.depth", "market.trades"]


# ---------------------------------------------------------------------------
# Imbalance helper (same logic as original data_feed.py)
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

def run_consumer():
    consumer = Consumer(KAFKA_CONF)
    consumer.subscribe(TOPICS)

    # Bootstrap order book with a REST snapshot
    book = OrderBookEngine(symbol="BTCUSDT")
    book.load_snapshot()
    maker = MarketMaker(book)

    print("[OB CONSUMER] Subscribed — polling market.depth + market.trades …")

    # 1-second status bar state
    last_log_ts   = time.time()
    trade_count   = 0
    last_mid      = None

    try:
        while True:
            # poll() blocks for at most 1ms — keeps end-to-end latency low
            msg = consumer.poll(timeout=0.001)

            if msg is None:
                # No message arrived in this 1ms window — check if it's time
                # to print the 1-second status bar
                _maybe_log(book, maker, last_log_ts, trade_count, last_mid,
                           lambda new_ts, new_tc, new_mid: None)
                now = time.time()
                if now - last_log_ts >= 1.0:
                    if book.synced:
                        bb = book.best_bid()
                        ba = book.best_ask()
                        if bb and ba:
                            mid       = (bb + ba) / 2.0
                            spread    = ba - bb
                            imbalance = compute_imbalance(book)
                            s         = maker.status()
                            print(
                                f"[1s BAR] mid={mid:.3f}  spread={spread:.5f}  "
                                f"imbalance={imbalance:.4f}  trades={trade_count}  "
                                f"INV={s['inventory']}  PnL={s['pnl']}"
                            )
                            last_mid = mid
                    trade_count  = 0
                    last_log_ts  = now
                continue

            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    # End of partition — nothing to process right now
                    continue
                print(f"[OB CONSUMER][ERROR] {msg.error()}")
                continue

            topic = msg.topic()

            # ----------------------------------------------------------------
            # Dispatch by topic
            # ----------------------------------------------------------------
            if topic == "market.depth":
                ev = decode_depth(msg.value())
                book.on_depth_diff(ev)
                maker.on_book_update()

            elif topic == "market.trades":
                ev = decode_trade(msg.value())
                maker.on_trade(ev)
                trade_count += 1

    except KeyboardInterrupt:
        print("\n[OB CONSUMER] Shutting down …")
    finally:
        # Commit final offsets before exit
        consumer.close()
        print("[OB CONSUMER] Consumer closed cleanly.")


def _maybe_log(*args, **kwargs):
    # Placeholder to keep the no-message branch readable; actual logging
    # logic is inlined above to avoid function-call overhead on the hot path.
    pass


if __name__ == "__main__":
    run_consumer()
