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



### Order Book Engine
What the Order Book Engine does (in simple words)

It takes DepthDiff events and maintains:

- Current bids (price → quantity)

- Current asks (price → quantity)

- Correct sequence order

- Detects missed updates

Outputs:

- Best bid

- Best ask

- Spread

- (later) imbalance, microprice

This is exactly what real HFT feed handlers do.


### How Order Book Engines work
```text
Start
 ↓
Receive diffs → BUFFER
 ↓
Fetch SNAPSHOT (REST)
 → We use REST API to get the full order book from Binance when we fall out of sync
 ↓
Find first diff where:
 U ≤ lastUpdateId + 1 ≤ u
 ↓
Apply diffs
 ↓
Set synced = True
 ↓
Continue live updates
```




### CODE EXPLAINATION OF ORDER_BOOK_ENGINE

👉 You are defining a **new component** whose job is:

> “Maintain the live order book for one trading symbol.”

This class will:
- Receive depth updates
- Store bids & asks
- Detect gaps
- Tell you best bid/ask

- self.buffer = deque(maxlen=5000) # Using Double Ended Queue for fast insertions and removals from both ends.


### BIG CONFUSION: “Why REST API if I already have WebSocket data?”

This is the most important concept 👇

🚨 WebSocket depth data is NOT a full order book

What you receive from WebSocket:

{
  "U": 82735727088,
  "u": 82735727098,
  "b": [["90105.00", "3.38"]],
  "a": [["90106.20", "0.51"]]
}


This means:

“Change these price levels”

It does NOT mean:

“This is the full book”

“Here is the starting state”

🧠 Why snapshot is mandatory (real-world analogy)
Imagine WhatsApp messages

You join a group late.

Messages you receive:
“Delete message”, “Edit message”, “React 👍”

❓ But delete/edit what message?

You first need:

The full chat history

That’s the snapshot

#### You must re-snapshot if ANY of these happen:
        # - Sequence gap detected ,  Expected next update id = last_update_id + 1, But received U > last_update_id + 1
        # - WebSocket reconnect - If your WS disconnects for even 1 second, then we need re-snapshot
        # - Engine restart / crash , then we Re-Snapshot


#### Functions
load_snapshot() -> this function is used to load the snapshot from Binance

on_depth_diff() -> #Apply DIFF. ( on_depth_diff() decides what to do with each depth update: ), stores in buffer and checks if the current diff is too old and it detects gaps

_try_sync() ->  tries to connect the snapshot with the buffered depth updates so the order book becomes correct and usable.

_apply_diff() -> takes a depth update and modifies your local order book so it matches the exchange.



## market_maker.py

At first: 
  This code creates the brain of a market-making bot that:

  looks at the order book,

  decides where to place buy/sell quotes,

  tracks risk (inventory),

  tracks profit & loss (PnL).

  Nothing is trading yet — this is just setting up the brain.


  on_book_update() -> This function decides where to place buy and sell orders every time the order book changes("""
    Called every time the order book updates
    """)

  #### Explaination of this logic inside on_book_update()

  Step 6: Calculate Quote Prices 
  bid_price = mid - self.spread_offset - skew 
  ask_price = mid + self.spread_offset - skew 
  
  ##### Without skew 
  buy at mid - spread_offset 
  sell at mid + spread_offset 
  
  ##### With skew 
  inventory > 0 → prices move DOWN 
  inventory < 0 → prices move UP

  #### Explaination
  et’s do this with a very concrete real-life example, step by step.

🎯 Scenario: You are a BTC market maker
Current market
Best Bid (bb) = 100.00
Best Ask (ba) = 100.10


So:

mid = (100.00 + 100.10) / 2 = 100.05


You choose:

spread_offset = 0.05
inventory_skew = 0.02

✅ Case 1: No inventory (neutral)
inventory = 0
skew = inventory × inventory_skew = 0

Prices
bid = mid - spread_offset = 100.05 - 0.05 = 100.00
ask = mid + spread_offset = 100.05 + 0.05 = 100.10


📌 You quote exactly at the edges of the book.

💡 Interpretation
You are balanced.
No risk.
Just collect spread.

⚠️ Case 2: You bought too much BTC (inventory > 0)
inventory = +1.0 BTC
skew = 1.0 × 0.02 = 0.02

Prices
bid = 100.05 - 0.05 - 0.02 = 99.98
ask = 100.05 + 0.05 - 0.02 = 100.08


📉 Prices moved DOWN

Side	Before	After
Buy	100.00	99.98
Sell	100.10	100.08
Why?

You already own too much BTC

You want to:

buy less → bid lower

sell faster → ask lower

📌 This pushes inventory back toward zero.

⚠️ Case 3: You sold too much BTC (inventory < 0)
inventory = -1.0 BTC
skew = -1.0 × 0.02 = -0.02

Prices
bid = 100.05 - 0.05 - (-0.02) = 100.02
ask = 100.05 + 0.05 - (-0.02) = 100.12


📈 Prices moved UP

Side	Before	After
Buy	100.00	100.02
Sell	100.10	100.12
Why?

You are short BTC

You want to:

buy faster → bid higher

sell less → ask higher

📌 Again, inventory moves back toward zero.

🧠 Real-Life Analogy (Very Intuitive)

Imagine you run a gold exchange shop.

You have TOO MUCH gold

You lower your buy price (don’t want more gold)

You lower your sell price (want to get rid of gold fast)

You have TOO LITTLE gold

You raise your buy price (attract sellers)

You raise your sell price (slow down selling)

That’s exactly what skew does.