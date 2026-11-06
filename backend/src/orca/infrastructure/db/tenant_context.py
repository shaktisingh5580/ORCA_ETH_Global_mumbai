"""Purpose: install a validated tenant identifier for one database transaction.
Behavior: use SET LOCAL through a bound parameter and clear state at transaction end.
Refs: API sections 2.2,10,16; main plan P02.3; Phase: P02.
"""

from uuid import UUID

from sqlalchemy import Connection, text


def set_tenant_context(connection: Connection, tenant_id: UUID) -> None:
    connection.execute(
        text("SELECT set_config('orca.tenant_id', :tenant_id, true)"), {"tenant_id": str(tenant_id)}
    )


def assert_runtime_role(connection: Connection) -> None:
    result = connection.execute(
        text("""SELECT rolsuper, rolbypassrls
      FROM pg_roles WHERE rolname = current_user""")
    ).one()
    if bool(result.rolsuper) or bool(result.rolbypassrls):
        raise RuntimeError("runtime database role must not bypass row security")
