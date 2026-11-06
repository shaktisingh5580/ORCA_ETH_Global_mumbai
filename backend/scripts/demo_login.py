"""Purpose: obtain a real Supabase session for the local research demo without exposing passwords.
Behavior: prompt interactively, authenticate with Auth and save only a private local access token.
Refs: Supabase Auth password token API; accelerated demo decision; Phase: accelerated P02 integration.
"""

import getpass
import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    values = dotenv_values(ROOT / ".env")
    base = values["SUPABASE_URL"]
    assert base
    parsed = urlsplit(base)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".supabase.co")
        or parsed.username
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Pinned Supabase project required")
    email = input("Supabase user email: ").strip()
    password = getpass.getpass("Supabase password (hidden): ")
    payload = json.dumps({"email": email, "password": password}).encode()
    request = Request(
        base.rstrip("/") + "/auth/v1/token?grant_type=password",
        data=payload,
        headers={"apikey": str(values["SUPABASE_ANON_KEY"]), "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=15) as response:
        document = json.loads(response.read(65_536))
    token = document.get("access_token")
    if not isinstance(token, str) or "\n" in token or "\r" in token:
        raise ValueError("No valid session returned")
    folder = ROOT / ".cache/demo"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "access-token.txt").write_text(token, encoding="utf-8")
    print("Access token saved privately in .cache/demo/access-token.txt; do not share")
    print("Auth user UUID:", document["user"]["id"])


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Login unavailable:", type(error).__name__)
        raise SystemExit(1) from None
