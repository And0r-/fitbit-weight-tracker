"""Streak calculation for food tracking gamification."""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from .config import settings
from .models import Meal

logger = logging.getLogger(__name__)

TZ = ZoneInfo(settings.timezone)


def _get_food_today() -> str:
    """Get today's food day (06:00 boundary)."""
    now = datetime.now(TZ)
    if now.hour < settings.day_boundary_hour:
        now = now - timedelta(days=1)
    return now.strftime("%Y-%m-%d")


def _is_cheat_day(day_str: str) -> bool:
    """Check if a day string is a cheat day."""
    cheat_days = [d.strip().lower() for d in settings.cheat_day.split(",") if d.strip()]
    dt = datetime.strptime(day_str, "%Y-%m-%d")
    return dt.strftime("%A").lower() in cheat_days


def calculate_streak(db: Session) -> dict:
    """Calculate the current streak of days without a red meal.

    Rules:
    - Streak counts consecutive days with NO red (health_color='red') meals
    - Cheat days are completely ignored (don't break or extend the streak)
    - Only days with at least one logged meal count
    - Today counts if meals are logged

    Returns dict with:
    - days: current streak count
    - since: date the streak started (or None)
    - best: all-time best streak
    """
    # Get all completed meals ordered by day
    meals = (
        db.query(Meal)
        .filter(Meal.analysis_status == "complete")
        .order_by(Meal.day.desc())
        .all()
    )

    if not meals:
        return {"days": 0, "since": None, "best": 0}

    # Group meals by day
    days_data = {}
    for m in meals:
        if m.day not in days_data:
            days_data[m.day] = {"is_cheat": m.is_cheat_day, "has_red": False}
        if m.health_color == "red":
            days_data[m.day]["has_red"] = True

    # Walk backwards from today counting streak
    today = _get_food_today()
    current_streak = 0
    streak_since = None
    best_streak = 0
    running_streak = 0

    # Get all days sorted newest first
    all_days = sorted(days_data.keys(), reverse=True)

    for day in all_days:
        info = days_data[day]

        # Skip cheat days entirely
        if info["is_cheat"]:
            continue

        if not info["has_red"]:
            running_streak += 1
            streak_since = day
        else:
            # Streak broken
            if current_streak == 0:
                current_streak = running_streak
            best_streak = max(best_streak, running_streak)
            running_streak = 0
            streak_since = None

    # Final check
    if current_streak == 0:
        current_streak = running_streak
    best_streak = max(best_streak, running_streak)

    # If streak_since is still set, it's the start of current streak
    current_since = None
    if current_streak > 0:
        # Walk to find the actual start
        count = 0
        for day in all_days:
            if days_data[day]["is_cheat"]:
                continue
            if not days_data[day]["has_red"]:
                count += 1
                current_since = day
                if count >= current_streak:
                    break
            else:
                break

    return {
        "days": current_streak,
        "since": current_since,
        "best": best_streak,
    }
