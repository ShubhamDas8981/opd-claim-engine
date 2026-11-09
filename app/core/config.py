from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional

class Settings(BaseSettings):
    # App
    APP_NAME: str = "OPD Digital Wallet AI"
    DEBUG: bool = True

    # Database
    MYSQL_URL: str = "mysql+pymysql://root:root_password@localhost:3306/opd_wallet"

    # Cache
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379

    # Kafka
    KAFKA_BOOTSTRAP_SERVERS: str = "localhost:9092"
    KAFKA_TOPIC_CLAIMS: str = "opd_claims_topic"
    KAFKA_TOPIC_TICKETS: str = "opd_tickets_topic"

    # AI / Groq
    GROQ_API_KEY: str = "your_groq_api_key_here"
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    #GROQ_MODEL: str = "openai/gpt-oss-20b"

    # Qdrant (RAG)
    QDRANT_URL: str = "http://localhost:6333"
    QDRANT_API_KEY: Optional[str] = None

    # HubSpot
    HUBSPOT_TOKEN: str = ""
    HUBSPOT_PIPELINE: str = "0"
    HUBSPOT_STAGE: str = "1"
    HUBSPOT_PRIORITY: str = "MEDIUM"
    HUBSPOT_PORTAL_ID: str = ""
    HUBSPOT_APP_HOST: str = "app.hubspot.com"

    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore"  # This tells Pydantic to ignore variables in .env not defined here
    )

settings = Settings()
