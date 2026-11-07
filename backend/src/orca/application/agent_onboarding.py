"""Purpose: manage organization-owned service registrations without granting research access.
Behavior: version metadata, consume scoped proofs once, and require fresh qualification to activate.
Refs: institutional API/data design sections 2 and 7; Phase: F02.
"""

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from sqlalchemy import text
from sqlalchemy.orm import Session

from orca.application.organizations import Organizations
from orca.application.projects import IdempotencyConflict, StaleVersion
from orca.contracts.agents import AgentDraft, AgentProof
from orca.domain.identity import Principal
from orca.infrastructure.federation.https_egress import endpoint_origin
from orca.infrastructure.federation.signing import canonical, digest, unb64


class Onboarding:
    def __init__(self, session: Session, tenant: UUID, organization: UUID, principal: Principal):
        self.session, self.tenant, self.organization, self.principal = (
            session,
            tenant,
            organization,
            principal,
        )

    def params(self, write: bool = False) -> dict[str, Any]:
        Organizations(self.session, self.tenant, self.organization).authorize(
            self.principal, "org:write" if write else "org:read"
        )
        return {"t": self.tenant, "o": self.organization}

    def event(self, identifier: UUID, kind: str, data: dict[str, Any]) -> None:
        self.session.execute(
            text("""INSERT INTO federation.registration_events
          (id,tenant_id,organization_id,agent_id,actor,event_type,data)
          VALUES(:event,:t,:o,:id,:actor,:kind,CAST(:data AS jsonb))"""),
            {
                **self.params(True),
                "event": uuid4(),
                "id": identifier,
                "actor": self.principal.subject,
                "kind": kind,
                "data": json.dumps(data),
            },
        )

    def row(self, identifier: UUID, write: bool = False) -> dict[str, Any]:
        row = (
            self.session.execute(
                text("""SELECT r.*,v.registration,
          (SELECT max(q.expires_at) FROM federation.agent_qualifications q WHERE q.tenant_id=r.tenant_id
            AND q.agent_id=r.id AND q.binding_version=r.binding_version AND q.report->>'status'='PASS')
            AS qualification_expires_at FROM federation.registrations r
          JOIN federation.registration_versions v ON v.id=r.binding_version AND v.tenant_id=r.tenant_id
          WHERE r.tenant_id=:t AND r.organization_id=:o AND r.id=:id"""),
                {**self.params(write), "id": identifier},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("Agent not found")
        return dict(row)

    @staticmethod
    def view(row: dict[str, Any]) -> dict[str, Any]:
        return {
            **{
                k: row[k]
                for k in (
                    "id",
                    "organization_id",
                    "version",
                    "state",
                    "binding_version",
                    "registration",
                    "created_at",
                    "updated_at",
                )
            },
            "identity_status": "service_only_not_ens_verified",
            "affiliation_status": "self_declared",
            "research_dispatch_enabled": False,
            "qualification_expires_at": row.get("qualification_expires_at"),
            "service_eligible": row["state"] == "active"
            and bool(row.get("qualification_expires_at"))
            and row["qualification_expires_at"] > datetime.now(timezone.utc),
        }

    def listing(self) -> list[dict[str, Any]]:
        ids: Any = self.session.execute(
            text("""SELECT id FROM federation.registrations
          WHERE tenant_id=:t AND organization_id=:o ORDER BY created_at DESC LIMIT 100"""),
            self.params(),
        ).scalars()
        return [self.view(self.row(i)) for i in ids]

    def create(self, body: AgentDraft, key: str) -> dict[str, Any]:
        params = self.params(True)
        endpoint_origin(body.endpoint)
        hashed = digest(body.model_dump(mode="json"))
        previous = (
            self.session.execute(
                text("""SELECT id,request_digest FROM federation.registrations
          WHERE tenant_id=:t AND organization_id=:o AND created_by=:actor AND idempotency_key=:key"""),
                {**params, "actor": self.principal.subject, "key": key},
            )
            .mappings()
            .one_or_none()
        )
        if previous:
            if previous["request_digest"] != hashed:
                raise IdempotencyConflict()
            return self.view(self.row(previous["id"]))
        identifier, binding = uuid4(), uuid4()
        self.session.execute(
            text("""INSERT INTO federation.registrations
          (id,tenant_id,organization_id,created_by,idempotency_key,request_digest,binding_version)
          VALUES(:id,:t,:o,:actor,:key,:digest,:binding)"""),
            {
                **params,
                "id": identifier,
                "actor": self.principal.subject,
                "key": key,
                "digest": hashed,
                "binding": binding,
            },
        )
        self.insert_version(identifier, binding, body)
        self.event(identifier, "draft_created", {"binding_version": str(binding)})
        return self.view(self.row(identifier))

    def insert_version(self, identifier: UUID, binding: UUID, body: AgentDraft) -> None:
        self.session.execute(
            text("""INSERT INTO federation.registration_versions
          (id,tenant_id,organization_id,agent_id,registration)
          VALUES(:binding,:t,:o,:id,CAST(:body AS jsonb))"""),
            {
                **self.params(True),
                "binding": binding,
                "id": identifier,
                "body": body.model_dump_json(),
            },
        )

    def expected(self, identifier: UUID, version: int) -> dict[str, Any]:
        row = self.row(identifier, True)
        if row["version"] != version:
            raise StaleVersion()
        if row["state"] == "revoked":
            raise ValueError("Revoked agent cannot be changed; register a new service")
        return row

    def change(self, identifier: UUID, state: str) -> None:
        self.session.execute(
            text("""UPDATE federation.registrations SET state=:state,
          version=version+1,updated_at=now() WHERE tenant_id=:t AND organization_id=:o AND id=:id"""),
            {**self.params(True), "id": identifier, "state": state},
        )

    def revise(self, identifier: UUID, version: int, body: AgentDraft) -> dict[str, Any]:
        self.expected(identifier, version)
        endpoint_origin(body.endpoint)
        binding = uuid4()
        self.insert_version(identifier, binding, body)
        self.session.execute(
            text("""UPDATE federation.registrations SET binding_version=:binding
          WHERE tenant_id=:t AND organization_id=:o AND id=:id"""),
            {**self.params(True), "id": identifier, "binding": binding},
        )
        self.change(identifier, "draft")
        self.event(identifier, "binding_revised", {"binding_version": str(binding)})
        return self.view(self.row(identifier))

    def challenge(
        self, identifier: UUID, version: int, card: dict[str, Any], coordinator: dict[str, str]
    ) -> dict[str, Any]:
        row = self.expected(identifier, version)
        if row["state"] not in {"draft", "verification_pending"}:
            raise ValueError("Revise the binding before requesting another proof")
        registration = row["registration"]
        if digest(card) != registration["card_digest"]:
            raise ValueError("Agent Card differs from the registered digest")
        if card.get("url") != registration["endpoint"] or card.get("protocolVersion") != "0.3.0":
            raise ValueError("Agent Card endpoint or protocol differs")
        if registration["capability"] not in {s.get("id") for s in card.get("skills", [])}:
            raise ValueError("Agent Card does not declare the requested capability")
        challenge_id = uuid4()
        expiry = datetime.now(timezone.utc) + timedelta(minutes=10)
        payload = {
            "schema_version": "orca.registration-proof/1.0",
            "challenge_id": str(challenge_id),
            "nonce": secrets.token_urlsafe(32),
            "organization_id": str(self.organization),
            "agent_id": str(identifier),
            "binding_version": str(row["binding_version"]),
            "registration_digest": digest(registration),
            "endpoint": registration["endpoint"],
            "card_digest": registration["card_digest"],
            "key_id": registration["key_id"],
            "expires_at": expiry.isoformat(),
            "audience": "orca-agent-onboarding",
            "coordinator": coordinator,
        }
        self.session.execute(
            text("""INSERT INTO federation.registration_challenges
          (id,tenant_id,organization_id,agent_id,binding_version,payload,expires_at)
          VALUES(:challenge,:t,:o,:id,:binding,CAST(:payload AS jsonb),:expiry)"""),
            {
                **self.params(True),
                "challenge": challenge_id,
                "id": identifier,
                "binding": row["binding_version"],
                "payload": json.dumps(payload),
                "expiry": expiry,
            },
        )
        self.change(identifier, "verification_pending")
        self.event(identifier, "challenge_created", {"challenge_id": str(challenge_id)})
        return {"payload": payload, "agent": self.view(self.row(identifier))}

    def prove(self, identifier: UUID, body: AgentProof) -> dict[str, Any]:
        row = self.expected(identifier, body.expected_version)
        challenge = (
            self.session.execute(
                text("""SELECT * FROM federation.registration_challenges
          WHERE tenant_id=:t AND organization_id=:o AND agent_id=:id AND id=:challenge FOR UPDATE"""),
                {**self.params(True), "id": identifier, "challenge": UUID(body.challenge_id)},
            )
            .mappings()
            .one_or_none()
        )
        if (
            row["state"] != "verification_pending"
            or challenge is None
            or challenge["consumed_at"]
            or challenge["expires_at"] <= datetime.now(timezone.utc)
            or challenge["binding_version"] != row["binding_version"]
        ):
            raise ValueError("Challenge expired, consumed or belongs to another binding")
        Ed25519PublicKey.from_public_bytes(unb64(row["registration"]["public_key"])).verify(
            unb64(body.signature), canonical(challenge["payload"])
        )
        self.session.execute(
            text("""UPDATE federation.registration_challenges SET consumed_at=now(),signature=:sig
          WHERE tenant_id=:t AND id=:challenge"""),
            {"t": self.tenant, "challenge": UUID(body.challenge_id), "sig": body.signature},
        )
        self.change(identifier, "verified")
        self.event(identifier, "service_proof_verified", {"challenge_id": body.challenge_id})
        return self.view(self.row(identifier))

    def qualify(
        self, identifier: UUID, version: int, binding: UUID, report: dict[str, Any]
    ) -> dict[str, Any]:
        row = self.expected(identifier, version)
        if row["state"] != "verified" or row["binding_version"] != binding:
            raise ValueError("Current service proof required")
        self.session.execute(
            text("""INSERT INTO federation.agent_qualifications
          (id,tenant_id,organization_id,agent_id,binding_version,report,expires_at)
          VALUES(:report,:t,:o,:id,:binding,CAST(:data AS jsonb),now()+interval '1 day')"""),
            {
                **self.params(True),
                "report": uuid4(),
                "id": identifier,
                "binding": binding,
                "data": json.dumps(report),
            },
        )
        self.change(identifier, "qualified" if report.get("status") == "PASS" else "verified")
        self.event(identifier, "qualification_completed", report)
        return self.view(self.row(identifier))

    def transition(self, identifier: UUID, version: int, action: str) -> dict[str, Any]:
        row = self.expected(identifier, version)
        target = {"activate": "active", "suspend": "suspended", "revoke": "revoked"}[action]
        if action == "activate":
            valid: Any = self.session.execute(
                text("""SELECT EXISTS(SELECT 1 FROM federation.agent_qualifications
              WHERE tenant_id=:t AND agent_id=:id AND binding_version=:binding
              AND expires_at>now() AND report->>'status'='PASS')"""),
                {"t": self.tenant, "id": identifier, "binding": row["binding_version"]},
            ).scalar_one()
            if row["state"] not in {"qualified", "suspended"} or not valid:
                raise ValueError("Fresh qualified binding is required to activate")
        self.change(identifier, target)
        self.event(identifier, action, {"binding_version": str(row["binding_version"])})
        return self.view(self.row(identifier))

    def history(self, identifier: UUID) -> list[dict[str, Any]]:
        self.row(identifier)
        rows = self.session.execute(
            text("""SELECT id,event_type,data,created_at
          FROM federation.registration_events WHERE tenant_id=:t AND agent_id=:id
          ORDER BY created_at,id LIMIT 200"""),
            {"t": self.tenant, "id": identifier},
        ).mappings()
        return [dict(r) for r in rows]

    def queue_qualification(self, identifier: UUID, version: int, key: str) -> dict[str, Any]:
        params = {**self.params(True), "actor": self.principal.subject, "key": key}
        prior = (
            self.session.execute(
                text("""SELECT * FROM federation.registration_jobs
          WHERE tenant_id=:t AND organization_id=:o AND actor=:actor AND idempotency_key=:key"""),
                params,
            )
            .mappings()
            .one_or_none()
        )
        if prior:
            if prior["agent_id"] != identifier or prior["expected_version"] != version:
                raise IdempotencyConflict()
            return {k: prior[k] for k in ("id", "agent_id", "state", "report", "error_code")}
        row = self.expected(identifier, version)
        if row["state"] != "verified":
            raise ValueError("Verify current service proof first")
        pending = (
            self.session.execute(
                text("""SELECT id,agent_id,state,report,error_code FROM federation.registration_jobs
          WHERE tenant_id=:t AND agent_id=:agent AND binding_version=:binding AND state IN ('queued','running')"""),
                {"t": self.tenant, "agent": identifier, "binding": row["binding_version"]},
            )
            .mappings()
            .one_or_none()
        )
        if pending is not None:
            return dict(pending)
        job = uuid4()
        self.session.execute(
            text("""INSERT INTO federation.registration_jobs
          (id,tenant_id,organization_id,agent_id,actor,idempotency_key,expected_version,binding_version)
          VALUES(:job,:t,:o,:agent,:actor,:key,:version,:binding)"""),
            {
                **params,
                "job": job,
                "agent": identifier,
                "version": version,
                "binding": row["binding_version"],
            },
        )
        self.event(identifier, "qualification_queued", {"job_id": str(job)})
        return {
            "id": job,
            "agent_id": identifier,
            "state": "queued",
            "report": None,
            "error_code": None,
        }

    def job(self, identifier: UUID, job: UUID) -> dict[str, Any]:
        self.row(identifier)
        row = (
            self.session.execute(
                text("""SELECT id,agent_id,state,report,error_code FROM federation.registration_jobs
          WHERE tenant_id=:t AND organization_id=:o AND agent_id=:id AND id=:job"""),
                {**self.params(), "id": identifier, "job": job},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise LookupError("Qualification job not found")
        return dict(row)

    def jobs(self, identifier: UUID) -> list[dict[str, Any]]:
        self.row(identifier)
        rows = self.session.execute(
            text("""SELECT id,agent_id,state,report,error_code
          FROM federation.registration_jobs WHERE tenant_id=:t AND organization_id=:o
          AND agent_id=:id ORDER BY created_at DESC LIMIT 20"""),
            {**self.params(), "id": identifier},
        ).mappings()
        return [dict(r) for r in rows]

    def current_challenge(self, identifier: UUID) -> dict[str, Any]:
        row = self.row(identifier, True)
        payload = self.session.execute(
            text("""SELECT payload FROM federation.registration_challenges
          WHERE tenant_id=:t AND agent_id=:id AND binding_version=:binding
          AND consumed_at IS NULL AND expires_at>now() ORDER BY expires_at DESC LIMIT 1"""),
            {"t": self.tenant, "id": identifier, "binding": row["binding_version"]},
        ).scalar_one_or_none()
        if payload is None:
            raise LookupError("No unexpired challenge")
        return {"payload": payload, "agent": self.view(row)}
