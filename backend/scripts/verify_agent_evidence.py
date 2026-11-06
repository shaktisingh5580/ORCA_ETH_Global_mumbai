"""Purpose: verify retained agent coordination against live Arkiv without the ORCA API/database.
Behavior: check pinned wallets, service signatures and expected digests; leave live authority unknown.
Refs: canonical independent-verifier rules; H04-agent-coordination.json; Phase: H04.
"""

import argparse
import json
from pathlib import Path

from orca.contracts.network import AgentBinding, SignedEnvelope
from orca.infrastructure.federation.runtime import ROOT, ArkivGateway, verify_chain_record
from orca.infrastructure.federation.signing import strict_json


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--evidence", type=Path, default=ROOT / "docs/gates/HACKATHON/H04-live-coordination.json"
    )
    parser.add_argument(
        "--bindings", type=Path, default=ROOT / "contracts/deployments/agent-demo-rabdi.json"
    )
    parser.add_argument("--rpc-env-file", type=Path, default=ROOT / ".env.arkiv")
    args = parser.parse_args()
    evidence = strict_json(args.evidence.read_bytes())
    manifest = strict_json(args.bindings.read_bytes())
    bindings = {
        item["wallet"].lower(): AgentBinding.model_validate(item) for item in manifest["bindings"]
    }
    gateway = ArkivGateway(args.rpc_env_file, ROOT / ".cache/network/verifier", "")
    verified = []
    for task in evidence["tasks"]:
        for reference in task["chain_records"]:
            binding = bindings[reference["owner"].lower()]
            observed = gateway.call(
                {
                    "command": "read",
                    "entity_key": reference["entity_key"],
                    "owner": binding.wallet,
                    "content_digest": reference["content_digest"],
                }
            )
            envelope = SignedEnvelope.model_validate(observed["envelope"])
            verify_chain_record(observed, binding, envelope)
            if (
                envelope.payload["task_id"] != task["id"]
                or envelope.payload["kind"] != reference["kind"]
            ):
                raise ValueError("chain record does not match retained task linkage")
            verified.append(
                {
                    "task_id": task["id"],
                    "kind": reference["kind"],
                    "entity_key": reference["entity_key"],
                    "owner": binding.wallet,
                    "observed_at": observed["observed_at"],
                }
            )
    print(
        json.dumps(
            {
                "status": "LIVE_CHAIN_RECORDS_VERIFIED",
                "records": verified,
                "orca_api_used": False,
                "database_used": False,
                "wallet_private_key_used": False,
                "authorization_freshness": "unknown; offline binding snapshot does not establish current revocation status",
                "ens_verified": False,
                "institution_verified": False,
                "scientific_correctness": "not established by chain verification",
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
