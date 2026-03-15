"""Food upload, meal grouping, and day boundary logic."""
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image
from PIL.ExifTags import Base as ExifBase
from sqlalchemy.orm import Session

from .config import settings
from .models import Meal, MealPhoto

logger = logging.getLogger(__name__)

FOOD_DIR = Path("/app/data/food")
ORIGINALS_DIR = FOOD_DIR / "originals"
DISPLAY_DIR = FOOD_DIR / "display"
THUMB_DIR = FOOD_DIR / "thumbs"
TZ = ZoneInfo(settings.timezone)


def _ensure_dirs():
    FOOD_DIR.mkdir(parents=True, exist_ok=True)
    ORIGINALS_DIR.mkdir(parents=True, exist_ok=True)
    DISPLAY_DIR.mkdir(parents=True, exist_ok=True)
    THUMB_DIR.mkdir(parents=True, exist_ok=True)


def _extract_exif_datetime(filepath: Path) -> datetime | None:
    """Extract photo timestamp from EXIF data."""
    try:
        img = Image.open(filepath)
        exif = img._getexif()
        if not exif:
            return None

        # Try DateTimeOriginal (36867), then DateTime (306)
        for tag_id in (36867, 306):
            val = exif.get(tag_id)
            if val:
                try:
                    dt = datetime.strptime(val, "%Y:%m:%d %H:%M:%S")
                    return dt.replace(tzinfo=TZ)
                except ValueError:
                    continue
        return None
    except Exception:
        return None


def _strip_exif_and_resize(filepath: Path, output_path: Path, max_dim: int, quality: int = 85):
    """Resize image, strip EXIF metadata, auto-rotate, and save as JPEG."""
    try:
        from PIL import ImageOps
        img = Image.open(filepath)
        # Auto-rotate based on EXIF orientation, then strip metadata
        img = ImageOps.exif_transpose(img)
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        # Save without EXIF (creating new image strips all metadata)
        clean = Image.new("RGB", img.size)
        clean.paste(img)
        clean.save(output_path, "JPEG", quality=quality)
    except Exception as e:
        logger.error(f"Image processing failed for {filepath.name}: {e}")


def compute_food_day(dt: datetime) -> str:
    """Compute the 'food day' for a datetime using the 06:00 boundary.

    Before 06:00 = previous day. After 06:00 = current day.
    """
    local = dt.astimezone(TZ) if dt.tzinfo else dt.replace(tzinfo=TZ)
    if local.hour < settings.day_boundary_hour:
        local = local - timedelta(days=1)
    return local.strftime("%Y-%m-%d")


def is_cheat_day(day: str) -> bool:
    """Check if a day string falls on a configured cheat day."""
    cheat_days = [d.strip().lower() for d in settings.cheat_day.split(",") if d.strip()]
    dt = datetime.strptime(day, "%Y-%m-%d")
    weekday_name = dt.strftime("%A").lower()
    return weekday_name in cheat_days


def find_or_create_meal(db: Session, photo_taken_at: datetime) -> Meal:
    """Find an existing meal within the grouping window, or create a new one.

    Photos within MEAL_GROUP_HOURS of the first photo in a meal = same meal.
    """
    day = compute_food_day(photo_taken_at)
    window = timedelta(hours=settings.meal_group_hours)

    # Find meals on this day where the photo fits in the time window
    meals = db.query(Meal).filter(Meal.day == day).all()

    for meal in meals:
        # Check if photo is within 2h of the first photo in this meal
        # Normalize both to aware datetimes for comparison
        meal_time = meal.first_photo_at.replace(tzinfo=TZ) if meal.first_photo_at.tzinfo is None else meal.first_photo_at
        photo_time = photo_taken_at.replace(tzinfo=TZ) if photo_taken_at.tzinfo is None else photo_taken_at
        if abs((photo_time - meal_time).total_seconds()) <= window.total_seconds():
            return meal

    # No matching meal — create new one
    # Store as naive datetime (DB consistency)
    naive_time = photo_taken_at.replace(tzinfo=None) if photo_taken_at.tzinfo else photo_taken_at
    meal = Meal(
        day=day,
        first_photo_at=naive_time,
        is_cheat_day=is_cheat_day(day),
    )
    db.add(meal)
    db.flush()
    logger.info(f"Created new meal {meal.id} for day {day}")
    return meal


def save_uploaded_photo(db: Session, file_data: bytes, original_filename: str) -> tuple[Meal, MealPhoto]:
    """Save an uploaded photo and assign it to a meal.

    Returns (meal, photo) tuple.
    """
    _ensure_dirs()

    # Save original (with EXIF, for re-analysis)
    base_name = str(uuid.uuid4())
    ext = Path(original_filename).suffix.lower() or ".jpg"
    original_path = ORIGINALS_DIR / f"{base_name}{ext}"
    original_path.write_bytes(file_data)

    # Extract timestamp from EXIF before stripping
    photo_taken_at = _extract_exif_datetime(original_path)
    if photo_taken_at:
        logger.info(f"EXIF timestamp: {photo_taken_at}")
    else:
        photo_taken_at = datetime.now(TZ)
        logger.info(f"No EXIF timestamp, using upload time: {photo_taken_at}")

    # Generate display version (1200px, no EXIF)
    display_filename = f"{base_name}.jpg"
    _strip_exif_and_resize(original_path, DISPLAY_DIR / display_filename, max_dim=1200, quality=85)

    # Generate thumbnail (400px, no EXIF)
    thumb_filename = f"{base_name}.jpg"
    _strip_exif_and_resize(original_path, THUMB_DIR / thumb_filename, max_dim=400, quality=80)

    # filename points to the original (for AI analysis)
    filename = f"originals/{base_name}{ext}"

    # Find or create meal
    meal = find_or_create_meal(db, photo_taken_at)

    # Update first_photo_at if this photo is earlier
    naive_taken = photo_taken_at.replace(tzinfo=None) if photo_taken_at.tzinfo else photo_taken_at
    if naive_taken < meal.first_photo_at:
        meal.first_photo_at = naive_taken

    # Create photo record (store as naive local time)
    photo = MealPhoto(
        meal_id=meal.id,
        filename=filename,
        original_filename=original_filename,
        photo_taken_at=naive_taken,
        display_path=f"display/{display_filename}",
        thumbnail_path=f"thumbs/{thumb_filename}",
    )
    db.add(photo)
    db.flush()  # Ensure photo.id is set

    return meal, photo
