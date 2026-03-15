"""PostgreSQL database connection and session management."""
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from .config import settings
from .models import Base

# Create engine
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,  # Check connection health
    pool_size=5,
    max_overflow=10,
)

# Session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db():
    """Create all tables and run migrations."""
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        for col, default in [
            ("can_view_oura", "FALSE"),
            ("can_view_food", "FALSE"),
        ]:
            conn.execute(text(
                f"ALTER TABLE share_tokens ADD COLUMN IF NOT EXISTS {col} BOOLEAN DEFAULT {default}"
            ))
        conn.commit()


def get_db():
    """Dependency to get database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
