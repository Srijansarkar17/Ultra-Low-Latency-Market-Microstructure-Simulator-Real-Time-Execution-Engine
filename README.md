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


  ### on_book_update() -> This function decides where to place buy and sell orders every time the order book changes("""
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



  ### on_trade() -> This function pretends your limit orders got filled when market trades cross your quoted prices.

  #### Explaination of on_trade() function:
  This sentence is the key idea behind paper trading.

  What is a limit order?

  A limit order is you saying:

  “I want to buy only if price is ≤ X”

  “I want to sell only if price is ≥ Y”

  ```
  Example:

  BUY  BTC @ 100.00
  SELL BTC @ 100.10


  You’re waiting, not forcing a trade.
  ```

  What is a market trade?

A market trade is when someone else actually trades right now.


```
Example:

Someone sells BTC at 99.98
Someone buys BTC at 100.12
```

These are real trades happening in the market.

What does “cross your quoted prices” mean?

It means:

🔹 BUY side

If the market price goes DOWN to or below your buy price,
someone would sell to you.

Your BUY quote = 100.00
Market trade = 99.98  ← crossed your price

🔹 SELL side

If the market price goes UP to or above your sell price,
someone would buy from you.

Your SELL quote = 100.10
Market trade = 100.12 ← crossed your price

What does “pretends your orders got filled” mean?

Because:

You are not actually sending orders to Binance

You are only watching trade data

So your code says:

“If this trade would have filled my order on a real exchange,
then I’ll pretend it did.”

###### Simple Analogy (Real Life)

Imagine you put a sign outside your shop:

BUY apples at ₹100
SELL apples at ₹110


Now:

Someone sells apples at ₹98 → they’d come to you

Someone buys apples at ₹112 → they’d buy from you

Even if you didn’t physically transact,
you assume it happened because your price was better.


### Correction of the PnL Logic Error
We counted "money recieved" as profit, whereas the profit should only happen after you both BUY and SELL

##### Example
🏪 Real-life example: Fruit Shop 🍎

Imagine you run a fruit shop (you = market maker).

Prices in the market

People buy apples at ₹99

People sell apples at ₹101

So the spread = ₹2

❌ What your code is doing now (WRONG)
Step 1: You SELL first

You sell 1 apple at ₹101

Your code says:

Profit += 101


💥 This is the bug

Because…

Reality:

You gave away an apple

You don’t own it anymore

You haven’t bought it yet

This is cash flow, NOT profit.

Step 2: You STOP selling

Your inventory becomes:

inventory = -1 apple


Your risk control says:

“I am short apples, stop selling”

✅ This part is CORRECT.

But your PnL shows:
Profit = ₹101 ❌


That’s impossible.

You didn’t earn ₹101.
You just received money for something you owe.

✅ What PROFIT actually means
Profit happens only when you COMPLETE THE CYCLE
Correct cycle:

Buy low

Sell high
OR

Sell high

Buy low

✅ Correct behavior with the same example
Step 1: SELL first (still no profit)

You sell 1 apple @ ₹101

Thing	Value
Inventory	-1 apple
Cash	+₹101
Profit	₹0 ✅

No profit yet.

Step 2: BUY later

You buy 1 apple @ ₹99

Now calculate profit:

Profit = Sell Price - Buy Price
Profit = 101 - 99 = ₹2


🎉 THAT is real profit.


### Explaination of new logic of realized_pnl

🛒 Real-life example (fruit shop 🍎)
Step 1: You buy apples

Buy 10 apples at ₹100 each

avg_price = 100

No profit yet ❌

Step 2: You sell apples

Sell 10 apples at ₹105 each

Selling price = 105

Now compute profit:

(price - avg_price) × qty
= (105 - 100) × 10
= 5 × 10
= ₹50


So:

self.realized_pnl += 50


✅ You actually made ₹50.

rading example (BTC)
Buy first → Sell later (LONG trade)

1️⃣ Buy 0.01 BTC at ₹91,200

avg_price = 91200
inventory = +0.01


2️⃣ Sell 0.01 BTC at ₹91,250

profit = (91250 - 91200) × 0.01
        = 50 × 0.01
        = ₹0.50

self.realized_pnl += 0.50


✅ Real profit.



### Bug Related to classic market-maker bug related to SHORT positions and avg_price handling.
We are treating a SELL as if it always closes a BUY
self.realized_pnl += (price - self.avg_price) * qty is only correct when you are selling BTC that you previously BOUGHT

But in your log:

👉 You sold first
👉 That means you opened a SHORT position
👉 There was no buy yet to close

##### What is missing in your logic

You currently handle only LONG logic correctly:

Buy → set avg_price

Sell → (sell - avg_buy) × qty

But for SHORT trades, logic is inverted:

SHORT logic (very important)

SELL first → this sets avg_price for the short

BUY later → this realizes PnL