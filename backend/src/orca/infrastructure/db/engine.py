"""Purpose: create the non-owner PostgreSQL connection pool for application traffic.
Behavior: require an explicit URL and reset transaction state on pool return.
Refs: main plan P02.1/P02.3; docs/04_database.md section 2; Phase: P02.
"""

from sqlalchemy import Engine, create_engine, event


def create_runtime_engine(database_url: str) -> Engine:
    engine = create_engine(database_url, pool_pre_ping=True, pool_reset_on_return="rollback")

    @event.listens_for(engine, "checkin")
    def reset_connection(dbapi_connection: object, connection_record: object) -> None:
        rollback = getattr(dbapi_connection, "rollback", None)
        if callable(rollback):
            rollback()

    return engine
