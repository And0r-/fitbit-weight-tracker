"""Database models for PostgreSQL."""
from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
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
    ip_address = Column(String(45), nullable=True)  # IPv6 max length
    user_agent = Column(Text, nullable=True)

    # Relationship to token
    share_token = relationship("ShareToken", back_populates="access_logs")
