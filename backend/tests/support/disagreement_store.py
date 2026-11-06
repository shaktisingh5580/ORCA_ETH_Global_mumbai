"""Purpose: persist deliberately synthetic demo runs separately from real research records.
Behavior: scope reads/actions by tenant/project, append immutable stages and use versioned correction.
Refs: H05 demonstration isolation contract; Phase: H05; test/demo tooling only.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from orca.infrastructure.federation.signing import digest


class DemoStore:
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / "runs.sqlite3"
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,tenant TEXT NOT NULL,project TEXT NOT NULL,subject TEXT NOT NULL,
              request_key TEXT NOT NULL,request_digest TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'queued',version INTEGER NOT NULL DEFAULT 1,
              phase INTEGER NOT NULL DEFAULT 0,public_reference INTEGER NOT NULL,created_at TEXT NOT NULL,lease_token TEXT,lease_until REAL,
              error_code TEXT,UNIQUE(tenant,subject,request_key))""")
            db.execute(
                "CREATE TABLE IF NOT EXISTS stages(run_id TEXT NOT NULL,sequence INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(run_id,sequence),FOREIGN KEY(run_id) REFERENCES runs(id))"
            )
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS stages_immutable BEFORE UPDATE ON stages BEGIN SELECT RAISE(ABORT,'demo evidence is append only'); END"
            )
            db.execute(
                "CREATE TRIGGER IF NOT EXISTS stages_no_delete BEFORE DELETE ON stages BEGIN SELECT RAISE(ABORT,'demo evidence is append only'); END"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS actions(run_id TEXT NOT NULL,key TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(run_id,key))"
            )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(
        self, tenant: str, project: str, subject: str, key: str, public: bool
    ) -> dict[str, Any]:
        payload = {"project_id": project, "approve_public_reference": public}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id,request_digest FROM runs WHERE tenant=? AND subject=? AND request_key=?",
                (tenant, subject, key),
            ).fetchone()
            if existing:
                if existing["request_digest"] != digest(payload):
                    raise ValueError("idempotency key already used for another demonstration")
                identifier = existing["id"]
            else:
                identifier = str(uuid4())
                db.execute(
                    "INSERT INTO runs(id,tenant,project,subject,request_key,request_digest,public_reference,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        tenant,
                        project,
                        subject,
                        key,
                        digest(payload),
                        int(public),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
        return self.read(identifier, tenant, project)

    def read(self, identifier: str, tenant: str, project: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM runs WHERE id=? AND tenant=? AND project=?",
                (identifier, tenant, project),
            ).fetchone()
            if row is None:
                raise LookupError("demonstration not found")
            stages = [
                json.loads(item[0])
                for item in db.execute(
                    "SELECT data FROM stages WHERE run_id=? ORDER BY sequence", (identifier,)
                )
            ]
        return {
            "id": row["id"],
            "project_id": row["project"],
            "state": row["state"],
            "version": row["version"],
            "created_at": row["created_at"],
            "source_mode": "synthetic_demo",
            "scenario_id": "biased-current-sst-v1",
            "fixture_notice": "Intentional +1.0 Celsius bias in producer input; not observed marine research",
            "publication_mode": "public_reference" if row["public_reference"] else "private",
            "stages": stages,
            "error_code": row["error_code"],
        }

    def correct(
        self, identifier: str, tenant: str, project: str, version: int, key: str
    ) -> dict[str, Any]:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM runs WHERE id=? AND tenant=? AND project=?",
                (identifier, tenant, project),
            ).fetchone()
            if row is None:
                raise LookupError("demonstration not found")
            expected = digest({"expected_version": version})
            previous = db.execute(
                "SELECT digest FROM actions WHERE run_id=? AND key=?", (identifier, key)
            ).fetchone()
            if previous:
                if previous[0] != expected:
                    raise ValueError("correction retry body differs")
            else:
                if row["state"] != "challenged" or row["version"] != version:
                    raise ValueError("demonstration changed or has no pending challenge")
                db.execute("INSERT INTO actions VALUES(?,?,?)", (identifier, key, expected))
                db.execute(
                    "UPDATE runs SET state='correcting',version=version+1 WHERE id=?", (identifier,)
                )
        return self.read(identifier, tenant, project)
