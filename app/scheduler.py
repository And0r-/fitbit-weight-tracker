"""Background scheduler for automatic Fitbit sync."""
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings
from .fitbit import fitbit_client
from .influxdb_client import weight_db

logger = logging.getLogger(__name__)


async def sync_weight_data(days: int = 7):
    """Sync weight data from Fitbit to InfluxDB."""
    logger.info(f"Starting weight sync (last {days} days)...")

    if not fitbit_client.is_authenticated():
        logger.warning("Fitbit not authenticated - skipping sync")
        return 0

    try:
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        entries = await fitbit_client.get_weight_range(start_date, end_date)
        logger.info(f"Fetched {len(entries)} weight entries from Fitbit")

        if entries:
            weight_db.write_weights_batch(entries)
            logger.info(f"Written {len(entries)} entries to InfluxDB")

        return len(entries)

    except Exception as e:
        logger.error(f"Sync failed: {e}")
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
            trigger=IntervalTrigger(hours=settings.sync_interval_hours),
            id="fitbit_sync",
            name="Fitbit Weight Sync",
            replace_existing=True,
        )

        self.scheduler.start()
        self._started = True
        logger.info(
            f"Scheduler started - syncing every {settings.sync_interval_hours} hours"
        )

    def stop(self):
        """Stop the scheduler."""
        if self._started:
            self.scheduler.shutdown()
            self._started = False
            logger.info("Scheduler stopped")

    async def run_now(self, days: int = 7):
        """Trigger immediate sync."""
        return await sync_weight_data(days)

    async def run_full_sync(self, days: int = 365):
        """Trigger full history sync."""
        return await sync_full_history(days)


# Singleton instance
sync_scheduler = SyncScheduler()
