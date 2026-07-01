from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker
from .config import settings

SQLALCHEMY_DATABASE_URL = f"sqlite:///{settings.storage_path}/captionator.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema() -> None:
    """Idempotent additive migration. There's no Alembic here: create_all() only
    creates missing TABLES, not missing COLUMNS on a table that already exists on
    disk. Any column added to the Job model after a deployment's first run needs
    this, or existing installs would 500 on the new fields forever.

    Safe to call on every startup, and to call concurrently from both the backend
    and worker processes racing against the same SQLite file on first deploy: a
    losing race just hits "duplicate column name", which is swallowed.
    """
    from .models import Job  # local import: avoids a circular import with Base

    if "jobs" not in inspect(engine).get_table_names():
        return  # create_all() will create it with every current column; nothing to add

    existing = {col["name"] for col in inspect(engine).get_columns("jobs")}
    for column in Job.__table__.columns:
        if column.name in existing:
            continue
        col_type = column.type.compile(engine.dialect)
        try:
            with engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE jobs ADD COLUMN "{column.name}" {col_type}'))
        except OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise
