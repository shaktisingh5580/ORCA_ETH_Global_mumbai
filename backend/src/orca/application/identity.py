"""Purpose: enforce role boundaries and last-administrator protection.
Behavior: reject membership removal when it would leave a tenant unmanaged.
Refs: API section 3.2; main plan P02.4; Phase: P02.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from orca.infrastructure.db.models import Membership


class LastAdministratorError(Exception):
    pass


def revoke_membership(session: Session, membership: Membership) -> None:
    if membership.role in {"org_owner", "org_admin"} and membership.status == "active":
        count = session.scalar(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.tenant_id == membership.tenant_id,
                Membership.status == "active",
                Membership.role.in_(("org_owner", "org_admin")),
            )
        )
        if count is None or count <= 1:
            raise LastAdministratorError
    membership.status = "revoked"
    session.flush()
