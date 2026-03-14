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

    # ============================================
    # Oura Ring Data
    # ============================================

    def _write_points_safe(self, points: list[dict]):
        """Write points with reconnect on failure."""
        if not points:
            return
        try:
            self.client.write_points(points)
        except Exception:
            self._reset_client()
            raise

    def write_sleep_batch(self, daily_sleep: list[dict], sleep_sessions: list[dict]):
        """Write sleep data to InfluxDB."""
        points = []

        # Daily sleep scores (one per day)
        for entry in daily_sleep:
            day = entry.get("day")
            if not day:
                continue
            fields = {"score": int(entry.get("score", 0))}
            contributors = entry.get("contributors", {})
            for key in ["deep_sleep", "efficiency", "latency", "rem_sleep",
                        "restfulness", "timing", "total_sleep"]:
                if key in contributors and contributors[key] is not None:
                    fields[f"contrib_{key}"] = int(contributors[key])
            points.append({
                "measurement": "oura_daily_sleep",
                "tags": {"source": "oura"},
                "time": f"{day}T00:00:00Z",
                "fields": fields,
            })

        # Sleep sessions (detailed per-session data)
        for entry in sleep_sessions:
            bedtime_start = entry.get("bedtime_start")
            if not bedtime_start:
                continue
            sleep_type = entry.get("type", "unknown")
            fields = {}
            for key in ["total_sleep_duration", "deep_sleep_duration",
                        "rem_sleep_duration", "light_sleep_duration"]:
                if key in entry and entry[key] is not None:
                    fields[key] = int(entry[key])
            if "efficiency" in entry and entry["efficiency"] is not None:
                fields["efficiency"] = int(entry["efficiency"])
            hr = entry.get("heart_rate", {})
            if isinstance(hr, dict) and "lowest" in hr:
                fields["lowest_heart_rate"] = int(hr["lowest"])
            if "average_heart_rate" in entry and entry["average_heart_rate"] is not None:
                fields["average_heart_rate"] = float(entry["average_heart_rate"])
            if "average_hrv" in entry and entry["average_hrv"] is not None:
                fields["average_hrv"] = float(entry["average_hrv"])

            if fields:
                points.append({
                    "measurement": "oura_sleep",
                    "tags": {"source": "oura", "type": sleep_type},
                    "time": bedtime_start,
                    "fields": fields,
                })

        self._write_points_safe(points)

    def write_readiness_batch(self, entries: list[dict]):
        """Write readiness data to InfluxDB."""
        points = []
        for entry in entries:
            day = entry.get("day")
            if not day:
                continue
            fields = {"score": int(entry.get("score", 0))}
            contributors = entry.get("contributors", {})
            for key in ["activity_balance", "body_temperature", "hrv_balance",
                        "previous_day_activity", "previous_night", "recovery_index",
                        "resting_heart_rate", "sleep_balance"]:
                if key in contributors and contributors[key] is not None:
                    fields[f"contrib_{key}"] = int(contributors[key])
            if "temperature_deviation" in entry and entry["temperature_deviation"] is not None:
                fields["temperature_deviation"] = float(entry["temperature_deviation"])
            points.append({
                "measurement": "oura_readiness",
                "tags": {"source": "oura"},
                "time": f"{day}T00:00:00Z",
                "fields": fields,
            })
        self._write_points_safe(points)

    def write_heart_rate_batch(self, entries: list[dict]):
        """Write heart rate data to InfluxDB."""
        points = []
        for entry in entries:
            ts = entry.get("timestamp")
            if not ts:
                continue
            fields = {"bpm": int(entry["bpm"])}
            points.append({
                "measurement": "oura_heart_rate",
                "tags": {"source": entry.get("source", "unknown")},
                "time": ts,
                "fields": fields,
            })
        self._write_points_safe(points)

    def write_stress_batch(self, entries: list[dict]):
        """Write stress data to InfluxDB."""
        points = []
        for entry in entries:
            day = entry.get("day")
            if not day:
                continue
            fields = {}
            if "stress_high" in entry and entry["stress_high"] is not None:
                fields["stress_high"] = int(entry["stress_high"])
            if "recovery_high" in entry and entry["recovery_high"] is not None:
                fields["recovery_high"] = int(entry["recovery_high"])
            if "day_summary" in entry and entry["day_summary"] is not None:
                fields["day_summary"] = str(entry["day_summary"])
            if fields:
                points.append({
                    "measurement": "oura_stress",
                    "tags": {"source": "oura"},
                    "time": f"{day}T00:00:00Z",
                    "fields": fields,
                })
        self._write_points_safe(points)

    def get_sleep_history(self, days: int = 3) -> dict:
        """Get sleep scores and sessions for the last N days."""
        daily_q = f"""
            SELECT * FROM oura_daily_sleep
            WHERE time > now() - {days}d
            ORDER BY time ASC
        """
        sessions_q = f"""
            SELECT * FROM oura_sleep
            WHERE time > now() - {days}d
            ORDER BY time ASC
        """
        daily_result = self._query(daily_q)
        sessions_result = self._query(sessions_q)
        return {
            "daily": list(daily_result.get_points()),
            "sessions": list(sessions_result.get_points()),
        }

    def get_readiness_history(self, days: int = 3) -> list[dict]:
        """Get readiness data for the last N days."""
        query = f"""
            SELECT * FROM oura_readiness
            WHERE time > now() - {days}d
            ORDER BY time ASC
        """
        result = self._query(query)
        return list(result.get_points())

    def get_heart_rate_history(self, hours: int = 24) -> list[dict]:
        """Get heart rate data for the last N hours."""
        query = f"""
            SELECT bpm, source FROM oura_heart_rate
            WHERE time > now() - {hours}h
            ORDER BY time ASC
        """
        result = self._query(query)
        return list(result.get_points())

    def get_stress_history(self, days: int = 1) -> list[dict]:
        """Get stress data for the last N days."""
        query = f"""
            SELECT * FROM oura_stress
            WHERE time > now() - {days}d
            ORDER BY time ASC
        """
        result = self._query(query)
        return list(result.get_points())

    def write_workouts_batch(self, entries: list[dict]):
        """Write workout data to InfluxDB."""
        points = []
        for entry in entries:
            start = entry.get("start_datetime")
            if not start:
                continue
            fields = {}
            if "calories" in entry and entry["calories"] is not None:
                fields["calories"] = float(entry["calories"])
            if "intensity" in entry and entry["intensity"] is not None:
                fields["intensity"] = str(entry["intensity"])
            if "distance" in entry and entry["distance"] is not None:
                fields["distance"] = float(entry["distance"])
            activity = entry.get("activity", "unknown")
            # Store activity as field too so we can query it
            fields["activity"] = str(activity)
            if fields:
                points.append({
                    "measurement": "oura_workout",
                    "tags": {"source": "oura", "activity": str(activity)},
                    "time": start,
                    "fields": fields,
                })
        self._write_points_safe(points)

    def get_workout_history(self, days: int = 7) -> list[dict]:
        """Get workout data for the last N days."""
        query = f"""
            SELECT * FROM oura_workout
            WHERE time > now() - {days}d
            ORDER BY time ASC
        """
        result = self._query(query)
        return list(result.get_points())


# Singleton instance
weight_db = WeightDatabase()
