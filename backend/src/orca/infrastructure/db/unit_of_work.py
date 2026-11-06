"""Purpose: bind every application transaction to a server-authorized tenant.
Behavior: begin, set transaction-local context, commit on success and rollback otherwise.
Refs: API sections 10,12,16; main plan P02.3; Phase: P02.
"""

from types import TracebackType
from uuid import UUID

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from orca.infrastructure.db.tenant_context import assert_runtime_role, set_tenant_context


class TenantUnitOfWork:
    def __init__(self, engine: Engine, tenant_id: UUID) -> None:
        self.engine = engine
        self.tenant_id = tenant_id
        self.session: Session | None = None

    def __enter__(self) -> Session:
        self.session = Session(self.engine, expire_on_commit=False)
        connection = self.session.connection()
        assert_runtime_role(connection)
        set_tenant_context(connection, self.tenant_id)
        return self.session

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.session is None:
            return
        try:
            self.session.commit() if exc is None else self.session.rollback()
        finally:
            self.session.close()
