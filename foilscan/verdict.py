"""Verdict JSON: the stable contract between the scanner, the calendar and
the future dashboard (spec section 9). Bump SCHEMA_VERSION on any change of
field meaning."""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import config
from .models import NearMiss, SourceStatus, Window


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


def load_latest(data_dir: str | Path = config.DATA_DIR) -> dict:
    path = Path(data_dir) / "latest.json"
    if not path.exists():
        from .errors import StaleDataError

        raise StaleDataError(f"{path} does not exist; no successful scan recorded")
    return json.loads(path.read_text())
