"""Purpose: preserve uploaded evidence and approved normalization in the existing project boundary.
Behavior: authorize each operation, deduplicate retries and append immutable transformations.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A01; api design.md project authority; Phase: A01.
"""

import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.workspace_access import authorize_project
from orca.contracts.normalization import DatasetInput, DatasetView, Mapping, NormalizationView
from orca.domain.identity import Principal
from orca.domain.normalization import digest, inspect, normalize


class TabularEvidence:
    def __init__(self, session: Session, tenant: UUID, project: UUID, principal: Principal):
        self.session, self.tenant, self.project, self.principal = (
            session,
            tenant,
            project,
            principal,
        )

    def authorize(self, scope: str = "project:read") -> dict[str, Any]:
        authorize_project(self.session, self.tenant, self.project, self.principal, scope)
        return {"t": self.tenant, "p": self.project}

    @staticmethod
    def view(row: Any) -> DatasetView:
        return DatasetView(**{k: row[k] for k in DatasetView.model_fields})

    def create(self, body: DatasetInput) -> DatasetView:
        params = self.authorize("project:write")
        inspection = inspect(body.content, body.format)
        content_digest = "sha256:" + hashlib.sha256(body.content.encode()).hexdigest()
        params.update(
            id=uuid4(),
            actor=self.principal.subject,
            **body.model_dump(),
            digest=content_digest,
            inspection=json.dumps(inspection),
        )
        self.session.execute(
            text("""INSERT INTO research.tabular_datasets
          (id,tenant_id,project_id,created_by,name,format,source_mode,content,content_digest,inspection)
          VALUES(:id,:t,:p,:actor,:name,:format,:source_mode,:content,:digest,CAST(:inspection AS jsonb))
          ON CONFLICT(tenant_id,project_id,content_digest,source_mode) DO NOTHING"""),
            params,
        )
        row = (
            self.session.execute(
                text("""SELECT * FROM research.tabular_datasets
          WHERE tenant_id=:t AND project_id=:p AND content_digest=:digest AND source_mode=:source_mode"""),
                params,
            )
            .mappings()
            .one()
        )
        return self.view(row)

    def list_datasets(self) -> list[DatasetView]:
        rows = self.session.execute(
            text("""SELECT * FROM research.tabular_datasets
          WHERE tenant_id=:t AND project_id=:p ORDER BY created_at DESC LIMIT 50"""),
            self.authorize(),
        ).mappings()
        return [self.view(r) for r in rows]

    def row(self, identifier: UUID) -> Any:
        row = (
            self.session.execute(
                text("""SELECT * FROM research.tabular_datasets
          WHERE tenant_id=:t AND project_id=:p AND id=:id"""),
                {**self.authorize(), "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("Dataset not found")
        return row

    def preview(self, identifier: UUID, mapping: Mapping) -> dict[str, Any]:
        row = self.row(identifier)
        return normalize(row["content"], row["format"], mapping)

    def approve(self, identifier: UUID, mapping: Mapping) -> NormalizationView:
        params = self.authorize("project:write")
        result = self.preview(identifier, mapping)
        row = self.row(identifier)
        # Source and mapping references are part of the immutable artifact digest.
        result.update(
            source_mode=row["source_mode"],
            source_digest=row["content_digest"],
            mapping_digest=digest(mapping.model_dump()),
        )
        params.update(
            id=uuid4(),
            dataset=identifier,
            actor=self.principal.subject,
            mapping=json.dumps(mapping.model_dump()),
            mapping_digest=digest(mapping.model_dump()),
            result=json.dumps(result),
            digest=digest(result),
        )
        self.session.execute(
            text("""INSERT INTO research.tabular_normalizations
          (id,tenant_id,project_id,dataset_id,approved_by,mapping,mapping_digest,result,artifact_digest)
          VALUES(:id,:t,:p,:dataset,:actor,CAST(:mapping AS jsonb),:mapping_digest,CAST(:result AS jsonb),:digest)
          ON CONFLICT(tenant_id,dataset_id,mapping_digest) DO NOTHING"""),
            params,
        )
        saved = (
            self.session.execute(
                text("""SELECT * FROM research.tabular_normalizations
          WHERE tenant_id=:t AND project_id=:p AND dataset_id=:dataset AND mapping_digest=:mapping_digest"""),
                params,
            )
            .mappings()
            .one()
        )
        return NormalizationView(**{k: saved[k] for k in NormalizationView.model_fields})

    def normalizations(self, identifier: UUID) -> list[NormalizationView]:
        self.row(identifier)
        rows = self.session.execute(
            text("""SELECT * FROM research.tabular_normalizations
          WHERE tenant_id=:t AND project_id=:p AND dataset_id=:id ORDER BY created_at"""),
            {**self.authorize(), "id": identifier},
        ).mappings()
        return [
            NormalizationView(**{k: r[k] for k in NormalizationView.model_fields}) for r in rows
        ]
