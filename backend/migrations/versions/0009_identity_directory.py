"""Purpose: enumerate only a verified principal's organization memberships before tenant selection.
Behavior: expose a bounded security-definer read function to the private runtime role only.
Refs: API 3.2 GET me/organizations; Phase: P02 frontend integration follow-up.
"""

from alembic import op

revision = "0009_identity_directory"
down_revision = "0008_demo_results"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE FUNCTION iam.principal_organizations(p_subject text,p_after uuid,p_limit integer)
      RETURNS TABLE(id uuid,tenant_id uuid,name text,slug text,visibility text,role text)
      LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,iam AS $$
      SELECT o.id,o.tenant_id,o.name::text,o.slug::text,o.visibility::text,m.role::text
      FROM iam.organizations o JOIN iam.memberships m ON m.tenant_id=o.tenant_id
      WHERE m.subject=p_subject AND m.status='active' AND (p_after IS NULL OR o.id>p_after)
      ORDER BY o.id LIMIT greatest(1,least(p_limit,101)) $$""")
    op.execute("REVOKE ALL ON FUNCTION iam.principal_organizations(text,uuid,integer) FROM PUBLIC")
    op.execute(
        "GRANT EXECUTE ON FUNCTION iam.principal_organizations(text,uuid,integer) TO orca_runtime"
    )


def downgrade() -> None:
    raise RuntimeError("Identity directory downgrade requires explicit API compatibility review")
