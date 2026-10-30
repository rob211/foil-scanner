import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "worker_watchdog", Path(__file__).resolve().parent.parent / "scripts" / "worker_watchdog.py"
)
ww = importlib.util.module_from_spec(spec)
sys.modules["worker_watchdog"] = ww
spec.loader.exec_module(ww)

NOW = datetime(2026, 10, 30, 6, 0, tzinfo=timezone.utc)


def test_recent_dispatch_is_fine():
    assert ww.verdict(NOW - timedelta(minutes=50), NOW) is None


def test_long_gap_fails_with_hours():
    msg = ww.verdict(NOW - timedelta(hours=5), NOW)
    assert msg.startswith("WORKER SILENT") and "5.0 h" in msg


def test_no_dispatch_ever_fails():
    assert ww.verdict(None, NOW).startswith("WORKER SILENT")
