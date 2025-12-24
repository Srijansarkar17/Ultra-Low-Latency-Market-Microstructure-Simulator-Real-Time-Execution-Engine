from dataclasses import dataclass # This allows us to create **simple data containers**  (no logic, just data). Think of it like a **struct**
from typing import Optional
from market_handler import Trade  # Imports **real trade events** coming from Binance
from order_book_engine import OrderBookEngine # Imports your order book.

# The market maker does NOT build prices itself. It reads the book to know where the market is.

@dataclass
class Quote: # This represents one order you place. For Example: Quote("buy", 87847.87, 0.001). Meaning “I want to buy 0.001 BTC at price 87847.87”
    side: str # This is your own order, not from the exchange.
    price: float
    qty: float

class MarketMaker:  # This class is your market-making engine. It decides prices, tracks inventory, tracks profit
    def __init__(
            self,
            book: OrderBookEngine,
            quote_size: float = 0.01,
            max_inventory: float = 0.01,
            spread_offset: float = 0.01,
            inventory_skew: float = 0.02,
    ):
        self.book = book # Store a reference to your **live order book, The market maker **reads prices from here**: - best bid - best ask - spread

        #risk controls
        self.inventory = 0.0 # How much BTC you currently hold. Example: +0.002 → you bought BTC, -0.002 → you sold BTC. This is the most important risk variable.
        self.max_inventory = max_inventory # Safety limit. If I hold too much BTC, STOP buying. This prevents **blowing up

        # quoting params
        self.quote_size = quote_size # How big each order is. Example: 0.001 BTC per order. Small Order: safer
        self.spread_offset = spread_offset # How far from mid-price you place orders. Example: mid = 100 spread_offset = 0.01 so buy at 99.99 sell at 100.01. This is how you earn the spread.

        self.inventory_skew = inventory_skew # Controls **how aggressively prices move** based on inventory.
        # Example: If you bought too much → lower prices to sell faster, If you sold too much → raise prices to buy back

        # active quotes
        self.bid_quote: Optional[Quote] = None # These store your current open orders. At any time: You may have one buy quote, One sell quote or None
        self.ask_quote: Optional[Quote] = None

        # PnL(Profit and Loss)
        self.realized_pnl = 0.0 # Tracks **actual money earned/lost** from completed trades. Buy → PnL decreases, Sell → PnL increases
