"""Compact health summary endpoint for AI assistants."""
import logging
from datetime import datetime, timedelta
from statistics import mean
from zoneinfo import ZoneInfo

from .config import settings
from .influxdb_client import weight_db

logger = logging.getLogger(__name__)

TZ = ZoneInfo(settings.timezone)


def _to_local(utc_str: str) -> str:
    """Convert UTC ISO string to local timezone string."""
    if not utc_str:
        return utc_str
    s = utc_str.replace("Z", "+00:00")
    if "." in s:
        dot = s.index(".")
        rest = ""
        for c in s[dot + 1:]:
            if c in "+-":
                rest = s[s.index(c, dot):]
                break
        s = s[:dot] + rest
    try:
        dt = datetime.fromisoformat(s)
        local = dt.astimezone(TZ)
        return local.strftime("%Y-%m-%dT%H:%M:%S%z")
    except Exception:
        return utc_str


def _local_now() -> datetime:
    return datetime.now(TZ)


def _compute_trend(values: list) -> str:
    """Simple linear regression trend."""
    vals = [v for v in values if v is not None]
    if len(vals) < 3:
        return "insufficient_data"
    n = len(vals)
    x_mean = (n - 1) / 2
    y_mean = mean(vals)
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(vals))
    den = sum((i - x_mean) ** 2 for i in range(n))
    slope = num / den if den else 0
    rel = slope / y_mean * 100 if y_mean else 0
    if rel > 2:
        return "improving"
    elif rel < -2:
        return "declining"
    return "stable"


def _safe_mean(vals: list) -> float | None:
    clean = [v for v in vals if v is not None]
    return round(mean(clean), 1) if clean else None


def _build_weight_summary() -> dict:
    """Weight: current + goal + 14 days daily + monthly averages."""
    # Last 14 days
    recent = weight_db.get_weight_history(14)
    # Last 12 months for monthly averages
    all_data = weight_db.get_weight_history(365)

    # Goal from stats
    stats_90d = weight_db.get_stats(90)

    # Group by day (last entry per day)
    daily = {}
    for p in recent:
        day = _to_local(p["time"])[:10]
        daily[day] = p["weight"]
    daily_list = [{"date": d, "kg": round(w, 1)} for d, w in sorted(daily.items())]

    # Monthly averages (older than 14 days)
    cutoff = (_local_now() - timedelta(days=14)).strftime("%Y-%m-%d")
    monthly = {}
    for p in all_data:
        day = _to_local(p["time"])[:10]
        if day >= cutoff:
            continue
        month = day[:7]
        if month not in monthly:
            monthly[month] = []
        monthly[month].append(p["weight"])

    monthly_list = []
    for m in sorted(monthly.keys()):
        vals = monthly[m]
        monthly_list.append({
            "month": m,
            "avg": round(mean(vals), 1),
            "min": round(min(vals), 1),
            "max": round(max(vals), 1),
        })

    current = stats_90d.get("current")
    trend = stats_90d.get("trend_per_week")

    return {
        "current_kg": current,
        "trend_per_week_kg": trend,
        "last_14_days": daily_list,
        "monthly_avg": monthly_list,
    }


