from kafka import KafkaProducer, KafkaConsumer
import json
from app.core.config import settings

class KafkaClient:
    def __init__(self):
        self.producer = KafkaProducer(
            bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
            value_serializer=lambda v: json.dumps(v).encode('utf-8'),
            request_timeout_ms=5000,
            delivery_timeout_ms=10000
        )

    def produce(self, topic: str, message: dict):
        print(f"📡 [KAFKA PRODUCER] Sending message to topic {topic}: {message}")
        self.producer.send(topic, message)
        self.producer.flush()

    def get_consumer(self, topic: str, group_id: str):
        return KafkaConsumer(
            topic,
            bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS,
            group_id=group_id,
            value_deserializer=lambda m: json.loads(m.decode('utf-8')),
            auto_offset_reset='earliest',
            consumer_timeout_ms=1000
        )

kafka_client = KafkaClient()
