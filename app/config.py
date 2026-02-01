"""Application configuration using Pydantic Settings."""
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Fitbit OAuth
    fitbit_client_id: str
    fitbit_client_secret: str
    fitbit_redirect_uri: str = "http://localhost:8080/callback"

    # InfluxDB
    influxdb_host: str = "influxdb"
    influxdb_port: int = 8086
    influxdb_db: str = "fitbit"

    # PostgreSQL
    database_url: str = "postgresql://fitbit:fitbit@postgres:5432/fitbit"

    # Admin token (imported to DB on first start)
    admin_token: str

    # Sync
    sync_interval_hours: int = 4

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
