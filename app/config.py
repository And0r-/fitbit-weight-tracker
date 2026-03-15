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
    sync_interval_minutes: int = 30

    # Oura OAuth
    oura_client_id: str = ""
    oura_client_secret: str = ""
    oura_redirect_uri: str = "http://localhost:8080/oura/callback"

    # Anthropic API (for food analysis)
    anthropic_api_key: str = ""

    # Food tracking
    cheat_day: str = "saturday"  # comma-separated: "saturday,sunday"
    day_boundary_hour: int = 6  # day ends at 06:00
    meal_group_hours: int = 2  # photos within 2h = same meal
    analysis_debounce_seconds: int = 3

    # UI: default period preset (1M, 3M, 6M, 1J, Alle)
    default_period: str = "3M"

    # Timezone for display
    timezone: str = "Europe/Zurich"

    # Security (set SECURE_COOKIES=false for local HTTP dev)
    secure_cookies: bool = True

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
