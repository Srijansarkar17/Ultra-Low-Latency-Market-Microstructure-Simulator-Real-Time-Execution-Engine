## data_feed.py
This creates a single websocket that listens to two streams at once : trade events and depth events

### Trade Events
Trade events (also known as "trade feeds") provide a real-time record of every transaction that has been executed and matched on the exchange. This data stream essentially confirms completed transactions and is the basis for the "last traded price" and volume metrics. 
Key information in a typical trade event includes:
Price: The exact price at which the trade was executed.
Quantity: The amount of the asset traded in that specific transaction.
Timestamp: The precise time the trade occurred.
Direction (sometimes): Whether the trade was initiated by a buyer (taker buy) or a seller (taker sell). 
Trade events are crucial for understanding current price movements and historical volume data


### Depth Events
Depth events (or "depth feeds") provide updates to the order book, which is an electronic list of all pending buy (bids) and sell (asks or offers) orders at various price levels. Market depth data helps traders assess the liquidity of an asset and predict potential future price movements. 

Depth events can come in different levels of detail:
Level 1 Data: Provides only the best bid (highest buy price) and best ask (lowest sell price).
Level 2/3 Data: Provides a view of multiple price levels in the order book (e.g., the top 5, 10, or 20 bids and asks).
Full Order Book (Tick by Tick): Provides every single change to any order in the entire book, requiring exponentially more bandwidth. 

Key information in a typical depth event includes:
Price Level: A specific price point in the order book.
Aggregated Quantity: The total volume of orders waiting to be filled at that specific price.
Updates: Notifications for additions, modifications, or removals of orders from the order book. 


### Meaning of recv() in Websockets
Wait for the next message from the WebSocket server and return it.
It's an asynchronous receive. Your code pauses until the next message arrives.
It does NOT block the entire program — only this coroutine pauses.

## Output Format and Meaning
What each top-level field means

Each printed line is one event (as NDJSON) with:

- ts_recv_ms – when your program received the message (milliseconds).

- latency_ms – your rough one-way latency.

- stream – which feed it came from:

- btcusdt@depth@100ms → order book diff depth updates

- btcusdt@trade → individual trades

- data – the raw Binance payload.


Two kinds of payloads you’re getting
1) Depth diffs: "e": "depthUpdate"

Example fields inside data:

E – event time (microseconds in your case, because you added timeUnit=MICROSECOND)

s – symbol (BTCUSDT)

U – first update ID in this event (sequence)

u – last update ID in this event (sequence)

b – bid updates: list of [price, qty]

a – ask updates: list of [price, qty]

Rules of thumb:

A level with "qty" == "0.00000000" means remove that price level.

Non-zero qty means set/replace the quantity at that price.

U/u are for ordering/continuity if you maintain a local order book.

2) Trades: "e": "trade"

Example fields:

E – event time (microseconds with your URL)

t – trade ID

p – price (string)

q – quantity (string)

m – is buyer the market maker?

true → taker was a seller (price moved down / hit the bid)

false → taker was a buyer (price moved up / lifted the ask)