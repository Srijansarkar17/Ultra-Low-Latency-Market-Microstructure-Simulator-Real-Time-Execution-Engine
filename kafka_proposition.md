# Kafka Integration Proposition
## Market Microstructure Simulator — Event-Driven Upgrade

---

## 1. Should You Use Kafka?

**Short answer: Yes, and it's a strong resume move — but only if you add it correctly.**

You already wrote "event-driven" on your resume. Your current architecture IS event-driven — but it uses Python's `asyncio.Queue`, which is an **in-process, single-machine** queue. Kafka replaces that queue with a **distributed, durable, multi-consumer** message broker. This is a significant, legitimate architectural upgrade.

> [!IMPORTANT]
> Kafka does NOT make your code faster. Binance WebSocket → Kafka → Consumer will actually add ~1–5ms of latency vs. direct asyncio. The value is **architectural correctness, durability, and scalability** — which is exactly what a senior engineering role cares about.

---

## 2. Your Current Architecture (What You Have Now)

```
Binance WebSocket
       │
       ▼
  ws_receiver()          ← pure recv loop
       │  (asyncio.Queue)
       ▼
  book_consumer()        ← applies depth diffs to OrderBookEngine
       │                 ← fires MarketMaker.on_book_update()
       ▼
  logging_loop()         ← 1s timer, reads book state, logs to CSV
```

**Problems with this for a resume:**
- Everything runs in one Python process, one thread (asyncio = cooperative, not parallel)
- If the logging loop crashes, you lose trades
- You can't plug in a second consumer (e.g., an ML model, a risk engine) without rewriting `main()`
- No replay: if something breaks, you lose all tick data

---

## 3. Kafka Architecture (What You'd Build)

```
Binance WebSocket
       │
       ▼
  ┌─────────────────────────────┐
  │    PRODUCER SERVICE         │
  │  (ws_receiver → Kafka)      │
  │                             │
  │  Topic: market.depth        │  ← DepthDiff events (JSON)
  │  Topic: market.trades       │  ← Trade events (JSON)
  └─────────────────────────────┘
             │
             │  (Kafka Broker — local Docker)
             │
    ┌────────┴────────┐
    │                 │
    ▼                 ▼
┌──────────────┐  ┌──────────────────────┐
│  CONSUMER 1  │  │  CONSUMER 2          │
│ OrderBook +  │  │ FeatureLogger        │
│ MarketMaker  │  │ (ML Feature Logging) │
└──────────────┘  └──────────────────────┘
```

Each box is an **independent Python process**. They can be killed/restarted independently.  
Kafka stores all messages durably → you can **replay** any day's tick data.

---

## 4. The Three Kafka Topics You'd Create

| Topic | Key | Value | Partitions |
|---|---|---|---|
| `market.depth` | `BTCUSDT` | Serialized `DepthDiff` (JSON/Avro) | 1 |
| `market.trades` | `BTCUSDT` | Serialized `Trade` (JSON/Avro) | 1 |
| `maker.quotes` | `BTCUSDT` | MarketMaker quote snapshots | 1 |

For a single-symbol simulator, 1 partition each is fine. Multi-symbol → partition by symbol.

---

## 5. Exact Code Changes Required

### 5a. New File: `kafka_producer.py` (replaces `ws_receiver` in `data_feed.py`)

```python
from confluent_kafka import Producer
import asyncio, json, websockets, time
from market_handler import MarketDecoder

KAFKA_CONF = {"bootstrap.servers": "localhost:9092"}
WS_URL = "wss://stream.binance.com:9443/stream?streams=btcusdt@depth@100ms/btcusdt@trade&timeUnit=MICROSECOND"

async def run_producer():
    producer = Producer(KAFKA_CONF)
    decoder = MarketDecoder(expect_microseconds=True)

    async with websockets.connect(WS_URL) as ws:
        while True:
            raw = await ws.recv()
            ts_recv_us = int(time.time() * 1_000_000)
            msg = json.loads(raw)
            ev = decoder.parse_combined(msg, ts_recv_us)

            if ev is None:
                continue

            payload = json.dumps(ev.__dict__).encode("utf-8")

            if ev.etype == "depth_diff":
                producer.produce("market.depth", key="BTCUSDT", value=payload)
            elif ev.etype == "trade":
                producer.produce("market.trades", key="BTCUSDT", value=payload)

            producer.poll(0)  # non-blocking flush trigger

asyncio.run(run_producer())
```

### 5b. New File: `kafka_consumer_orderbook.py` (replaces `book_consumer` + `logging_loop`)

