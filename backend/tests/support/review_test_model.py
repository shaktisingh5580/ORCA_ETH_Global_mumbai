"""Purpose: provide a deterministic model double solely for reviewer tests.
Behavior: derive test decisions from the real comparison tool output.
Refs: ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A03; Phase: A03 test support.
"""


class ReviewModel:
    def turn(self, instruction, contents, tools):
        if len(contents) == 1:
            name, args = "inspect_review_evidence", {}
        else:
            packet = contents[-1]["parts"][0]["functionResponse"]["response"]
            ids = [f["id"] for f in packet["findings"]]
            if "You are ORCA's reviewer" in instruction:
                args = {
                    "verdict": "challenged" if ids else "agrees",
                    "summary": "Compared approved evidence",
                    "finding_ids": ids,
                    "limitations": ["Team-operated review"],
                }
            else:
                args = {
                    "action": "recompute_approved_plan" if ids else "no_change",
                    "summary": "Use approved evidence",
                    "finding_ids": ids,
                }
            name = "submit_decision"
        return {
            "model": "test-only",
            "content": {"role": "model", "parts": [{"functionCall": {"name": name, "args": args}}]},
        }
