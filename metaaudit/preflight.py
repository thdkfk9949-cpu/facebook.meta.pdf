"""One cheap call that answers "is my setup actually working?".

The full audit makes six or more requests and pages through the whole
account, so a credential mistake surfaces slowly and in the middle of other
output. This does the smallest possible read first and reports exactly what
the token can see, which is what you want before the real run.
"""

from __future__ import annotations

import os

from .api import GraphClient, GraphError
from .currency import fmt, offset_for
from .fetch import ACCOUNT_FIELDS

# Numeric account_status values Meta returns, and what they mean for spending.
ACCOUNT_STATUS = {
    1: ("ACTIVE", True),
    2: ("DISABLED", False),
    3: ("UNSETTLED", False),
    7: ("PENDING_RISK_REVIEW", False),
    8: ("PENDING_SETTLEMENT", False),
    9: ("IN_GRACE_PERIOD", False),
    100: ("PENDING_CLOSURE", False),
    101: ("CLOSED", False),
}

# Probe exactly what the audit will ask for. A preflight that reads a smaller
# set can pass and still be followed by a permission failure mid-audit.
FIELDS = ",".join(ACCOUNT_FIELDS)


def run(
    client: GraphClient, account_id: str, *, env_file: str | None = None
) -> tuple[int, str]:
    """Return (exit_code, report). Never raises: the report is the diagnosis."""
    lines: list[str] = []
    lines.append(f"Graph API version : {client.api_version}")
    lines.append(f"Ad account        : {account_id}")
    lines.append("")

    try:
        account = client.get(account_id, {"fields": FIELDS})
    except GraphError as exc:
        lines.append("FAILED to read the ad account.")
        lines.append("")
        lines.append(str(exc))
        return 3, "\n".join(lines)

    currency = account.get("currency", "USD")
    status_code = account.get("account_status")
    status_name, spendable = ACCOUNT_STATUS.get(
        status_code, (f"UNKNOWN({status_code})", True)
    )

    lines.append("OK  token is valid and can read this ad account.")
    lines.append("")
    lines.append(f"  Name          : {account.get('name', '(unnamed)')}")
    lines.append(f"  Currency      : {currency}  (minor unit divisor {offset_for(currency)})")
    lines.append(f"  Timezone      : {account.get('timezone_name', 'unknown')}")
    lines.append(f"  Status        : {status_name}")
    spent = account.get("amount_spent")
    if spent is not None:
        # amount_spent is a lifetime total in minor units.
        lines.append(
            f"  Lifetime spend: {fmt(float(spent) / offset_for(currency), currency)}"
        )
    if not spendable:
        reason = account.get("disable_reason")
        lines.append("")
        lines.append(
            f"  WARNING: this account is {status_name}"
            + (f" (disable_reason={reason})" if reason else "")
            + " and is not delivering. The audit will still run, but recent"
            " windows may be empty."
        )

    # Reading the account node can succeed while the edges the audit needs are
    # blocked, so prove one edge read too.
    lines.append("")
    try:
        probe = client.get(
            f"{account_id}/campaigns", {"fields": "id", "limit": 1}
        )
    except GraphError as exc:
        lines.append("FAILED to list campaigns — the audit needs this edge.")
        lines.append("")
        lines.append(str(exc))
        return 3, "\n".join(lines)

    found = len(probe.get("data") or [])
    lines.append(
        "OK  campaigns edge is readable"
        + ("." if found else " (no campaigns returned — the account may be empty).")
    )

    if client.rate.worst_pct:
        lines.append("")
        lines.append(f"  Rate limit usage: {client.rate.worst_pct:.0f}% of quota")

    lines.append("")
    lines.append(f"  API requests used by this check: {client.request_count}")
    lines.append("")
    lines.append("Setup looks good. Run the audit:")
    runner = "py" if os.name == "nt" else "python3"
    cmd = f"  {runner} -m metaaudit"
    if env_file:
        cmd += f" --env-file {env_file}"
    lines.append(cmd)
    return 0, "\n".join(lines)
