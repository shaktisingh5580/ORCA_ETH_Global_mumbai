"""Purpose: request actual Gemini function calls with bounded transport and safe error reporting.
Behavior: round-robin paid project credentials, respect cooldowns and bound failover; never expose secrets.
Refs: Gemini generateContent API; ORCA_ARKIV_AI_PROTOTYPE_PLAN.md A02; Phase: A02.
"""

import re
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Any

import httpx


class ModelUnavailable(RuntimeError):
    pass


class GeminiReasoner:
    def __init__(self, key: str, model: str = "gemini-2.5-flash"):
        if not key or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,90}", model):
            raise ModelUnavailable("GEMINI_NOT_CONFIGURED")
        self.keys = list(dict.fromkeys(k.strip() for k in key.split(",") if k.strip()))
        if not self.keys:
            raise ModelUnavailable("GEMINI_NOT_CONFIGURED")
        self.model = model
        self.cursor = 0
        self.cooldowns: dict[str, float] = {}
        self.lock = threading.Lock()

    def select_key(self) -> str:
        with self.lock:
            for _ in self.keys:
                key = self.keys[self.cursor % len(self.keys)]
                self.cursor += 1
                if self.cooldowns.get(key, 0) <= time.monotonic():
                    return key
        raise ModelUnavailable("GEMINI_POOL_COOLDOWN")

    def cool(self, key: str, response: httpx.Response) -> None:
        delay = 60.0
        value = response.headers.get("Retry-After", "")
        try:
            delay = float(value)
        except ValueError:
            try:
                delay = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                pass
        try:
            for detail in response.json().get("error", {}).get("details", []):
                retry = detail.get("retryDelay", "")
                if re.fullmatch(r"[0-9.]+s", retry):
                    delay = max(delay, float(retry[:-1]))
        except (ValueError, TypeError, AttributeError):
            pass
        with self.lock:
            self.cooldowns[key] = time.monotonic() + max(1, delay)

    def turn(
        self, instruction: str, contents: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=45, follow_redirects=False) as client:
                payload = {
                    "systemInstruction": {"parts": [{"text": instruction}]},
                    "contents": contents,
                    "tools": [{"functionDeclarations": tools}],
                    "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
                    "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
                }
                for attempt in range(len(self.keys)):
                    key = self.select_key()
                    try:
                        response = client.post(
                            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                            headers={"x-goog-api-key": key},
                            json=payload,
                        )
                    except httpx.TransportError:
                        with self.lock:
                            self.cooldowns[key] = time.monotonic() + 15
                        if attempt + 1 < len(self.keys):
                            continue
                        raise ModelUnavailable("GEMINI_TRANSPORT_UNAVAILABLE") from None
                    if response.status_code == 429 or response.status_code >= 500:
                        self.cool(key, response)
                        if attempt + 1 < len(self.keys):
                            continue
                    break
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
