from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import declarative_base, sessionmaker
from .config import settings

SQLALCHEMY_DATABASE_URL = f"sqlite:///{settings.storage_path}/captionator.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    # check_same_thread=False: FastAPI/Celery share this engine across threads.
    # timeout=30: backend, worker, and beat all call ensure_schema() against
    # this same file on startup; sqlite3's default 5s lock-wait is tight
    # enough that a startup race can raise "database is locked" instead of
    # just waiting it out. These are sub-millisecond metadata/UPDATE
    # statements, so 30s is ample headroom without risking a real hang.
    connect_args={"check_same_thread": False, "timeout": 30},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _default_sql_literal(value) -> str:
    """Render a Python column default as a SQL literal for an ALTER TABLE
    DEFAULT clause or backfill UPDATE. Only the scalar types Job columns
    actually use today (bool, int, float, str) are supported."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def ensure_schema() -> None:
    """Idempotent additive migration. There's no Alembic here: create_all() only
    creates missing TABLES, not missing COLUMNS on a table that already exists on
    disk. Any column added to the Job model after a deployment's first run needs
    this, or existing installs would 500 on the new fields forever.

    Safe to call on every startup, and to call concurrently from the backend,
    worker, and beat processes racing against the same SQLite file on first
    deploy: a losing race just hits "duplicate column name", which is
    swallowed (the engine's `timeout` above makes that the rare outcome
    rather than a "database is locked" crash).
    """
    from .models import Job  # local import: avoids a circular import with Base

    inspector = inspect(engine)
    if "jobs" not in inspector.get_table_names():
        return  # create_all() will create it with every current column; nothing to add

    existing = {col["name"] for col in inspector.get_columns("jobs")}
    for column in Job.__table__.columns:
        has_scalar_default = (
            column.default is not None and getattr(column.default, "is_scalar", False)
        )
        if column.name not in existing:
            col_type = column.type.compile(engine.dialect)
            default_sql = ""
            if has_scalar_default:
                default_sql = f" DEFAULT {_default_sql_literal(column.default.arg)}"
            ddl = f'ALTER TABLE jobs ADD COLUMN "{column.name}" {col_type}{default_sql}'
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
            except OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
        elif has_scalar_default:
            # Backfill any row left NULL by a pre-v1.5.1 deploy that ran this
            # migration before it added the DEFAULT clause above.
            default_sql = _default_sql_literal(column.default.arg)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        f'UPDATE jobs SET "{column.name}" = {default_sql} '
                        f'WHERE "{column.name}" IS NULL'
                    )
                )
