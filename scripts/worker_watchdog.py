"""Fail when the Cloudflare Worker has stopped triggering runs.

The Worker drives the real cadence; GitHub's own `schedule` is a sparse
backstop. A dead Worker (most often an expired GITHUB_TOKEN) used to leave
only a `poll gap:` note on the dashboard - the backstop kept going, nothing
went red, nothing was emailed. This runs on backstop ticks only and fails
the run when no Worker dispatch has been seen for WORKER_SILENT_AFTER, so
failure_report.py files it like any other fault.

    python scripts/worker_watchdog.py

Stdlib + the `gh` CLI that every runner has; GH_TOKEN and GITHUB_REPOSITORY
come from the workflow.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# Live is dispatched at least hourly, scan every 2 h. Three hours of nothing
# is not a shed tick or two, it is a Worker that is not there.
WORKER_SILENT_AFTER = timedelta(hours=3)


def last_dispatch(repo: str) -> datetime | None:
    out = subprocess.run(
        ["gh", "api", f"repos/{repo}/actions/runs?event=workflow_dispatch&per_page=1"],
        text=True, capture_output=True, check=True,
    ).stdout
    runs = json.loads(out).get("workflow_runs") or []
    if not runs:
        return None
    return datetime.fromisoformat(runs[0]["created_at"].replace("Z", "+00:00"))


def verdict(last: datetime | None, now: datetime) -> str | None:
    """The failure line, or None when the Worker looks alive."""
    if last is None:
        return "WORKER SILENT: no Worker-dispatched run on record at all"
    gap = now - last
    if gap < WORKER_SILENT_AFTER:
        return None
    return (f"WORKER SILENT: no Worker-dispatched run for {gap.total_seconds() / 3600:.1f} h "
            f"(last {last:%Y-%m-%d %H:%M} UTC)")


def main() -> int:
    now = datetime.now(timezone.utc)
    msg = verdict(last_dispatch(os.environ["GITHUB_REPOSITORY"]), now)
    if msg:
        print(msg)
        return 1
    print("worker ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
