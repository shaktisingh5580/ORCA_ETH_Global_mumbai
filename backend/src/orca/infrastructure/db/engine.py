"""Purpose: create the non-owner PostgreSQL connection pool for application traffic.
Behavior: select psycopg for standard PostgreSQL URLs and reset pool state on return.
Refs: main plan P02.1/P02.3; docs/04_database.md section 2; Phase: P02.
"""

from sqlalchemy import Engine, create_engine, event


def sqlalchemy_psycopg_url(database_url: str) -> str:
    """Accept a libpq-compatible URL while selecting the installed SQLAlchemy driver."""
    if database_url.startswith("postgresql://"):
        return "postgresql+psycopg://" + database_url[len("postgresql://") :]
    return database_url


def create_runtime_engine(database_url: str) -> Engine:
    engine = create_engine(
        sqlalchemy_psycopg_url(database_url), pool_pre_ping=True, pool_reset_on_return="rollback"
    )

    @event.listens_for(engine, "checkin")
    def reset_connection(dbapi_connection: object, connection_record: object) -> None:
        rollback = getattr(dbapi_connection, "rollback", None)
        if callable(rollback):
            rollback()

    return engine
