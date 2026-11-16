"""Purpose: keep P02 database URLs usable by both psycopg and SQLAlchemy.
Behavior: verify standard PostgreSQL URLs select the installed psycopg driver without connecting.
Refs: main_implementation plan.md P02.1/P02.3; Phase: P02.
"""

from orca.infrastructure.db.engine import create_runtime_engine, sqlalchemy_psycopg_url


def test_standard_postgres_url_selects_psycopg() -> None:
    ordinary = "postgresql://orca_runtime:password@127.0.0.1:5432/orca_test"
    dialect = sqlalchemy_psycopg_url(ordinary)
    assert dialect == ordinary.replace("postgresql://", "postgresql+psycopg://", 1)
    assert sqlalchemy_psycopg_url(dialect) == dialect
    engine = create_runtime_engine(ordinary)
    try:
        assert engine.dialect.driver == "psycopg"
    finally:
        engine.dispose()
