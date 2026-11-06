"""Purpose: define tenant-keyed IAM, project and upload persistence models.
Behavior: keep composite tenant references explicit for migration and application use.
Refs: docs/04_database.md sections 2,7; API sections 3.2-3.3; Phase: P02.
"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TenantRow:
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        UniqueConstraint("id", "tenant_id"),
        UniqueConstraint("slug"),
        {"schema": "iam"},
    )
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(63))
    visibility: Mapped[str] = mapped_column(String(24), default="private")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Membership(TenantRow, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["iam.organizations.tenant_id"], ondelete="CASCADE"),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "subject"),
        {"schema": "iam"},
    )
    subject: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="active")


class Project(TenantRow, Base):
    __tablename__ = "projects"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id"], ["iam.organizations.tenant_id"], ondelete="RESTRICT"),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "slug"),
        {"schema": "research"},
    )
    slug: Mapped[str] = mapped_column(String(63))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    access_class: Mapped[str] = mapped_column(String(24), default="project")
    state: Mapped[str] = mapped_column(String(16), default="active")
    version: Mapped[int] = mapped_column(BigInteger, default=1)


class ProjectMember(TenantRow, Base):
    __tablename__ = "project_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["research.projects.tenant_id", "research.projects.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint("tenant_id", "project_id", "principal_id"),
        {"schema": "research"},
    )
    project_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    principal_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    role: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="active")


class Upload(TenantRow, Base):
    __tablename__ = "uploads"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["research.projects.tenant_id", "research.projects.id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint("tenant_id", "id"),
        Index("ix_upload_project_status", "tenant_id", "project_id", "status"),
        {"schema": "research"},
    )
    project_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True))
    object_key: Mapped[str] = mapped_column(String(1024), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(127))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    classification: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(24), default="staging")
    object_version_id: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSONB, default=dict)
    finalized: Mapped[bool] = mapped_column(Boolean, default=False)
