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
            avg_price: float = 0.0,
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

        #To correct the mistake of PnL
        self.avg_price = 0.0

    
    # This function decides where to place buy and sell orders every time the order book changes.
    # Look at the market → decide my prices → place quotes safely
    def on_book_update(self):
        """
        Called every time the order book updates
        """
        # This function runs again and again, whenever: a depth update arrives, best bid / best ask changes

        if not self.book.synced:
            return # if the order book is not fully synced yet, do NOTHING, because an unsynced book means wrong prices and wrong prices means loss
        
        bb = self.book.best_bid() # bb = highest price someone wants to buy
        ba = self.book.best_ask() # ba = lowest price someone wants to sell

        if bb is None or ba is None: # if either side is not there don't quote
            return
        
        mid = (bb+ ba)/2 # Calculate Mid Price and Spread
        spread = bb - ba

        if spread > 0.5: # If the market is too wide, market is unstable, high risk, low liquidity, so dont place any orders. This is RISK MANAGEMENT
            self.bid_quote = None
            self.ask_quote = None
            return
        
        # Inventory-Based Skew
        skew = self.inventory * self.inventory_skew # This adjusts prices based on how much BTC you already hold.
        # Example:
        # inventory = +0.01 BTC (you bought too much)
        # inventory_skew = 0.02
        # skew = 0.01 × 0.02 = 0.0002
        # Meaning: lower both prices, sell faster, stop buying, This prevents inventory blow-up.


        # Calculate Quote Prices(Explaination with real life example in README.md)
        bid_price = mid - self.spread_offset - skew
        ask_price = mid + self.spread_offset - skew

        # Enforce Inventory Limits
        if self.inventory >= self.max_inventory: # If you already own too much BTC: Stop buying
            bid_price = None

        if self.inventory <= -self.max_inventory: # If you already sold too much BTC:Stop selling
            ask_price = None

        # Create Quotes
        if bid_price:
            self.bid_quote = (
                Quote("buy", bid_price, self.quote_size)
            )
        else:
            None

        if ask_price:
            self.ask_quote = (
                Quote("sell", ask_price, self.quote_size)
            )
        else:
            None
    

    def on_trade(self, trade: Trade): # This runs every time a trade happens in the market.
        """
        Simulate fills using trade prints
        """
        # A trade print means: Someone actually bought or sold at a real price.

        if self.bid_quote and trade.price <= self.bid_quote.price: # What this checks: Do I currently have a buy order (bid_quote)?  # You had a buy order in the market,  Someone traded at a price equal or lower So your buy order was filled

            # This code updates your average buy price when your buy order gets filled.
            # Why this matters:
                # You might buy BTC multiple times at different prices
                # To calculate real profit later, you need to know:
                # “On average, at what price did I buy my BTC?”
                # That’s exactly what avg_price represents.
            ##Fixing the BUY logic
            qty = self.bid_quote.qty
            price = trade.price

            # Two cases exist:
            # Case 1: You already owned BTC
            if self.inventory != 0:
                current_value = self.avg_price * abs(self.inventory)  # How much money your existing BTC is worth (at average price), Example : Inventory = 0.02 BTC, avg_price = 86,000, So , current_value = 0.02 × 86,000 = 1,720
                new_value = price*qty # This is the value of new BTC you just bought. Example: 0.01 × 87,000 = 870
                total_quantity = abs(self.inventory) + qty  # Total BTC you now hold:  Example: 0.02 + 0.01 = 0.03 BTC

                self.avg_price = (current_value + new_value) / total_quantity # Recalculate average price, Example: 
                # avg_price = (1720 + 870) / 0.03
                        #   = 2590 / 0.03
                        #   = 86,333, Your new average buy price is 86,333.
            
            # Case 2: This is your first BUY
            else:
                # If there was no inventory before, avg price is simply the new price
                self.avg_price = price # Your average price is simply the buy price


            self.inventory += qty
            print(f"[FILL] BUY {price}")
            self.bid_quote = None

        if self.ask_quote and trade.price >= self.ask_quote.price:
            # Similar to that of buy
            qty = self.ask_quote.qty
            price = trade.price

            #realized_pnl = sell_price - avg_buy_price
            self.realized_pnl += (price - self.avg_price) * qty
            self.inventory -= qty
            print(f"[FILL] SELL {price}")
            self.ask_quote = None



    def status(self): # This function is called when you want to see what’s going on inside your market maker
        # Think of it like: “Show me my current position.”
        return {
            "inventory" : round(self.inventory, 6),
            "pnl" : round(self.realized_pnl, 2),
            "bid": self.bid_quote.price if self.bid_quote else None,
            "ask": self.ask_quote.price if self.ask_quote else None,
        }

