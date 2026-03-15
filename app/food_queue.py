"""Analysis queue worker for food photos."""
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal
from .food_analyzer import analyze_meal_photos
from .models import AnalysisQueue, Meal, MealPhoto
from .ws import ws_manager

logger = logging.getLogger(__name__)


def schedule_analysis(db: Session, meal: Meal):
    """Schedule or reschedule analysis for a meal (with debounce).

    Cancels any existing pending job for this meal and creates a new one
    with run_after = now + debounce_seconds.
    """
    # Cancel existing pending jobs for this meal
    db.query(AnalysisQueue).filter(
        AnalysisQueue.meal_id == meal.id,
        AnalysisQueue.status == "pending",
    ).update({"status": "cancelled"})

    # Create new job with debounce delay
    run_after = datetime.utcnow() + timedelta(seconds=settings.analysis_debounce_seconds)
    job = AnalysisQueue(
        meal_id=meal.id,
        run_after=run_after,
    )
    db.add(job)

    # Reset meal analysis status
    meal.analysis_status = "pending"
    meal.health_score = None
    meal.health_color = None
    meal.ai_comment = None
    meal.items_json = None
    meal.total_calories = None
    meal.total_protein_g = None
    meal.total_carbs_g = None
    meal.total_fat_g = None

    db.commit()
    logger.info(f"Scheduled analysis for meal {meal.id} (run after {run_after})")


async def process_queue():
    """Process one pending job from the analysis queue.

    Called periodically by the scheduler (every 10s).
    Uses SELECT ... FOR UPDATE SKIP LOCKED for concurrency safety.
    """
    db = SessionLocal()
    try:
        # Find next ready job
        job = (
            db.query(AnalysisQueue)
            .filter(
                AnalysisQueue.status == "pending",
                AnalysisQueue.run_after <= datetime.utcnow(),
            )
            .order_by(AnalysisQueue.run_after.asc())
            .with_for_update(skip_locked=True)
            .first()
        )

        if not job:
            return

        meal = db.query(Meal).filter(Meal.id == job.meal_id).first()
        if not meal:
            job.status = "failed"
            job.error_message = "Meal not found"
            db.commit()
            return

        # Get photo paths
        photos = db.query(MealPhoto).filter(MealPhoto.meal_id == meal.id).all()
        if not photos:
            job.status = "failed"
            job.error_message = "No photos found"
            db.commit()
            return

        photo_paths = [p.filename for p in photos]

        # Mark as processing
        job.status = "processing"
        meal.analysis_status = "analyzing"
        db.commit()

        logger.info(f"Processing analysis for meal {meal.id} ({len(photo_paths)} photos)")

        try:
            result = await analyze_meal_photos(
                photo_paths,
                is_cheat_day=meal.is_cheat_day,
                correction_note=meal.correction_note,
            )

            # Update meal with results
            meal.total_calories = result.get("total_calories")
            meal.total_protein_g = result.get("total_protein_g")
            meal.total_carbs_g = result.get("total_carbs_g")
            meal.total_fat_g = result.get("total_fat_g")
            meal.health_score = result.get("health_score")
            meal.health_color = result.get("health_color")
            meal.ai_comment = result.get("comment")
            meal.items_json = result.get("items")
            meal.analysis_status = "complete"

            # Update photo types from AI
            photo_types = result.get("photo_types", [])
            for i, photo in enumerate(photos):
                if i < len(photo_types):
                    photo.photo_type = photo_types[i]

            job.status = "complete"
            job.completed_at = datetime.utcnow()

            logger.info(f"Analysis complete for meal {meal.id}: "
                        f"score={meal.health_score}, calories={meal.total_calories}")

            # Notify connected clients
            await ws_manager.broadcast("meal_analyzed", {
                "meal_id": meal.id,
                "health_score": meal.health_score,
                "health_color": meal.health_color,
                "status": "complete",
            })

        except Exception as e:
            logger.error(f"Analysis failed for meal {meal.id}: {e}")
            job.retry_count += 1
            job.error_message = str(e)[:500]

            if job.retry_count >= job.max_retries:
                job.status = "failed"
                meal.analysis_status = "failed"
                logger.error(f"Meal {meal.id} analysis permanently failed after {job.max_retries} retries")
                await ws_manager.broadcast("meal_analyzed", {
                    "meal_id": meal.id, "status": "failed",
                })
            else:
                # Retry with backoff: 5min, 15min, 45min
                backoff = timedelta(minutes=5 * (3 ** (job.retry_count - 1)))
                job.run_after = datetime.utcnow() + backoff
                job.status = "pending"
                meal.analysis_status = "pending"
                logger.info(f"Retrying meal {meal.id} in {backoff} (attempt {job.retry_count})")

        db.commit()

    except Exception as e:
        logger.error(f"Queue worker error: {e}")
        db.rollback()
    finally:
        db.close()


def get_queue_status(db: Session) -> dict:
    """Get current queue status for admin dashboard."""
    pending = db.query(AnalysisQueue).filter(AnalysisQueue.status == "pending").count()
    processing = db.query(AnalysisQueue).filter(AnalysisQueue.status == "processing").count()
    failed = db.query(AnalysisQueue).filter(AnalysisQueue.status == "failed").count()

    return {
        "pending": pending,
        "processing": processing,
        "failed": failed,
        "has_errors": failed > 0,
    }


def retry_failed_jobs(db: Session) -> int:
    """Reset all failed jobs to pending for retry."""
    count = db.query(AnalysisQueue).filter(
        AnalysisQueue.status == "failed"
    ).update({
        "status": "pending",
        "retry_count": 0,
        "run_after": datetime.utcnow(),
        "error_message": None,
    })
    db.commit()

    # Also reset associated meals
    if count > 0:
        failed_meal_ids = [
            j.meal_id for j in
            db.query(AnalysisQueue).filter(AnalysisQueue.status == "pending").all()
        ]
        db.query(Meal).filter(Meal.id.in_(failed_meal_ids)).update(
            {"analysis_status": "pending"}, synchronize_session=False
        )
        db.commit()

    logger.info(f"Reset {count} failed jobs for retry")
    return count
