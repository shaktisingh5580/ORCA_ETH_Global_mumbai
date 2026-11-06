"""Purpose: persist and page P03 catalog rows within an authorized tenant transaction.
Behavior: validate immutable manifests, enforce matching ownership and stable UUID cursors.
Refs: API 3.4; main plan P03.1/P03.5; Phase: P03.1.
"""

import json
from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.contracts.catalog import ProductVersion, ScientificProduct, SourceProvider


class CatalogService:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session, self.tenant_id = session, tenant_id

    def providers(
        self, *, after: str | None = None, query: str | None = None, limit: int = 50
    ) -> list[SourceProvider]:
        if not 1 <= limit <= 101:
            raise ValueError("page fetch limit must be between 1 and 101")
        rows = self.session.execute(
            text("""SELECT DISTINCT provider_id COLLATE "C" AS provider_id
              FROM research.catalog_products WHERE tenant_id=:tenant
              AND (CAST(:after AS text) IS NULL OR provider_id COLLATE "C">CAST(:after AS text) COLLATE "C")
              AND (CAST(:query AS text) IS NULL OR strpos(lower(provider_id),lower(CAST(:query AS text)))>0)
              ORDER BY provider_id COLLATE "C" LIMIT :limit"""),
            {"tenant": self.tenant_id, "after": after, "query": query, "limit": limit},
        )
        return [SourceProvider(id=row["provider_id"]) for row in rows.mappings()]

    def add_product(self, product: ScientificProduct) -> None:
        if product.tenant_id != self.tenant_id:
            raise PermissionError("catalog tenant mismatch")
        self.session.execute(
            text("""INSERT INTO research.catalog_products
          (id,tenant_id,provider_id,source_product_id,kind,access_class,
           metadata_publication_allowed,status)
          VALUES (:id,:tenant_id,:provider_id,:source_product_id,:kind,:access_class,
                  :metadata_publication_allowed,:status)"""),
            product.model_dump(),
        )

    def add_version(self, version: ProductVersion) -> None:
        if version.tenant_id != self.tenant_id:
            raise PermissionError("version tenant mismatch")
        self.session.execute(
            text("""INSERT INTO research.catalog_versions
          (id,tenant_id,product_id,source_version,metadata_digest,manifest)
          VALUES (:id,:tenant_id,:product_id,:source_version,:metadata_digest,CAST(:manifest AS jsonb))
          ON CONFLICT (tenant_id,product_id,source_version,metadata_digest) DO NOTHING"""),
            {
                "id": version.id,
                "tenant_id": version.tenant_id,
                "product_id": version.product_id,
                "source_version": version.source_version,
                "metadata_digest": version.metadata_digest,
                "manifest": json.dumps(version.model_dump(mode="json")),
            },
        )

    def products(
        self,
        *,
        after: UUID | None = None,
        limit: int = 50,
        provider_id: str | None = None,
        kind: str | None = None,
        variable: str | None = None,
        bbox: tuple[float, float, float, float] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[ScientificProduct]:
        if not 1 <= limit <= 101:
            raise ValueError("page fetch limit must be between 1 and 101")
        rows = self.session.execute(
            text("""SELECT p.* FROM research.catalog_products p
          WHERE p.tenant_id=:tenant AND (CAST(:after AS uuid) IS NULL OR p.id>CAST(:after AS uuid))
          AND (CAST(:provider AS text) IS NULL OR p.provider_id=:provider)
          AND (CAST(:kind AS text) IS NULL OR p.kind=:kind)
          AND ((CAST(:variable AS text) IS NULL AND CAST(:west AS float) IS NULL
                AND CAST(:start AS timestamptz) IS NULL AND CAST(:end AS timestamptz) IS NULL) OR EXISTS (
            SELECT 1 FROM research.catalog_versions v
            WHERE v.tenant_id=p.tenant_id AND v.product_id=p.id
              AND (CAST(:variable AS text) IS NULL OR v.manifest->>'standard_name'=:variable)
              AND (CAST(:west AS float) IS NULL OR (
                (v.manifest #>> '{coverage,bbox,0}')::float < :east
                AND (v.manifest #>> '{coverage,bbox,2}')::float > :west
                AND (v.manifest #>> '{coverage,bbox,1}')::float < :north
                AND (v.manifest #>> '{coverage,bbox,3}')::float > :south))
              AND (CAST(:start AS timestamptz) IS NULL OR
                (v.manifest #>> '{coverage,end}')::timestamptz > :start)
              AND (CAST(:end AS timestamptz) IS NULL OR
                (v.manifest #>> '{coverage,start}')::timestamptz < :end)))
          ORDER BY p.id LIMIT :limit"""),
            {
                "tenant": self.tenant_id,
                "after": after,
                "limit": limit,
                "provider": provider_id,
                "kind": kind,
                "variable": variable,
                "west": bbox[0] if bbox else None,
                "south": bbox[1] if bbox else None,
                "east": bbox[2] if bbox else None,
                "north": bbox[3] if bbox else None,
                "start": start,
                "end": end,
            },
        )
        return [ScientificProduct.model_validate(dict(row)) for row in rows.mappings()]

    def product(self, product_id: UUID) -> ScientificProduct | None:
        row = (
            self.session.execute(
                text("SELECT * FROM research.catalog_products WHERE tenant_id=:tenant AND id=:id"),
                {"tenant": self.tenant_id, "id": product_id},
            )
            .mappings()
            .one_or_none()
        )
        return ScientificProduct.model_validate(dict(row)) if row else None

    def versions(
        self,
        product_id: UUID,
        *,
        after: UUID | None = None,
        limit: int = 50,
        version_id: UUID | None = None,
    ) -> list[ProductVersion]:
        if not 1 <= limit <= 101:
            raise ValueError("page fetch limit must be between 1 and 101")
        rows = self.session.execute(
            text("""SELECT id,tenant_id,product_id,source_version,metadata_digest,manifest
              FROM research.catalog_versions WHERE tenant_id=:tenant AND product_id=:product
              AND (CAST(:version AS uuid) IS NULL OR id=CAST(:version AS uuid))
              AND (CAST(:after AS uuid) IS NULL OR id>CAST(:after AS uuid))
              ORDER BY id LIMIT :limit"""),
            {
                "tenant": self.tenant_id,
                "product": product_id,
                "after": after,
                "limit": limit,
                "version": version_id,
            },
        )
        result = []
        for row in rows.mappings():
            version = ProductVersion.model_validate(row["manifest"])
            for field in ("id", "tenant_id", "product_id", "source_version", "metadata_digest"):
                if getattr(version, field) != row[field]:
                    raise ValueError("catalog version manifest disagrees with stored identity")
            result.append(version)
        return result
