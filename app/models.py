"""Database models for PostgreSQL."""
from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class ShareToken(Base):
    """Share tokens for friends to view weight data."""

    __tablename__ = "share_tokens"

    id = Column(Integer, primary_key=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(100), nullable=False)  # e.g. "Max", "Anna"
    is_admin = Column(Boolean, default=False)  # Admin tokens can manage other tokens
    can_view_oura = Column(Boolean, default=False)  # Access to Oura Ring data
    can_view_food = Column(Boolean, default=False)  # Access to food diary
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True)

    # Relationship to access logs
    access_logs = relationship("AccessLog", back_populates="share_token")


class AccessLog(Base):
    """Log of token usage for tracking."""

    __tablename__ = "access_logs"

    id = Column(Integer, primary_key=True)
    token_id = Column(Integer, ForeignKey("share_tokens.id"), nullable=False)
    accessed_at = Column(DateTime, default=datetime.utcnow)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)

    share_token = relationship("ShareToken", back_populates="access_logs")


class Meal(Base):
    """A grouped meal (one or more photos taken within 2h)."""

    __tablename__ = "meals"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Computed day (using 06:00 boundary)
    day = Column(String(10), nullable=False, index=True)  # "2026-03-14"
    first_photo_at = Column(DateTime, nullable=False)
    is_cheat_day = Column(Boolean, default=False)

    # Analysis results
    analysis_status = Column(String(20), default="pending")  # pending|analyzing|complete|failed
    total_calories = Column(Integer, nullable=True)
    total_protein_g = Column(Integer, nullable=True)
    total_carbs_g = Column(Integer, nullable=True)
    total_fat_g = Column(Integer, nullable=True)
    health_score = Column(Integer, nullable=True)  # 1-100
    health_color = Column(String(10), nullable=True)  # green/yellow/red
    ai_comment = Column(Text, nullable=True)
    items_json = Column(JSONB, nullable=True)  # [{name, portion, calories, ...}]
    correction_note = Column(Text, nullable=True)  # User correction for re-analysis

    photos = relationship("MealPhoto", back_populates="meal", order_by="MealPhoto.photo_taken_at")
    queue_jobs = relationship("AnalysisQueue", back_populates="meal")


class MealPhoto(Base):
    """A single photo belonging to a meal."""

    __tablename__ = "meal_photos"

    id = Column(Integer, primary_key=True)
    meal_id = Column(Integer, ForeignKey("meals.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    filename = Column(String(255), nullable=False)  # stored path relative to /app/data/food/
    original_filename = Column(String(255), nullable=True)
    file_hash = Column(String(64), nullable=True, index=True)  # SHA-256 for dedup
    photo_taken_at = Column(DateTime, nullable=False)
    display_path = Column(String(255), nullable=True)  # 1200px, no EXIF
    thumbnail_path = Column(String(255), nullable=True)  # 400px, no EXIF
    photo_type = Column(String(20), default="unknown")  # cooking|finished|unknown

    meal = relationship("Meal", back_populates="photos")


class AnalysisQueue(Base):
    """Queue for pending AI analysis jobs."""

    __tablename__ = "analysis_queue"

    id = Column(Integer, primary_key=True)
    meal_id = Column(Integer, ForeignKey("meals.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    status = Column(String(20), default="pending")  # pending|processing|complete|failed
    run_after = Column(DateTime, nullable=False)  # debounce: earliest execution time
    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)
    error_message = Column(Text, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    meal = relationship("Meal", back_populates="queue_jobs")
