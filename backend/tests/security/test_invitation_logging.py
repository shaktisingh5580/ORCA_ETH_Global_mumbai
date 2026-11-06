"""Purpose: prevent invitation bearer disclosure without breaking HTTP access log formatting.
Behavior: exercise the real Uvicorn formatter against valid and malformed invitation paths.
Refs: API 3.2; main plan P02; Phase: P02 completion repair.
"""

import logging

from uvicorn.logging import AccessFormatter

from orca.infrastructure.auth.access_logging import InvitationAccessFilter


def test_invitation_token_redaction_preserves_uvicorn_access_format():
    token = "a" * 32 + "." + "b" * 64
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "POST", f"/v1/invitations/{token}/accept", "1.1", 200),
        None,
    )
    assert InvitationAccessFilter().filter(record)
    output = AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False
    ).format(record)
    assert token not in output and "[redacted]" in output and "200" in output
