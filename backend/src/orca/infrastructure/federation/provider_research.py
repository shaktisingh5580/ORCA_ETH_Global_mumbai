"""Purpose: bind provider execution and A2A readback to one signed research grant.
Behavior: match operator-approved local inputs and persist the result before acknowledging it.
Refs: main_implementation plan.md P09.2–P09.3 and P13.2; api design.md §17.1. Phase: P09/P13.
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from a2a.server.context import ServerCallContext
from a2a.types import Task

from orca.agent_provider import DurableTasks
from orca.application.federation import SignedResearchGrant, verify_grant
from orca.infrastructure.federation.signing import digest, strict_json


def require_release(policy: Path, plan_digest: str, count: int) -> None:
    try:
        with policy.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("Release policy exceeds the size limit")
        document = strict_json(raw)
        if (
            not isinstance(document, dict)
            or set(document) != {"schema", "plans"}
            or document["schema"] != "orca.provider-release.v1"
        ):
            raise ValueError("Unsupported release policy")
        approval = document["plans"][plan_digest]
        if (
            not isinstance(approval, dict)
            or set(approval) != {"rights_record_digest", "min_samples"}
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", approval["rights_record_digest"])
            or type(approval["min_samples"]) is not int
            or type(count) is not int
            or not 2 <= approval["min_samples"] <= count <= 1000
        ):
            raise ValueError("Release requires a rights record and sufficient local samples")
    except (KeyError, OSError, ValueError, TypeError) as error:
        raise ValueError("Provider release is not approved") from error


class ResearchTasks(DurableTasks):
    def __init__(self, directory: Path) -> None:
        super().__init__(directory)
        self.release_policy = directory / "research-policy.json"
        with self.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS research_results(
              operation TEXT PRIMARY KEY, grant_digest TEXT NOT NULL,
              task_id TEXT NOT NULL UNIQUE, data TEXT NOT NULL)""")

    async def save(self, task: Task, context: ServerCallContext | None = None) -> None:
        if context and context.state["authorization"].get("scope") in {
            "research:dispatch",
            "research:readback",
        }:
            return  # Research results are written only by complete(), under the original grant.
        await super().save(task, context)

    async def get(self, task_id: str, context: ServerCallContext | None = None) -> Task | None:
        auth = context.state["authorization"] if context else {}
        if auth.get("scope") not in {"research:readback", "research:dispatch"}:
            return await super().get(task_id, context)
        if auth["scope"] == "research:readback" and task_id != auth["remote_task_id"]:
            return None
        with self.connection() as db:
            row = db.execute(
                """SELECT grant_digest,data,
                  json_extract(data,'$.artifacts[0].parts[0].data.payload.approved_plan_digest'),
                  json_extract(data,'$.artifacts[0].parts[0].data.payload.result.count')
                  FROM research_results WHERE operation=?
                  AND (task_id=? OR operation=?)""",
                (auth["jti"], task_id, task_id),
            ).fetchone()
        if row is None or row[0] != auth.get("grant_digest"):
            return None
        try:
            require_release(self.release_policy, row[2], row[3])
        except ValueError:
            return None
        return Task.model_validate_json(row[1])

    def complete(self, signed: SignedResearchGrant, task: Task, count: int) -> None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            require_release(self.release_policy, signed.grant.approved_plan_digest, count)
            operation = str(signed.grant.task_id)
            if db.execute(
                "SELECT 1 FROM research_results WHERE operation=?", (operation,)
            ).fetchone():
                raise ValueError("Research task already completed; use task readback")
            db.execute(
                "INSERT INTO research_results VALUES (?,?,?,?)",
                (operation, signed.grant_digest, task.id, task.model_dump_json(by_alias=True)),
            )


def approved_inputs(
    data: dict[str, Any], auth: dict[str, Any], config: dict[str, Any], policy: Path
) -> tuple[SignedResearchGrant, dict[str, Any]]:
    if (
        set(data) != {"schema_version", "grant"}
        or data["schema_version"] != "orca.scientific_task/1.0"
        or digest(data) != auth["request_digest"]
    ):
        raise ValueError("Invalid research request")
    signed = SignedResearchGrant.model_validate(data["grant"])
    coordinator = config["coordinator"]
    grant = verify_grant(signed, coordinator["public_key"], coordinator["key_id"])
    now = datetime.now(timezone.utc)
    if (
        grant.audience_agent_id != UUID(config["agent_id"])
        or grant.binding_version != UUID(config["research_binding_version"])
        or str(grant.task_id) != auth["jti"]
        or signed.grant_digest != auth["grant_digest"]
        or (grant.endpoint is not None and grant.endpoint != config["endpoint"])
        or not now < grant.expires_at
        or grant.capability_id != "temperature_summary_v1"
        or grant.method_id != "arithmetic_mean/1.0"
    ):
        raise ValueError("Grant is outside this provider's scope")
    entry = config["research_plans"][grant.approved_plan_digest]
    if (
        entry["organization_id"] != str(grant.organization_id)
        or entry["project_id"] != str(grant.project_id)
        or entry["dataset_ids"] != [str(item) for item in grant.dataset_ids]
    ):
        raise ValueError("Grant differs from operator-approved inputs")
    values = entry["values"]
    if not isinstance(values, list):
        raise ValueError("Provider inputs require a local sample list")
    require_release(policy, grant.approved_plan_digest, len(values))
    return signed, {
        "schema_version": "orca.provider-qualification/1.0",
        "operation_id": str(grant.task_id),
        "values": values,
        "unit": entry["unit"],
    }
