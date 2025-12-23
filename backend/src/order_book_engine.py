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


    #Apply DIFF. ( on_depth_diff() decides what to do with each depth update: )
    def on_depth_diff(self, diff: DepthDiff):  # This function is called every time a depth update arrives from the WebSocket.
        if not self.synced:
            self.buffer.append(diff) # Store the update in the buffer
            self._try_sync() # Try to see if snapshot + buffer can now be connected
            return
        
        # Ignore old Updates
        if diff.u <= self.last_update_id:  # If the update you received is older than what you already applied, ignore it.
            return
        # ( example:
        # last_update_id = 100
        # incoming diff: U=90, u=95
        # ❌ This update is already outdated
        # ✔ Ignore it safely)
        

        # Detect a GAP
        if diff.U > self.last_update_id + 1:  # This is the most critical safety check. This means you missed some updates, your order book is now wrong

            print("[ORDERBOOK] Gap Detected - > Resync")
            self.load_snapshot()
            self.buffer.clear()
            return
        
        self._apply_diff(diff)


    # _try_sync() tries to connect the snapshot with the buffered depth updates so the order book becomes correct and usable.
    def _try_sync(self): # It is called: After snapshot , Every time a new diff is buffered

        if self.last_update_id is None: #. If you haven’t fetched the snapshot yet: You don’t know the starting state You can’t syncSo you just wait
            return

        while self.buffer: # Loop over buffered diffs This means: “As long as there are buffered updates, try to process them.
            diff = self.buffer[0] # Look at the FIRST buffered diff,  Updates must be applied in order

            if diff.u <= self.last_update_id: # Discard diffs that are too old
                self.buffer.popleft()
                continue

            # Check the BRIDGING CONDITION (MOST IMPORTANT)

            if diff.U <= self.last_update_id + 1 <= diff.u: #this means its correct
                # Apply the diff and complete sync
                self._apply_diff(diff) # Apply this diff to the order book
                self.buffer.popleft() # Remove it from buffer
                self.synced = True # Mark order book as synced
                print("[ORDERBOOK] Book synced")

            # still waiting for correct diff
            return
                
                
    
        







    






