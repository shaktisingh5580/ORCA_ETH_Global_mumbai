"""Purpose: implement the durable quarantine-to-file lifecycle with project authorization.
Behavior: stage bounded CSV/JSON, validate bytes before visibility, and audit downloads.
Refs: API 3.3; main plan P02.5; Phase: P02 completion repair.
"""

import csv
import io
import json
import math
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.ports.object_store import ValidatingObjectStore
from orca.application.workspace_access import authorize_project, get_policy
from orca.contracts.files import (
    MAX_FILE_BYTES,
    CompleteUpload,
    EphemeralDownload,
    ProjectFile,
    UploadPart,
    UploadSession,
    UploadStatus,
)
from orca.contracts.projects import UploadIntent
from orca.domain.identity import Principal

FORMATS = {"text/csv": ".csv", "application/json": ".json"}
# Project visibility cannot weaken an individual file's classification.
FILE_VISIBILITY_SQL = """EXISTS(SELECT 1 FROM iam.memberships m
  WHERE m.tenant_id=u.tenant_id AND m.subject=:actor AND m.status='active' AND (
    u.classification='organization' OR m.role IN ('org_owner','org_admin')
    OR u.metadata_json->>'owner'=:actor
    OR EXISTS(SELECT 1 FROM research.project_members pm WHERE pm.tenant_id=u.tenant_id
      AND pm.project_id=u.project_id AND pm.principal_id=m.id AND pm.status='active'
      AND (u.classification<>'confidential' OR pm.role='admin'))
    OR (u.classification='project' AND EXISTS(SELECT 1 FROM iam.access_grants g
      JOIN research.projects p ON p.tenant_id=g.tenant_id AND p.id=g.project_id
      WHERE g.tenant_id=u.tenant_id AND g.project_id=u.project_id AND g.principal_id=m.id
      AND NOT g.revoked AND g.expires_at>clock_timestamp() AND g.resource_version=p.version))))"""


