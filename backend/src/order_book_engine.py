import requests
from collections import deque
from typing import Dict
from market_handler import DepthDiff

class OrderBookEngine:
    def __init__(self, symbol: str, snapshot_limit: int=1000 ): # This __init__ function creates and prepares a fresh, empty order book that is not yet trusted until it syncs with the exchange.

        # price -> quantity
        self.bids: Dict[float, float] = {} #stores the bids
        self.asks: Dict[float, float] = {} #stores the asks

        self.last_update_id: int | None=None # This stores the **latest sequence number** you have applied , - `None` → “I have no snapshot yet”
        self.synced: bool = False

        self.buffer = deque(maxlen=5000) # This is a **temporary waiting area** for depth updates. using Double Ended Queue

        self.snapshot_limit = snapshot_limit # Stores how **deep** your snapshot should be. 
        #Example:
        # - `100` → top 100 bids + asks
        # - `1000` → deeper book (more realistic)

        
