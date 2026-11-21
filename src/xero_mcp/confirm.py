"""One-time confirmation codes for actions the user must approve in the conversation.

When the app can't show its own confirmation prompt, the first call to approve_bills
returns a summary and a short code. The second call must bring that code
back. A code works once, only for the same action on exactly the items as they were
summarised (supplier, total, due date, …), and only for a few minutes. So the
action can't happen on the first call, can't be swapped for different bills,
and can't go ahead if something changed after the user saw the summary.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from typing import Any

CODE_TTL_SECONDS = 15 * 60
# No 0/O or 1/I/L, so a code read back by a person can't be misread.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def fingerprint(action: str, details: Any) -> str:
    """A hash of exactly what the user was shown."""
    data = json.dumps([action, details], sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


class Confirmations:
    def __init__(self, ttl: float = CODE_TTL_SECONDS):
        self._ttl = ttl
        self._pending: dict[str, tuple[str, str, float]] = {}

    def issue(self, action: str, fp: str) -> str:
        self._purge()
        code = "".join(secrets.choice(_ALPHABET) for _ in range(6))
        self._pending[code] = (action, fp, time.monotonic() + self._ttl)
        return code

    def redeem(self, action: str, code: str, fp: str) -> None:
        """Use up `code`, or raise ValueError saying why it can't be used."""
        self._purge()
        entry = self._pending.pop((code or "").strip().upper(), None)
        if entry is None:
            raise ValueError("That confirmation code isn't valid: it may have been used already or expired. "
                             "Call again without a code to get a new summary to show the user.")
        if entry[0] != action or entry[1] != fp:
            raise ValueError("These aren't the same items, or they changed since the summary was shown, "
                             "so nothing was done. Call again without a code and show the user the new summary.")

    def _purge(self) -> None:
        now = time.monotonic()
        for code in [c for c, (_, _, exp) in self._pending.items() if exp < now]:
            del self._pending[code]
