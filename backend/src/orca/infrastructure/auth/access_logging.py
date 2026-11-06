"""Purpose: prevent bearer invitation tokens from entering default HTTP access logs.
Behavior: redact invitation acceptance paths while preserving method, status and request metadata.
Refs: API 3.2 invitation token handling; main plan P02; Phase: P02 completion repair.
"""

import logging
import re


class InvitationAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            arguments = list(record.args)
            arguments[2] = re.sub(
                r"(/v1/invitations/)[^\s/?]+(/accept)", r"\1[redacted]\2", str(arguments[2])
            )
            record.args = tuple(arguments)
            return True
        message = record.getMessage()
        safe = re.sub(r"(/v1/invitations/)[^\s/?]+(/accept)", r"\1[redacted]\2", message)
        if safe != message:
            record.msg, record.args = safe, ()
        return True


def install_invitation_redaction() -> None:
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, InvitationAccessFilter) for f in logger.filters):
        logger.addFilter(InvitationAccessFilter())
