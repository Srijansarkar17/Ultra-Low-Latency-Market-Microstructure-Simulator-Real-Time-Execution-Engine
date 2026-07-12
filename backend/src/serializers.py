"""
serializers.py
--------------
Shared JSON serialisation / deserialisation for the two event types that flow
through Kafka: DepthDiff (market.depth) and Trade (market.trades).

Design decision:
  - We use plain JSON (not Avro / Protobuf) to keep the setup dependency-free.
  - dataclasses.asdict() gives us a clean dict → json.dumps pipeline with no
    custom __dict__ hacks and handles nested types correctly.
  - On the consumer side we reconstruct the dataclass from the dict so every
    consumer gets a fully-typed object, not a raw dict.
"""

import json
import dataclasses
from market_handler import DepthDiff, Trade


# ---------------------------------------------------------------------------
# Serialise (used by kafka_producer.py)
# ---------------------------------------------------------------------------

def encode_event(ev: DepthDiff | Trade) -> bytes:
    """
    Convert a DepthDiff or Trade dataclass instance to UTF-8 JSON bytes
    ready to be pushed as a Kafka message value.

    We tag the payload with an "etype" field so the consumer can reconstruct
    the correct type without relying on which Kafka topic the message came from.
    """
    return json.dumps(dataclasses.asdict(ev)).encode("utf-8")


# ---------------------------------------------------------------------------
# Deserialise (used by kafka_consumer_*.py)
# ---------------------------------------------------------------------------

def decode_depth(raw: bytes) -> DepthDiff:
    """
    Reconstruct a DepthDiff from raw Kafka message bytes.

    Note: bids / asks are stored as [[price, qty], ...] by json.dumps because
    tuples → lists in JSON.  We convert them back to List[Tuple[float, float]]
    here so the type matches the original dataclass definition.
    """
    d = json.loads(raw)
    d["bids"] = [tuple(x) for x in d["bids"]]
    d["asks"] = [tuple(x) for x in d["asks"]]
    return DepthDiff(**d)


def decode_trade(raw: bytes) -> Trade:
    """Reconstruct a Trade from raw Kafka message bytes."""
    return Trade(**json.loads(raw))
