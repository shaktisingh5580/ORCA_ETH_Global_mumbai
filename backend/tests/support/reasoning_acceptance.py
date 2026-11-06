"""Purpose: verify live Gemini tool selection on explicitly synthetic bounded observations.
Behavior: use ignored credentials, print no input secrets, write sanitized acceptance evidence.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A02 gate; Phase: A02 test tooling only.
"""

import json
from pathlib import Path
from uuid import UUID

from dotenv import dotenv_values

from orca.application.reasoning_loop import plan_research
from orca.infrastructure.gemini_reasoning import GeminiReasoner

ROOT = Path(__file__).resolve().parents[3]


def main() -> None:
    values = dotenv_values(ROOT / ".env.demo")
    key = values.get("ORCA_GEMINI_API_KEY") or ""
    dataset = str(UUID(int=77))
    events = []
    plan = plan_research(
        "Normalize this Celsius sea surface temperature CSV; the timestamps are explicitly UTC.",
        {
            dataset: {
                "name": "Synthetic Celsius control",
                "source_mode": "synthetic_demo",
                "format": "csv",
                "content": "timestamp_utc,temperature_celsius\n2025-01-01T00:00:00Z,27\n2025-01-02T00:00:00Z,28\n",
            }
        },
        GeminiReasoner(key, "gemini-2.5-flash"),
        lambda kind, data: events.append({"type": kind, "data": data}),
    )
    assert plan.outcome == "supported"
    assert any(
        e["type"] == "tool_completed" and e["data"]["tool"] == "preview_mapping" for e in events
    )
    evidence = {
        "phase": "A02",
        "live_planner": "PASS",
        "model": "gemini-2.5-flash",
        "source_mode": "synthetic_demo",
        "plan": plan.model_dump(mode="json"),
        "events": events,
        "full_phase_complete": False,
    }
    (ROOT / "docs/gates/HACKATHON/A02-live-planner.json").write_text(
        json.dumps(evidence, indent=2) + "\n"
    )
    print("Live Gemini inspected evidence and validated a normalization plan")


if __name__ == "__main__":
    main()
