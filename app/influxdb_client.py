"""InfluxDB client for weight data storage."""
from datetime import datetime, timedelta

from influxdb import InfluxDBClient

from .config import settings


class WeightDatabase:
    """InfluxDB client for weight timeseries data."""

    def __init__(self):
        self._client: InfluxDBClient | None = None

    @property
    def client(self) -> InfluxDBClient:
        """Lazy connection to InfluxDB with reconnect on failure."""
        if self._client is None:
            client = InfluxDBClient(
                host=settings.influxdb_host,
                port=settings.influxdb_port,
                database=settings.influxdb_db,
            )
            # Ensure database exists - only set _client after success
            client.create_database(settings.influxdb_db)
            self._client = client
        return self._client

    def _reset_client(self):
        """Reset client so next access creates a fresh connection."""
        try:
            if self._client:
                self._client.close()
        except Exception:
            pass
        self._client = None

    def write_weight(
        self,
        timestamp: datetime,
        weight: float,
        bmi: float | None = None,
        fat: float | None = None,
        source: str = "Aria",
    ):
        """Write a weight measurement to InfluxDB."""
        fields = {"weight": float(weight)}
        if bmi is not None:
            fields["bmi"] = float(bmi)
        if fat is not None:
            fields["fat"] = float(fat)

        point = {
            "measurement": "weight",
            "tags": {"source": source},
            "time": timestamp.isoformat(),
            "fields": fields,
        }

        self.client.write_points([point])

    def write_weights_batch(self, entries: list[dict]):
        """Write multiple weight entries efficiently."""
        points = []
        for entry in entries:
            # Parse date and time from Fitbit format
            dt = datetime.strptime(
                f"{entry['date']} {entry['time']}", "%Y-%m-%d %H:%M:%S"
            )

            fields = {"weight": float(entry["weight"])}
            if "bmi" in entry:
                fields["bmi"] = float(entry["bmi"])
            if "fat" in entry:
                fields["fat"] = float(entry["fat"])

            points.append(
                {
                    "measurement": "weight",
                    "tags": {"source": entry.get("source", "Unknown")},
                    "time": dt.isoformat(),
                    "fields": fields,
                }
            )

        if points:
            try:
                self.client.write_points(points)
            except Exception:
                self._reset_client()
                raise

    def _query(self, query: str):
        """Execute InfluxDB query with reconnect on failure."""
        try:
            return self.client.query(query)
        except Exception:
            self._reset_client()
            raise

    def get_weight_history(self, days: int = 30) -> list[dict]:
        """Get weight history for the last N days."""
        query = f"""
            SELECT weight, bmi, fat, source
            FROM weight
            WHERE time > now() - {days}d
            ORDER BY time ASC
        """
        result = self._query(query)
        points = list(result.get_points())

        return [
            {
                "time": p["time"],
                "weight": p["weight"],
                "bmi": p.get("bmi"),
                "fat": p.get("fat"),
                "source": p.get("source"),
            }
            for p in points
        ]

    def get_latest_weight(self) -> dict | None:
        """Get the most recent weight entry."""
        query = """
            SELECT weight, bmi, fat, source
            FROM weight
            ORDER BY time DESC
            LIMIT 1
        """
        result = self._query(query)
        points = list(result.get_points())
        if points:
            p = points[0]
            return {
                "time": p["time"],
                "weight": p["weight"],
                "bmi": p.get("bmi"),
                "fat": p.get("fat"),
                "source": p.get("source"),
            }
        return None

    def get_weight_range(self, from_date: str, to_date: str) -> list[dict]:
        """Get weight history for a specific date range."""
        query = f"""
            SELECT weight, bmi, fat, source
            FROM weight
            WHERE time >= '{from_date}T00:00:00Z' AND time <= '{to_date}T23:59:59Z'
            ORDER BY time ASC
        """
        result = self._query(query)
        points = list(result.get_points())

        return [
            {
                "time": p["time"],
                "weight": p["weight"],
                "bmi": p.get("bmi"),
                "fat": p.get("fat"),
                "source": p.get("source"),
            }
            for p in points
        ]

    def _calculate_stats(self, history: list[dict]) -> dict:
        """Calculate statistics from history data."""
        if not history:
            return {
                "current": None,
                "min": None,
                "max": None,
                "avg": None,
                "trend_per_week": None,
                "entries": 0,
            }

        weights = [h["weight"] for h in history]
        current = weights[-1] if weights else None

        # Calculate trend (kg per week)
        trend = None
        if len(weights) >= 2:
            first = weights[0]
            last = weights[-1]
            first_time = datetime.fromisoformat(history[0]["time"].replace("Z", ""))
            last_time = datetime.fromisoformat(history[-1]["time"].replace("Z", ""))
            days_diff = (last_time - first_time).days
            if days_diff > 0:
                trend = ((last - first) / days_diff) * 7  # per week

        return {
            "current": current,
            "min": min(weights),
            "max": max(weights),
            "avg": round(sum(weights) / len(weights), 2),
            "trend_per_week": round(trend, 2) if trend else None,
            "entries": len(history),
        }

    def get_stats(self, days: int = 30) -> dict:
        """Calculate statistics for the last N days."""
        history = self.get_weight_history(days)
        return self._calculate_stats(history)

    def get_stats_range(self, from_date: str, to_date: str) -> dict:
        """Calculate statistics for a specific date range."""
        history = self.get_weight_range(from_date, to_date)
        return self._calculate_stats(history)


# Singleton instance
weight_db = WeightDatabase()
