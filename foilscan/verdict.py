"""Verdict JSON: the stable contract between the scanner, the calendar and
the future dashboard (spec section 9). Bump SCHEMA_VERSION on any change of
field meaning."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import config
from .models import NearMiss, Observation, SourceStatus, Window


def build(
    now: datetime,
    sources: dict[str, SourceStatus],
    windows: list[Window],
    near_misses: list[NearMiss],
) -> dict:
    return {
        "schema_version": config.SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "sources": {name: asdict(s) for name, s in sources.items()},
        "windows": [
            {
                "trigger_id": w.trigger_id,
                "run_name": w.run_name,
                "date": w.start.date().isoformat(),
                "start": w.start.isoformat(),
                "end": w.end.isoformat(),
                "grade": w.grade,
                "peak_median_kn": w.peak_median_kn,
                "direction_deg": w.direction_deg,
                "models_agreeing": w.models_agreeing,
                "model_values": w.model_values,
                "title_tags": w.title_tags,
                "notes": w.notes,
                "swell_m": w.swell_m,
                "swell_dir_deg": w.swell_dir_deg,
                "high_tide": w.high_tide,
                "high_tide_m": w.high_tide_m,
                "spots": w.spots,
                "confidence": w.confidence,
                "live_status": w.live_status,
                "event_id": w.event_id,
                "foil_key": w.foil_key,
            }
            for w in windows
        ],
        "near_misses": [asdict(m) for m in near_misses],
    }


def write(verdict: dict, data_dir: str | Path = config.DATA_DIR) -> None:
    data_dir = Path(data_dir)
    history = data_dir / "history"
    history.mkdir(parents=True, exist_ok=True)
    text = json.dumps(verdict, indent=2, sort_keys=False) + "\n"
    (data_dir / "latest.json").write_text(text)
    day = verdict["generated_at"][:10]
    (history / f"{day}.json").write_text(text)


def _obs_dict(obs: Observation | None) -> dict | None:
    if obs is None:
        return None
    return {
        "station": obs.station,
        "time": obs.time.isoformat(),
        "speed_kn": round(obs.speed_kn, 1),
        "gust_kn": round(obs.gust_kn, 1),
        "dir_deg": obs.dir_deg,
    }


def build_live(
    now: datetime,
    obs: Observation | None,
    holfuy: Observation | None,
    checks: list[dict],
    notes: list[str],
) -> dict:
    """The live contract: data/live.json. obs is None only when the BOM fetch
    failed (the reason is in notes); the run still exits non-zero."""
    return {
        "schema_version": config.SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "obs": _obs_dict(obs),
        "holfuy": _obs_dict(holfuy),
        "checks": checks,
        "notes": notes,
    }


def write_live(payload: dict, data_dir: str | Path = config.DATA_DIR) -> None:
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "live.json").write_text(
        json.dumps(payload, indent=2, sort_keys=False) + "\n"
    )


def load_latest(data_dir: str | Path = config.DATA_DIR) -> dict:
    path = Path(data_dir) / "latest.json"
    if not path.exists():
        from .errors import StaleDataError

        raise StaleDataError(f"{path} does not exist; no successful scan recorded")
    return json.loads(path.read_text())
