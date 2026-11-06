"""Purpose: request actual Gemini function calls with bounded transport and safe error reporting.
Behavior: execute no model text, retain function-call parts and never expose provider credentials.
Refs: Gemini generateContent API; ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A02; Phase: A02.
"""

import re
from typing import Any

import httpx


class ModelUnavailable(RuntimeError):
    pass


class GeminiReasoner:
    def __init__(self, key: str, model: str = "gemini-2.5-flash"):
        if not key or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,90}", model):
            raise ModelUnavailable("GEMINI_NOT_CONFIGURED")
        self.key, self.model = key, model

    def turn(
        self, instruction: str, contents: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=45, follow_redirects=False) as client:
                response = client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                    headers={"x-goog-api-key": self.key},
                    json={
                        "systemInstruction": {"parts": [{"text": instruction}]},
                        "contents": contents,
                        "tools": [{"functionDeclarations": tools}],
                        "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
                        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
                    },
                )
                if response.status_code != 200:
                    raise ModelUnavailable(f"GEMINI_HTTP_{response.status_code}")
                if len(response.content) > 262144:
                    raise ModelUnavailable("GEMINI_RESPONSE_TOO_LARGE")
                document = response.json()
                content = document["candidates"][0]["content"]
                if not isinstance(content.get("parts"), list):
                    raise ValueError("missing model parts")
                return {
                    "content": content,
                    "model": document.get("modelVersion", self.model),
                    "usage": document.get("usageMetadata", {}),
                }
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as error:
            raise ModelUnavailable("GEMINI_INVALID_OR_UNAVAILABLE_RESPONSE") from error