```python
from confluent_kafka import Consumer
import json
from market_handler import DepthDiff, Trade
from order_book_engine import OrderBookEngine
from market_maker import MarketMaker

KAFKA_CONF = {
    "bootstrap.servers": "localhost:9092",
    "group.id": "orderbook-consumer-group",
    "auto.offset.reset": "latest",
}

def run_consumer():
    consumer = Consumer(KAFKA_CONF)
    consumer.subscribe(["market.depth", "market.trades"])

    book = OrderBookEngine(symbol="BTCUSDT")
    book.load_snapshot()
    maker = MarketMaker(book)

    while True:
        msg = consumer.poll(timeout=0.001)  # 1ms poll — sub-200ms latency maintained
        if msg is None or msg.error():
            continue

        data = json.loads(msg.value())
        topic = msg.topic()

        if topic == "market.depth":
            ev = DepthDiff(**data)
            book.on_depth_diff(ev)
            maker.on_book_update()

        elif topic == "market.trades":
            ev = Trade(**data)
            maker.on_trade(ev)

run_consumer()
```

---

## 6. Running Kafka Locally (Docker — 2 commands)

```bash
# 1. Start Kafka + Zookeeper with Docker Compose
docker compose up -d

# 2. Create topics
docker exec -it kafka kafka-topics.sh \
  --create --topic market.depth --partitions 1 --replication-factor 1 \
  --bootstrap-server localhost:9092

docker exec -it kafka kafka-topics.sh \
  --create --topic market.trades --partitions 1 --replication-factor 1 \
  --bootstrap-server localhost:9092
```

`docker-compose.yml`:
```yaml
version: "3"
services:
  zookeeper:
    image: confluentinc/cp-zookeeper:7.5.0
    environment:
      ZOOKEEPER_CLIENT_PORT: 2181

  kafka:
    image: confluentinc/cp-kafka:7.5.0
    ports:
      - "9092:9092"
    environment:
      KAFKA_BROKER_ID: 1
      KAFKA_ZOOKEEPER_CONNECT: zookeeper:2181
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://localhost:9092
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
```

---

## 7. What This Gives You on Your Resume

| Claim | Before Kafka | After Kafka |
|---|---|---|
| "Event-driven" | ✅ asyncio Queue | ✅ Kafka Topics (industry standard) |
| Durability | ❌ In-memory only | ✅ Disk-backed, replay-able |
| Decoupled producers/consumers | ❌ Same process | ✅ Separate processes |
| Multi-consumer fan-out | ❌ | ✅ Add ML model as Consumer 2 |
| Horizontal scalability | ❌ | ✅ Add partitions/consumers |

**Updated resume bullet that becomes defensible:**
> *"Designed a Kafka-backed event-driven pipeline ingesting live Binance L2 order book streams; decoupled WebSocket producer from order book consumer and ML feature logger via durable topics, achieving sub-200ms end-to-end latency."*

---

## 8. Honest Trade-offs (Know This for Interviews)

> [!WARNING]
> Interviewers WILL ask: "Why Kafka over Redis Streams / RabbitMQ / raw asyncio?"

**Your answer:**
- **vs. asyncio.Queue**: Kafka is durable (survives restarts), multi-process, and gives you tick data replay for backtesting — asyncio.Queue dies with the process.
- **vs. Redis Streams**: Kafka has better throughput at scale and native partition-based parallelism. Redis Streams is simpler for single-machine use.
- **vs. RabbitMQ**: Kafka is a log, not a queue — messages aren't deleted on consumption. You can replay historical tick data by resetting consumer offsets, which is priceless for backtesting.

**Latency reality:**
- asyncio direct: ~50–100µs queue overhead
- Kafka (local): ~1–5ms broker round-trip
- You compensate by: using `poll(0.001)` in consumer (non-blocking), keeping Kafka broker on localhost (loopback only), and noting latency is **still well within your sub-200ms claim**

---

## 9. Recommended Implementation Order

- [ ] 1. Set up Docker Compose with Kafka + Zookeeper
- [ ] 2. Create `kafka_producer.py` — pipe WebSocket → Kafka topics
- [ ] 3. Create `kafka_consumer_orderbook.py` — consume → OrderBook + MarketMaker
- [ ] 4. Wire serialization: use `dataclasses.asdict()` for clean JSON payloads
- [ ] 5. Add `kafka_consumer_logger.py` as a second independent consumer (ML features)
- [ ] 6. Update `README.md` with architecture diagram
- [ ] 7. Update resume bullet with Kafka-specific language

