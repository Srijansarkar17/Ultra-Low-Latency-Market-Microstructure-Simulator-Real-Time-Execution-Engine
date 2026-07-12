"""
kafka_admin.py
--------------
One-shot topic creation script.

Run ONCE after `docker compose up -d` to create the three Kafka topics.
If KAFKA_CREATE_TOPICS in docker-compose.yml already created them, this
script is a no-op (it will print "topic already exists" warnings and exit 0).

Usage:
    python kafka_admin.py

Topics created:
    market.depth   — L2 order book diffs (DepthDiff events)
    market.trades  — matched trade events (Trade events)
    maker.quotes   — market maker quote snapshots (future use)
"""

from confluent_kafka.admin import AdminClient, NewTopic

KAFKA_CONF  = {"bootstrap.servers": "localhost:9092"}
NUM_REPLICAS = 1  # local single-broker setup

TOPICS = [
    NewTopic("market.depth",  num_partitions=1, replication_factor=NUM_REPLICAS),
    NewTopic("market.trades", num_partitions=1, replication_factor=NUM_REPLICAS),
    NewTopic("maker.quotes",  num_partitions=1, replication_factor=NUM_REPLICAS),
]


def main():
    admin = AdminClient(KAFKA_CONF)
    results = admin.create_topics(TOPICS)

    for topic, future in results.items():
        try:
            future.result()
            print(f"[ADMIN] Created topic: {topic}")
        except Exception as exc:
            # TOPIC_ALREADY_EXISTS is expected if docker-compose already created them
            print(f"[ADMIN] Topic '{topic}': {exc}")


if __name__ == "__main__":
    main()
