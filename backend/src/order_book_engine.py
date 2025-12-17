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

    def load_snapshot(self): # This function downloads a full starting order book from Binance once

        """ Fetch initial order book snapshot from Binance REST API """ 
        url = "https://api.binance.com/api/v3/depth"   # Give me the current full order book state at this moment
        params = { 
            "symbol": self.symbol, 
            "limit": self.snapshot_limit 
        }

        # You must re-snapshot if ANY of these happen:
        # - Sequence gap detected ,  Expected next update id = last_update_id + 1, But received U > last_update_id + 1
        # - WebSocket reconnect - If your WS disconnects for even 1 second, then we need re-snapshot
        # - Engine restart / crash , then we Re-Snapshot

        resp = requests.get(url, params=params, timeout=5)
        data = resp.json()

        """
        You receive a response like:
            {
            "lastUpdateId": 82735727099,
            "bids": [["90105.00", "3.38"], ...],
            "asks": [["90106.20", "0.51"], ...]
            }
        """

        # Reset local book , You wipe everything you had before Because the snapshot is the source of truth.
        self.bids.clear()
        self.asks.clear()

        for price, qty in data["bids"]:
            self.bids[float(price)] = float(qty)

        """
        Now your book becomes:

        bids = {
            90105.0: 3.38,
            90104.9: 1.12
            }
        """
        # We do the same for asks as we did for bids
        for price, qty in data["asks"]:
            self.asks[float(price)] = float(qty)

        self.last_update_id = data["lastUpdateId"]
        self.synced = False # Why? You fetched snapshot But you haven’t replayed buffered diffs yet So the book is not live yet.

        




