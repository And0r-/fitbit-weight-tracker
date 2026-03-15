"""Background scheduler for automatic Fitbit and Oura sync."""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings
from .fitbit import fitbit_client
from .influxdb_client import weight_db
from .oura import oura_client

logger = logging.getLogger(__name__)


# ============================================
# Fitbit Sync
# ============================================


async def sync_weight_data(days: int = 7):
    """Sync weight data from Fitbit to InfluxDB."""
    logger.info(f"Starting weight sync (last {days} days)...")

    if not fitbit_client.is_authenticated():
        logger.warning("Fitbit not authenticated - skipping sync")
        return 0

    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        logger.info(f"Fetching {start_date.date()} to {end_date.date()}...")

        entries = await fitbit_client.get_weight_range(start_date, end_date)
        logger.info(f"Fetched {len(entries)} weight entries from Fitbit")

        if entries:
            weight_db.write_weights_batch(entries)
            logger.info(f"Written {len(entries)} entries to InfluxDB")
        else:
            logger.info("No new entries in this period")

        return len(entries)

    except Exception as e:
        logger.error(f"Sync failed: {e}", exc_info=True)
        return 0


async def sync_full_history(days: int = 365):
    """Sync full weight history in chunks (Fitbit API allows max 31 days per request)."""
    logger.info(f"Starting FULL history sync ({days} days)...")

    if not fitbit_client.is_authenticated():
        logger.warning("Fitbit not authenticated - skipping sync")
        return 0

    total_entries = 0
    chunk_size = 30  # Fitbit allows max 31 days

    try:
        end_date = datetime.now()

        # Go back in chunks
        while days > 0:
            chunk_days = min(chunk_size, days)
            start_date = end_date - timedelta(days=chunk_days)

            logger.info(f"Fetching {start_date.date()} to {end_date.date()}...")

            entries = await fitbit_client.get_weight_range(start_date, end_date)

            if entries:
                weight_db.write_weights_batch(entries)
                total_entries += len(entries)
                logger.info(f"  -> {len(entries)} entries")

            # Move window back
            end_date = start_date
            days -= chunk_days

        logger.info(f"Full sync complete: {total_entries} total entries")
        return total_entries

    except Exception as e:
        logger.error(f"Full sync failed: {e}")
        return total_entries


# ============================================
# Oura Sync
# ============================================


async def sync_oura_data(days: int = 3):
    """Sync Oura Ring data to InfluxDB."""
    logger.info(f"Starting Oura sync (last {days} days)...")

    if not oura_client.is_authenticated():
        logger.warning("Oura not authenticated - skipping sync")
        return

    start_date = oura_client._local_date(days)
    end_date = oura_client._local_today()

    try:
        # Sleep
        daily_sleep = await oura_client.get_daily_sleep(start_date, end_date)
        sleep_sessions = await oura_client.get_sleep(start_date, end_date)
        weight_db.write_sleep_batch(daily_sleep, sleep_sessions)
        logger.info(f"Oura sleep: {len(daily_sleep)} daily scores, {len(sleep_sessions)} sessions")
    except Exception as e:
        logger.error(f"Oura sleep sync failed: {e}")

    try:
        # Readiness
        readiness = await oura_client.get_daily_readiness(start_date, end_date)
        weight_db.write_readiness_batch(readiness)
        logger.info(f"Oura readiness: {len(readiness)} entries")
    except Exception as e:
        logger.error(f"Oura readiness sync failed: {e}")

    try:
        # Heart rate
        heart_rate = await oura_client.get_heart_rate(start_date, end_date)
        weight_db.write_heart_rate_batch(heart_rate)
        logger.info(f"Oura heart rate: {len(heart_rate)} entries")
    except Exception as e:
        logger.error(f"Oura heart rate sync failed: {e}")

    try:
        # Stress
        stress = await oura_client.get_daily_stress(start_date, end_date)
        weight_db.write_stress_batch(stress)
        logger.info(f"Oura stress: {len(stress)} entries")
    except Exception as e:
        logger.error(f"Oura stress sync failed: {e}")

    try:
        # Workouts
        workouts = await oura_client.get_workouts(start_date, end_date)
        weight_db.write_workouts_batch(workouts)
        logger.info(f"Oura workouts: {len(workouts)} entries")
    except Exception as e:
        logger.error(f"Oura workouts sync failed: {e}")

    try:
        # SpO2
        spo2 = await oura_client.get_daily_spo2(start_date, end_date)
        weight_db.write_spo2_batch(spo2)
        logger.info(f"Oura SpO2: {len(spo2)} entries")
    except Exception as e:
        logger.error(f"Oura SpO2 sync failed: {e}")


async def sync_oura_full(days: int = 30):
    """Sync full Oura history."""
    logger.info(f"Starting FULL Oura sync ({days} days)...")
    await sync_oura_data(days)
    logger.info("Full Oura sync complete")


class SyncScheduler:
    """Manages background sync jobs."""

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self._started = False

    def start(self):
        """Start the scheduler with configured interval."""
        if self._started:
            return

        self.scheduler.add_job(
            sync_weight_data,
            trigger=IntervalTrigger(minutes=settings.sync_interval_minutes),
            id="fitbit_sync",
            name="Fitbit Weight Sync",
            replace_existing=True,
        )

        self.scheduler.add_job(
            sync_oura_data,
            trigger=IntervalTrigger(minutes=settings.sync_interval_minutes),
            id="oura_sync",
            name="Oura Ring Sync",
            replace_existing=True,
        )

        self.scheduler.start()
        self._started = True
        logger.info(
            f"Scheduler started - syncing every {settings.sync_interval_minutes} minutes"
        )

    def stop(self):
        """Stop the scheduler."""
        if self._started:
            self.scheduler.shutdown()
            self._started = False
            logger.info("Scheduler stopped")

    async def run_now(self, days: int = 7):
        """Trigger immediate Fitbit sync."""
        return await sync_weight_data(days)

    async def run_full_sync(self, days: int = 365):
        """Trigger full Fitbit history sync."""
        return await sync_full_history(days)

    async def run_oura_now(self, days: int = 3):
        """Trigger immediate Oura sync."""
        return await sync_oura_data(days)

    async def run_oura_full(self, days: int = 30):
        """Trigger full Oura history sync."""
        return await sync_oura_full(days)


# Singleton instance
sync_scheduler = SyncScheduler()
