"""Entry point: `python -m foilscan scan|live [--dry-run]`.

Exit code is non-zero on ANY failure, including partial source failures
where the pass still completed (spec 8.5, 8.9).
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime

from . import config, fetch, gcal, verdict
from .models import SourceStatus
from .triggers import (
    baysurf_windows,
    entrance_reverse_windows,
    entrance_windows,
    hill60_windows,
    lake_windows,
    ne_windows,
    south_windows,
)


def _capture(sources: dict, name: str, fn):
    """Run one fetcher, record its status, return its snapshot or None."""
    try:
        snap = fn()
        sources[name] = SourceStatus(ok=True, fetched_at=datetime.now(config.TZ).isoformat())
        return snap
    except Exception as exc:  # noqa: BLE001 - recorded and re-raised via exit code
        sources[name] = SourceStatus(ok=False, error=f"{type(exc).__name__}: {exc}")
        print(f"SOURCE FAILED {name}: {exc}", file=sys.stderr)
        return None


def scan(now: datetime, dry_run: bool, data_dir: str) -> int:
    sources: dict[str, SourceStatus] = {}

    sun = _capture(sources, "open_meteo_sun", lambda: fetch.fetch_sun(now))
    lake_wind = _capture(
        sources, "open_meteo_wind_lake", lambda: fetch.fetch_wind(config.LAKE, now)
    )
    entrance_wind = _capture(
        sources,
        "open_meteo_wind_entrance",
        lambda: fetch.fetch_wind(config.ENTRANCE, now),
    )
    ocean_wind = _capture(
        sources, "open_meteo_wind_ocean", lambda: fetch.fetch_wind(config.OCEAN, now)
    )
    marine = _capture(sources, "open_meteo_marine", lambda: fetch.fetch_marine(now))

    windows, misses = [], []
    skipped: list[str] = []
    if sun is None:
        # Daylight clipping is load-bearing for every trigger (spec 4).
        skipped.append("ALL triggers skipped: sunrise/sunset unavailable")
    else:
        if lake_wind is not None:
            w, m = lake_windows(lake_wind, sun, now)
            windows += w
            misses += m
        else:
            skipped.append("lake triggers skipped: lake wind unavailable")

        if entrance_wind is not None and marine is not None:
            w, m = entrance_windows(entrance_wind, marine, sun, now)
            windows += w
            misses += m
            w, m = entrance_reverse_windows(entrance_wind, marine, sun, now)
            windows += w
            misses += m
        else:
            skipped.append("entrance triggers skipped: wind or marine unavailable")

        if ocean_wind is not None and marine is not None:
            sw, m = south_windows(ocean_wind, marine, sun, now)
            windows += sw
            misses += m
            windows += hill60_windows(sw, marine, sun, now)
            w, m = ne_windows(ocean_wind, marine, sun, now)
            windows += w
            misses += m
            w, m = baysurf_windows(ocean_wind, marine, sun, now)
            windows += w
            misses += m
        else:
            skipped.append("ocean triggers skipped: wind or marine unavailable")

    windows.sort(key=lambda w: (w.start, w.trigger_id))
    source_notes = skipped + [
        f"{name} failed: {s.error}" for name, s in sources.items() if not s.ok
    ]

    verdict.write(verdict.build(now, sources, windows, misses), data_dir)
    plan = gcal.sync(windows, now, source_notes, dry_run=dry_run)
    for line in plan:
        print(line)
    # Re-write with event ids filled in by sync.
    verdict.write(verdict.build(now, sources, windows, misses), data_dir)

    failed = [name for name, s in sources.items() if not s.ok]
    if failed:
        reason = "sources failed: " + ", ".join(failed)
        print(f"RUN FAILED (partial): {reason}", file=sys.stderr)
        if not dry_run:
            gcal.write_broken_event(reason, now)
        return 1
    print(f"scan ok: {len(windows)} window(s), {len(misses)} near miss(es)")
    return 0


def live_cmd(now: datetime, dry_run: bool, data_dir: str) -> int:
    from . import live

    for line in live.run(now, dry_run=dry_run, data_dir=data_dir):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="foilscan")
    parser.add_argument("command", choices=["scan", "live"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--data-dir", default=config.DATA_DIR)
    args = parser.parse_args(argv)

    config.validate()
    now = datetime.now(config.TZ)
    try:
        if args.command == "scan":
            return scan(now, args.dry_run, args.data_dir)
        return live_cmd(now, args.dry_run, args.data_dir)
    except Exception as exc:  # noqa: BLE001 - loud failure path (spec 8)
        traceback.print_exc()
        reason = f"{type(exc).__name__}: {exc}"
        if not args.dry_run:
            try:
                gcal.write_broken_event(reason, now)
                print("wrote SCANNER BROKEN calendar flag", file=sys.stderr)
            except Exception as flag_exc:  # noqa: BLE001
                print(
                    f"could not write SCANNER BROKEN flag either: {flag_exc}",
                    file=sys.stderr,
                )
        return 1


if __name__ == "__main__":
    sys.exit(main())
