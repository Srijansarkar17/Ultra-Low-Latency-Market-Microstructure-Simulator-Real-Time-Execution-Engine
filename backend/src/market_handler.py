from __future__ import annotations
from dataclasses import dataclass #@dataclass creates simple data containers (less boilerplate).
from typing import List, Tuple, Optional, Literal

EventType = Literal["depth_diff", "trade"] # A type that can only be depth_diff or trade

@dataclass
class DepthDiff:
    etype: EventType     #tells us the kind of event("depth_diff")
    ts_event_us: int     # is Binance’s event timestamp in microseconds
    ts_recv_us: int      # is your receive timestamp in microseconds
    symbol: str          # e.g., "BTCUSDT"
    U: int               # first update id
    u: int               # last update id
    pu: Optional[int]    # previous final update id (may be None)
    bids: List[Tuple[float, float]]  # [(price, qty), ...]
    asks: List[Tuple[float, float]] # bids and asks are lists of (price, quantity) updates.


@dataclass
class Trade:
    etype: EventType
    ts_event_us: int
    ts_recv_us: int
    symbol: str
    trade_id: int
    price: float
    qty: float
    taker_side: Literal["buy","sell"]  # buy=lifting ask, sell=hitting bid
# taker_side: "buy" = aggressive buyer (lifted ask), "sell" = aggressive seller (hit bid).

class MarketDecoder: # This class only converts raw Binance JSON → your DepthDiff/Trade objects.
    """
    Stateless decoder that converts Binance WS messages into normalized events.  “Stateless” = it doesn't keep order book state; just parsing
    Use this if you already have the WebSocket connection elsewhere.
    """

    def __init__(self, expect_microseconds: bool = True):
        # If your WS URL has &timeUnit=MICROSECOND, set True; else False.
        self.expect_microseconds = expect_microseconds

    def _to_us(self, E: int | float | None, fallback_us: int) -> int: # Takes Binance’s E (event time) and returns microseconds. If E missing/bad → use your local receive time (fallback_us). If expect_microseconds=True → already µs; else convert ms → µs.
        if E is None:
            return fallback_us
        try:
            Ei = int(E)
        except Exception:
            return fallback_us
        return Ei if self.expect_microseconds else Ei * 1000


    # This function decides whether the message is: a depth update, a trade or something else
    # If stream == "btcusdt@depth@100ms" → send to _depth_from_payload
    # If stream == "btcusdt@trade" → send to _trade_from_payload
    def parse_combined(self, msg: dict, ts_recv_us: int): # msg: the raw JSON received from Binance (as a Python dict).
        """
        For combined streams: msg looks like {"stream": "<name>", "data": {...}}
        """
        """the raw message looks like this 
                msg = {
                "stream": "btcusdt@trade",
                "data": {"e": "trade", "p": "90105.01", "q": "0.001"}
                }
        """
        if not isinstance(msg, dict): # If msg is NOT a Python dict → return nothing. This protects you from corrupted or weird data.
            return None
        stream = msg.get("stream", "") # parses out the stream part, For example Gets "btcusdt@trade" or "btcusdt@depth@100ms"
        data = msg.get("data", {}) # parses out the main data
        if not isinstance(data, dict): # makes sure that after parsing the data is also in dictionary
            return None
        return self._parse_stream(stream, data, ts_recv_us)


    # sometimes messages do not have any stream field so the entire JSON payload is the data. Thats why we need a different kind of parser
    def parse_raw(self, payload: dict, ts_recv_us: int): # payload is the raw JSON dict received from the websocket.
        """
        For raw streams: msg itself is the payload (no 'stream' wrapper).
        """
        # raw streams don't have "stream" or "data" keys
        if not isinstance(payload, dict):
            return None
        etype = payload.get("e", "") # Binance messages always include e, meaning event type. Could be: "depthUpdate" → order book diff "trade" → trade or something else like "kline" (ignored)
        if etype == "depthUpdate":  # If the event is a depth update → send the payload to _depth_from_payload()
            return self._depth_from_payload(payload, ts_recv_us)
        if etype == "trade": # If "e" == "trade" → pass data to _trade_from_payload()
            return self._trade_from_payload(payload, ts_recv_us)
        return None # it is neither depthUpdate nor trade

    # When you're using combined streams, Binance sends data like this
    #this function is called at the end of the parse_combined function
    def _parse_stream(self, stream: str, data: dict, ts_recv_us: int): # Look at the "stream" name, Decide if it is a depth update or trade
        # Depth streams end with "@depth" or "@depth@100ms"
        if stream.endswith("@depth") or stream.endswith("@depth@100ms"):
            return self._depth_from_payload(data, ts_recv_us)
        # Trade stream ends with "@trade"
        if stream.endswith("@trade"):
            return self._trade_from_payload(data, ts_recv_us)
        return None
    
    def _depth_from_payload(self, d: dict, ts_recv_us: int): 
        evt_us = self._to_us(d.get("E"), ts_recv_us) #self._to_us is a function written in this code that converts into microseconds, if Binance didn't send E, then fallback to ts_recv_us.

        bids = [(float(p), float(q)) for p, q in d.get("b", [])] # d.get("b", []) = list of bid updates,  Each bid is [price, quantity], Convert both to floats, Output shape: [(price, qty), (price, qty), ...]

        asks = [(float(p), float(q)) for p, q in d.get("a", [])] # same thing as bids but for asks

        return DepthDiff( # this creates a clean object
            etype="depth_diff", # So your system knows this is a depth update.
            ts_event_us=evt_us,
            ts_recv_us=ts_recv_us,
            symbol=d.get("s", "UNKNOWN"),
            U=int(d["U"]),
            u=int(d["u"]),
            pu=int(d["pu"]) if "pu" in d and d["pu"] is not None else None,
            bids=bids,
            asks=asks,
        )