def _build_heart_rate_summary() -> dict:
    """HR: last 2h samples, hourly avg 2-24h, daily summary 7d."""
    # Last 2h raw samples
    samples_2h = weight_db.get_heart_rate_history(2)
    # Last 24h for hourly buckets
    samples_24h = weight_db.get_heart_rate_history(24)
    # Last 7d for daily summary
    samples_7d = weight_db.get_heart_rate_history(168)

    # Delta-encode recent 2h samples (compact for AI context)
    recent_encoded = {}
    if samples_2h:
        first_local = _to_local(samples_2h[0]["time"])
        base_time = first_local[11:19] if first_local and "T" in first_local else "00:00:00"
        tz_offset = first_local[19:] if len(first_local) > 19 else ""
        h, m, s = int(base_time[:2]), int(base_time[3:5]), int(base_time[6:8])
        base_secs = h * 3600 + m * 60 + s

        encoded = []
        last_src = None
        for item in samples_2h:
            local = _to_local(item["time"])
            lt = local[11:19] if local and "T" in local else "00:00:00"
            lh, lm, ls = int(lt[:2]), int(lt[3:5]), int(lt[6:8])
            offset = (lh * 3600 + lm * 60 + ls) - base_secs
            if offset < 0:
                offset += 86400
            src = (item.get("source") or "?")[0]
            bpm = item["bpm"]
            if src != last_src:
                encoded.append(f"{offset}:{bpm}{src}")
                last_src = src
            else:
                encoded.append(f"{offset}:{bpm}")

        recent_encoded = {
            "base": f"{base_time}{tz_offset}",
            "count": len(encoded),
            "format": "offset_sec:bpm[source: a=awake r=rest s=sleep w=workout]",
            "d": " ".join(encoded),
        }

    # Hourly averages 2-24h ago
    cutoff_2h = (_local_now() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    hourly_buckets = {}
    for item in samples_24h:
        local = _to_local(item["time"])
        if not local or local >= _to_local(samples_2h[0]["time"]) if samples_2h else False:
            continue
        hour_key = local[:13]
        if hour_key not in hourly_buckets:
            hourly_buckets[hour_key] = []
        hourly_buckets[hour_key].append(item["bpm"])

    hourly_list = []
    for hour in sorted(hourly_buckets.keys()):
        vals = hourly_buckets[hour]
        hourly_list.append({
            "hour": hour + ":00",
            "avg": round(mean(vals), 1),
            "min": min(vals),
            "max": max(vals),
        })

    # Daily summary last 7 days
    daily_buckets = {}
    for item in samples_7d:
        local = _to_local(item["time"])
        day = local[:10] if local else ""
        source = item.get("source", "")
        if day not in daily_buckets:
            daily_buckets[day] = {"all": [], "rest": []}
        daily_buckets[day]["all"].append(item["bpm"])
        if source in ("rest", "sleep"):
            daily_buckets[day]["rest"].append(item["bpm"])

    daily_list = []
    for day in sorted(daily_buckets.keys()):
        b = daily_buckets[day]
        daily_list.append({
            "date": day,
            "avg": round(mean(b["all"]), 1),
            "min": min(b["all"]),
            "max": max(b["all"]),
            "resting_avg": round(mean(b["rest"]), 1) if b["rest"] else None,
        })

    return {
        "latest_bpm": samples_2h[-1]["bpm"] if samples_2h else None,
        "samples_2h": recent_encoded,
        "hourly_avg_2_24h": hourly_list,
        "daily_7d": daily_list,
    }


def _build_sleep_summary() -> dict:
    """Sleep: last night detail, 7 days scores, 30d average."""
    sleep_data = weight_db.get_sleep_history(30)
    daily = sleep_data.get("daily", [])
    sessions = sleep_data.get("sessions", [])

    # Format a sleep session
    def _fmt_session(s):
        total = s.get("total_sleep_duration", 0) or 0
        deep = s.get("deep_sleep_duration", 0) or 0
        rem = s.get("rem_sleep_duration", 0) or 0
        light = s.get("light_sleep_duration", 0) or 0
        return {
            "date": _to_local(s["time"])[:10],
            "type": s.get("type", "unknown"),
            "total_min": round(total / 60),
            "deep_min": round(deep / 60),
            "rem_min": round(rem / 60),
            "light_min": round(light / 60),
            "efficiency": s.get("efficiency"),
            "lowest_hr": s.get("lowest_heart_rate"),
            "avg_hr": s.get("average_heart_rate"),
            "avg_hrv": round(s["average_hrv"], 1) if s.get("average_hrv") else None,
        }

    # Last night (most recent long_sleep session)
    long_sessions = [s for s in sessions if s.get("type") == "long_sleep"]
    last_night = _fmt_session(long_sessions[-1]) if long_sessions else None

    # Recent naps (short_sleep, late_nap, rest) from last 3 days
    nap_types = ("short_sleep", "late_nap", "rest")
    cutoff_3d = (_local_now() - timedelta(days=3)).strftime("%Y-%m-%d")
    recent_naps = []
    for s in sessions:
        if s.get("type") in nap_types:
            day = _to_local(s["time"])[:10]
            if day >= cutoff_3d:
                recent_naps.append(_fmt_session(s))

    # Last 7 days scores
    scores_7d = []
    scores_all = []
    for d in daily:
        day = _to_local(d["time"])[:10]
        score = d.get("score", 0)
        scores_all.append(score)
        scores_7d.append({"date": day, "score": score})
    scores_7d = scores_7d[-7:]

    return {
        "last_night": last_night,
        "recent_naps": recent_naps if recent_naps else None,
        "last_7_days": scores_7d,
        "avg_score_30d": _safe_mean(scores_all),
        "trend_30d": _compute_trend(scores_all),
    }


def _build_readiness_summary() -> dict:
    """Readiness: today + 7 days scores + 30d average."""
    data = weight_db.get_readiness_history(30)

    scores_all = []
    entries_7d = []
    today_detail = None

    for entry in data:
        day = _to_local(entry["time"])[:10]
        score = entry.get("score", 0)
        scores_all.append(score)
        entries_7d.append({"date": day, "score": score})

        # Build contributors for today/latest
        today_detail = {
            "date": day,
            "score": score,
            "temperature_deviation": entry.get("temperature_deviation"),
        }

    return {
        "today": today_detail,
        "last_7_days": entries_7d[-7:],
        "avg_score_30d": _safe_mean(scores_all),
        "trend_30d": _compute_trend(scores_all),
    }


def _build_stress_summary() -> dict:
    """Stress: today + 7 days."""
    data = weight_db.get_stress_history(7)

    entries = []
    for entry in data:
        day = _to_local(entry["time"])[:10]
        stress_min = round(entry.get("stress_high", 0) / 60, 1) if entry.get("stress_high") else 0
        recovery_min = round(entry.get("recovery_high", 0) / 60, 1) if entry.get("recovery_high") else 0
        entries.append({
            "date": day,
            "summary": entry.get("day_summary", "unknown"),
            "stress_high_min": stress_min,
            "recovery_high_min": recovery_min,
        })

    return {
        "today": entries[-1] if entries else None,
        "last_7_days": entries,
    }


def _build_workout_summary() -> dict:
    """Workouts: last 7 days."""
    data = weight_db.get_workout_history(7)

    workouts = []
    for entry in data:
        local_time = _to_local(entry["time"])
        workouts.append({
            "date": local_time[:10] if local_time else "",
            "time": local_time[11:16] if local_time and "T" in local_time else "",
            "activity": entry.get("activity", "unknown"),
            "calories": entry.get("calories"),
            "intensity": entry.get("intensity"),
        })

    return {
        "count_7d": len(workouts),
        "workouts": workouts,
    }


async def build_health_summary(goal_weight: float | None = None) -> dict:
    """Build compact health summary for AI consumption."""
    now = _local_now()

    result = {
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "timezone": settings.timezone,
    }

    # Weight
    try:
        weight = _build_weight_summary()
        if goal_weight:
            weight["goal_kg"] = goal_weight
            if weight.get("current_kg"):
                weight["remaining_kg"] = round(weight["current_kg"] - goal_weight, 1)
        result["weight"] = weight
    except Exception as e:
        logger.error(f"Summary weight failed: {e}")
        result["weight"] = {"error": str(e)}

    # Heart rate
    try:
        result["heart_rate"] = _build_heart_rate_summary()
    except Exception as e:
        logger.error(f"Summary HR failed: {e}")
        result["heart_rate"] = {"error": str(e)}

    # Sleep
    try:
        result["sleep"] = _build_sleep_summary()
    except Exception as e:
        logger.error(f"Summary sleep failed: {e}")
        result["sleep"] = {"error": str(e)}

    # Readiness
    try:
        result["readiness"] = _build_readiness_summary()
    except Exception as e:
        logger.error(f"Summary readiness failed: {e}")
        result["readiness"] = {"error": str(e)}

    # Stress
    try:
        result["stress"] = _build_stress_summary()
    except Exception as e:
        logger.error(f"Summary stress failed: {e}")
        result["stress"] = {"error": str(e)}

    # SpO2
    try:
        spo2_data = weight_db.get_spo2_history(7)
        spo2_entries = []
        for entry in spo2_data:
            day = _to_local(entry["time"])[:10]
            spo2_entries.append({"date": day, "avg_pct": entry.get("average")})
        result["spo2"] = {
            "latest": spo2_entries[-1] if spo2_entries else None,
            "last_7_days": spo2_entries,
        }
    except Exception as e:
        logger.error(f"Summary SpO2 failed: {e}")
        result["spo2"] = {"error": str(e)}

    # Workouts
    try:
        result["workouts"] = _build_workout_summary()
    except Exception as e:
        logger.error(f"Summary workouts failed: {e}")
        result["workouts"] = {"error": str(e)}

    return result
