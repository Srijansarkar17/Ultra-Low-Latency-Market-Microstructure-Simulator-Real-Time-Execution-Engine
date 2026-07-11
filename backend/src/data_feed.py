import asyncio  # allows asynchronous code to run
import json
import time
import websockets
from market_handler import MarketDecoder, DepthDiff, Trade
from asyncio import Queue
from order_book_engine import OrderBookEngine
from market_maker import MarketMaker
from volatility_prediction_ml.feature_logger import FeatureLogger

SYMBOL = "btcusdt"  # lowercase for WebSockets
WS_URL = f"wss://stream.binance.com:9443/stream?streams={SYMBOL}@depth@100ms/{SYMBOL}@trade&timeUnit=MICROSECOND"

# ---------------------------------------------------------------------------
# Imbalance calculator (reads from book; called inside the timer loop)
# ---------------------------------------------------------------------------
def compute_imbalance(book, depth=5):
    bids = sorted(book.bids.items(), reverse=True)[:depth]
    asks = sorted(book.asks.items())[:depth]

    bid_qty = sum(qty for _, qty in bids)
    ask_qty = sum(qty for _, qty in asks)

    if bid_qty + ask_qty == 0:
        return 0.0

    return (bid_qty - ask_qty) / (bid_qty + ask_qty)


# ---------------------------------------------------------------------------
# Consumer: Order Book Updater  (Fix D — all book mutations happen HERE)
# ---------------------------------------------------------------------------
async def book_consumer(q: Queue, book: OrderBookEngine, maker: MarketMaker, trade_count):
    """
    Consumes decoded market events from the queue and updates the order book.

    FIX D: All state mutations (book, trade_count) happen ONLY inside this
    consumer. The logging timer loop reads book state AFTER the consumer has
    had a chance to drain the queue, preventing the stale-state concurrency
    bug where the logger read a not-yet-updated order book.
    """
    while True:
        ev = await q.get()  # await q.get() → wait until new data arrives

        if isinstance(ev, DepthDiff):
            book.on_depth_diff(ev)
            maker.on_book_update()

        elif isinstance(ev, Trade):
            # FIX C: trade_count accumulates here continuously.
            # It is reset ONLY by the 1-second logging_loop, not on every
            # WebSocket recv. This ensures no trades are silently dropped.
            maker.on_trade(ev)
            trade_count["count"] += 1


# ---------------------------------------------------------------------------
# 1-Second Timer Loop  (Fix A + C + D)
# ---------------------------------------------------------------------------
async def logging_loop(book: OrderBookEngine, maker: MarketMaker,
                       trade_count: dict, logger: FeatureLogger,
                       interval: float = 1.0):
    """
    FIX A: Samples market state at a fixed 1-second interval instead of on
    every WebSocket packet. Each row in features.csv now represents a real
    1-second time slice, making log returns and rolling statistics meaningful.

    FIX C: trade_count["count"] is reset HERE (once per second) so all
    trades that arrive within the 1-second window are properly aggregated
    before the reset — not silently cleared on every depth update packet.

    FIX D: By running as a separate asyncio task, the event loop has already
    given the book_consumer task multiple execution slots to drain the queue
    before this timer fires, so book.best_bid() / best_ask() reflect the
    most up-to-date state.
    """
    last_mid = None

    while True:
        await asyncio.sleep(interval)  # wait exactly 1 second

        if not book.synced:
            # Don't log until the order book is live and accurate
            continue

        bb = book.best_bid()
        ba = book.best_ask()

        if bb is None or ba is None:
            continue

        mid = (bb + ba) / 2.0
        spread = ba - bb

        # spread_change: difference from previous logged mid-price's spread
        if last_mid is None:
            spread_change = 0.0
        else:
            # Use mid price diff as a proxy (spread_change = Δspread is logged)
            spread_change = spread - (ba - bb)  # delta from last known spread

        imbalance = compute_imbalance(book)

        # Snapshot trade count for this second, then reset accumulator
        count_this_second = trade_count["count"]
        trade_count["count"] = 0  # FIX C: reset only here, once per second

        ts_us = int(time.time() * 1_000_000)

        s = maker.status()
        print(
            f"[1s BAR] mid={mid:.3f} spread={spread:.5f} "
            f"imbalance={imbalance:.4f} trades={count_this_second} "
            f"INV={s['inventory']} PNL={s['pnl']}"
        )

        logger.log(ts_us, mid, spread, spread_change, imbalance, count_this_second)
        last_mid = mid


# ---------------------------------------------------------------------------
# WebSocket receiver  (stripped to pure packet receipt + queue push)
# ---------------------------------------------------------------------------
async def ws_receiver(ws, decoder: MarketDecoder, q: Queue):
    """
    Pure receive loop. Reads raw WebSocket frames, decodes them, and pushes
    decoded events onto the shared queue. No book access, no logging here.
    """
    while True:
        raw = await ws.recv()
        ts_recv_us = int(time.time() * 1_000_000)
        msg = json.loads(raw)
        ev = decoder.parse_combined(msg, ts_recv_us)

        if ev is None:
            continue  # Skip unknown / heartbeat frames

        await q.put(ev)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
async def main():
    logger = FeatureLogger("features.csv")
    trade_count = {"count": 0}
    q: Queue = Queue(maxsize=10000)

    decoder = MarketDecoder(expect_microseconds=True)

    # Create order book engine + initial REST snapshot
    book = OrderBookEngine(symbol="BTCUSDT")
    book.load_snapshot()

    # Create market maker
    maker = MarketMaker(book)

    # Start the order-book consumer task
    consumer_task = asyncio.create_task(
        book_consumer(q, book, maker, trade_count)
    )

    async with websockets.connect(
        WS_URL, ping_interval=15, ping_timeout=10
    ) as ws:
        print(f"[WS] Connected to {WS_URL}")

        # Start the 1-second logging timer task
        timer_task = asyncio.create_task(
            logging_loop(book, maker, trade_count, logger, interval=1.0)
        )

        try:
            # Run the pure receive loop — all it does is fill the queue
            await ws_receiver(ws, decoder, q)
        finally:
            consumer_task.cancel()
            timer_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