def json_number(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("nonfinite JSON number")
    return parsed


def safe_cells(row: list[str]) -> None:
    if any(c.lstrip().startswith(("=", "+", "@")) or re.match(r"^\s*-\D", c) for c in row):
        raise ValueError("spreadsheet formula content rejected")


def validate_format(content: bytes, media_type: str) -> None:
    decoded = content.decode("utf-8-sig")
    if any(ord(c) < 32 and c not in "\r\n\t" for c in decoded):
        raise ValueError("binary or control content rejected")
    if media_type == "application/json":
        parsed = json.loads(
            decoded,
            parse_float=json_number,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")),
        )
        if not isinstance(parsed, (dict, list)):
            raise ValueError("JSON object or array required")
    elif media_type == "text/csv":
        rows = csv.reader(io.StringIO(decoded), strict=True)
        header = next(rows, None)
        if not header or len(header) > 1000 or len(set(header)) != len(header):
            raise ValueError("bounded unique CSV header required")
        safe_cells(header)
        seen = False
        for row in rows:
            if len(row) != len(header):
                raise ValueError("inconsistent CSV shape")
            # Files can be exported to spreadsheet tools: reject formula injection.
            safe_cells(row)
            seen = True
        if not seen:
            raise ValueError("CSV data rows required")
    else:
        raise ValueError("unsupported validation format")


class Files:
    def __init__(self, session: Session, tenant: UUID, store: ValidatingObjectStore):
        self.session, self.tenant, self.store = session, tenant, store

    def row(
        self, identifier: UUID, actor: Principal, scope: str, *, require_owner: bool = False
    ) -> Any:
        row = (
            self.session.execute(
                text(
                    "SELECT u.* FROM research.uploads u WHERE tenant_id=:t AND id=:id AND "
                    + FILE_VISIBILITY_SQL
                ),
                {"t": self.tenant, "id": identifier, "actor": actor.subject},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("upload not found")
        authorize_project(self.session, self.tenant, row["project_id"], actor, scope)
        row = (
            self.session.execute(
                text(
                    "SELECT u.* FROM research.uploads u WHERE tenant_id=:t AND id=:id AND "
                    + FILE_VISIBILITY_SQL
                    + " FOR UPDATE"
                ),
                {"t": self.tenant, "id": identifier, "actor": actor.subject},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("file not found")
        if (require_owner or not row["finalized"]) and row["metadata_json"].get(
            "owner"
        ) != actor.subject:
            raise LookupError("upload not found")
        return row

    @staticmethod
    def status(row: Any) -> UploadStatus:
        return UploadStatus(
            upload_id=row["id"],
            project_id=row["project_id"],
            status=row["status"],
            filename=row["filename"],
            rejection_code=row["metadata_json"].get("rejection_code"),
        )

    @staticmethod
    def metadata(row: Any) -> ProjectFile:
        return ProjectFile(
            id=row["id"],
            project_id=row["project_id"],
            filename=row["filename"],
            media_type=row["media_type"],
            size_bytes=row["size_bytes"],
            sha256="sha256:" + row["sha256"],
            classification=row["classification"],
            created_at=row["created_at"],
        )

    def create(self, project: UUID, body: UploadIntent, actor: Principal) -> UploadSession:
        authorize_project(self.session, self.tenant, project, actor, "project:write")
        if body.size_bytes > min(
            get_policy(self.session, self.tenant).max_upload_bytes, MAX_FILE_BYTES
        ):
            raise ValueError("upload exceeds configured size limit")
        extension = FORMATS.get(body.media_type)
        if extension is None or not body.filename.lower().endswith(extension):
            raise ValueError("prototype accepts validated UTF-8 CSV and JSON files only")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", body.filename):
            raise ValueError("safe filename required")
        project_class: str = self.session.execute(
            text("SELECT access_class FROM research.projects WHERE tenant_id=:t AND id=:p"),
            {"t": self.tenant, "p": project},
        ).scalar_one()
        rank = {"public": 0, "organization": 0, "project": 1, "restricted": 2, "confidential": 3}
        classification = (
            project_class
            if rank[project_class] > rank[body.classification]
            else body.classification
        )
        identifier = uuid4()
        expires = datetime.now(timezone.utc) + timedelta(seconds=300)
        staged = self.store.stage_checked(
            str(self.tenant),
            str(project),
            str(identifier),
            body.filename,
            body.sha256[7:],
            body.media_type,
            body.size_bytes,
        )
        self.session.execute(
            text("""INSERT INTO research.uploads(id,tenant_id,project_id,object_key,
          filename,media_type,size_bytes,sha256,classification,metadata_json)
          VALUES(:id,:t,:p,:key,:filename,:media_type,:size_bytes,:sha256,:classification,CAST(:metadata AS jsonb))"""),
            {
                **body.model_dump(),
                "classification": classification,
                "id": identifier,
                "t": self.tenant,
                "p": project,
                "key": staged.object_key,
                "sha256": body.sha256[7:],
                "metadata": json.dumps(
                    {
                        "owner": actor.subject,
                        "expires_at": expires.isoformat(),
                        "staging_key": staged.object_key,
                        "staging_deleted": False,
                    }
                ),
            },
        )
        return UploadSession(
            upload_id=identifier,
            expires_at=expires,
            parts=[
                UploadPart(
                    url=staged.upload_url,
                    headers={"Content-Type": body.media_type, "x-amz-meta-sha256": body.sha256[7:]},
                )
            ],
        )

    def complete(self, identifier: UUID, body: CompleteUpload, actor: Principal) -> UploadStatus:
        row = self.row(identifier, actor, "project:write", require_owner=True)
        if "sha256:" + row["sha256"] != body.sha256:
            raise ValueError("completion digest differs from upload intent")
        if row["status"] in {"pending_validation", "finalized"}:
            return self.status(row)
        if row["status"] != "staging":
            raise ValueError("upload is not completable")
        if datetime.fromisoformat(row["metadata_json"]["expires_at"]) < datetime.now(timezone.utc):
            raise ValueError("upload session expired")
        # HEAD is an existence/size check only. The validator independently hashes bytes.
        self.store.verify(row["object_key"], row["size_bytes"], row["sha256"])
        self.session.execute(
            text(
                "UPDATE research.uploads SET status='pending_validation',updated_at=clock_timestamp() WHERE tenant_id=:t AND id=:id"
            ),
            {"t": self.tenant, "id": identifier},
        )
        return self.status({**row, "status": "pending_validation"})

    def abort(self, identifier: UUID, actor: Principal) -> None:
        row = self.row(identifier, actor, "project:write", require_owner=True)
        if row["finalized"]:
            raise ValueError("published files cannot be aborted")
        self.store.abort(row["object_key"])
        self.session.execute(
            text(
                "UPDATE research.uploads SET status='aborted',updated_at=clock_timestamp() WHERE tenant_id=:t AND id=:id"
            ),
            {"t": self.tenant, "id": identifier},
        )

    def validate(self, identifier: UUID) -> bool:
        # Trusted worker operation: transaction and tenant context supplied by server.
        row = (
            self.session.execute(
                text(
                    "SELECT * FROM research.uploads WHERE tenant_id=:t AND id=:id FOR UPDATE SKIP LOCKED"
                ),
                {"t": self.tenant, "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return False
        if row["status"] != "pending_validation":
            metadata = row["metadata_json"]
            if metadata.get("staging_deleted") is not False or datetime.fromisoformat(
                metadata["expires_at"]
            ) > datetime.now(timezone.utc):
                return False
            self.store.abort(metadata["staging_key"])
            self.session.execute(
                text("""UPDATE research.uploads SET
              status=CASE WHEN status='staging' THEN 'aborted' ELSE status END,
              metadata_json=metadata_json || jsonb_build_object('staging_deleted',true),
              updated_at=clock_timestamp() WHERE tenant_id=:t AND id=:id"""),
                {"t": self.tenant, "id": identifier},
            )
            return True
        try:
            raw = self.store.read_checked(row["object_key"], row["size_bytes"], row["sha256"])
            validate_format(raw, row["media_type"])
        except (ValueError, UnicodeError, csv.Error, RecursionError):
            self.session.execute(
                text("""UPDATE research.uploads SET status='rejected',metadata_json=metadata_json ||
              '{"rejection_code":"CONTENT_VALIDATION_FAILED"}'::jsonb WHERE tenant_id=:t AND id=:id"""),
                {"t": self.tenant, "id": identifier},
            )
            return True
        final = self.store.publish(row["object_key"], raw, row["sha256"], row["media_type"])
        self.session.execute(
            text("""UPDATE research.uploads SET object_key=:key,object_version_id=:version,
          status='finalized',finalized=true,updated_at=clock_timestamp() WHERE tenant_id=:t AND id=:id"""),
            {
                "t": self.tenant,
                "id": identifier,
                "key": final.object_key,
                "version": final.version_id,
            },
        )
        return True

    def download(self, identifier: UUID, actor: Principal, purpose: str) -> EphemeralDownload:
        row = self.row(identifier, actor, "project:read")
        if not row["finalized"]:
            raise LookupError("file not found")
        self.session.execute(
            text("""INSERT INTO iam.membership_audit(id,tenant_id,actor_subject,member_id,event)
          VALUES(:id,:t,:s,:file,CAST(:event AS jsonb))"""),
            {
                "id": uuid4(),
                "t": self.tenant,
                "s": actor.subject,
                "file": identifier,
                "event": json.dumps(
                    {"kind": "file_download", "purpose": purpose, "ttl_seconds": 60}
                ),
            },
        )
        return EphemeralDownload(
            url=self.store.download_url(row["object_key"], str(self.tenant), 60),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            sha256="sha256:" + row["sha256"],
        )
