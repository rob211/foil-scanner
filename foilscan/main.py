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


def _identical(a, b) -> bool:
    """True when two wind snapshots carry the same numbers for every model."""
    if a is None or b is None or set(a.models) != set(b.models):
        return False
    return all(
        [(h.time, h.speed_kn, h.dir_deg) for h in a.models[m]]
        == [(h.time, h.speed_kn, h.dir_deg) for h in b.models[m]]
        for m in a.models
    )


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
    if _identical(lake_wind, entrance_wind):
        # The lake and the entrance are 3 km apart and every model here is
        # 10-25 km resolution, so both points land in the same grid cell and
        # the two fetches return the same numbers - 671 of 671 model-hours on
        # 10 Aug 2026. Spec section 2 anticipated this ("land cells read low,
        # compare against the manual sites"). Recorded, not silently accepted:
        # it means the entrance triggers have no entrance-specific signal.
        sources["entrance_grid"] = SourceStatus(
            ok=True,
            detail="entrance forecast identical to lake: same model grid cell, "
            "no entrance-specific signal",
        )
    source_notes = skipped + [
        f"{name} failed: {s.error}" for name, s in sources.items() if not s.ok
    ]

    # Published so the live job can diff observations against it and leave a
    # trace when the models bust (spec 9, added 10 Aug 2026).
    expected = verdict.build_expected(now, {"lake": lake_wind, "ocean": ocean_wind})

    verdict.write(verdict.build(now, sources, windows, misses, expected), data_dir)
    try:
        plan = gcal.sync(windows, now, source_notes, dry_run=dry_run, near_misses=misses)
        for line in plan:
            print(line)
    finally:
        # Always re-write, even when sync raised. sync now finishes its pass
        # and reports failures at the end, so some windows genuinely did get
        # event ids - and without this they never reach disk, leaving the next
        # live run unable to verify anything at all. Losing the ids for the
        # events that worked was the whole failure the sync change existed to
        # stop, one layer up.
        verdict.write(verdict.build(now, sources, windows, misses, expected), data_dir)

    failed = [name for name, s in sources.items() if not s.ok]
    if failed:
        reason = "sources failed: " + ", ".join(failed)
        print(f"RUN FAILED (partial): {reason}", file=sys.stderr)
        if not dry_run:
            gcal.write_broken_event(reason, now)
        return 1
    runs = [w for w in windows if w.grade != "watch"]
    print(
        f"scan ok: {len(runs)} window(s), {len(windows) - len(runs)} watch, "
        f"{len(misses)} near miss(es)"
    )
    return 0


def snapshot_cmd(now: datetime) -> int:
    from . import snapshot

    for line in snapshot.run(now):
        print(line)
    return 0


def live_cmd(now: datetime, dry_run: bool, data_dir: str, force: bool = False) -> int:
    from . import live

    for line in live.run(now, dry_run=dry_run, data_dir=data_dir, force=force):
        print(line)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="foilscan")
    parser.add_argument("command", choices=["scan", "live", "snapshot"])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--data-dir", default=config.DATA_DIR)
    parser.add_argument(
        "--force",
        action="store_true",
        help="skip the cadence gate; the caller has already decided this tick "
        "should run (the Cloudflare Worker applies the same rule before it "
        "dispatches)",
    )
    args = parser.parse_args(argv)

    config.validate()
    now = datetime.now(config.TZ)
    try:
        if args.command == "scan":
            return scan(now, args.dry_run, args.data_dir)
        if args.command == "snapshot":
            # Read-only: never writes the calendar, so the SCANNER BROKEN
            # path below must not fire for it either.
            return snapshot_cmd(now)
        return live_cmd(now, args.dry_run, args.data_dir, args.force)
    except Exception as exc:  # noqa: BLE001 - loud failure path (spec 8)
        traceback.print_exc()
        reason = f"{type(exc).__name__}: {exc}"
        if not args.dry_run and args.command != "snapshot":
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
