"""Pure trigger engine: snapshots in, windows and near misses out.

No I/O in this module. Every rule cites its section in docs/SPEC.md.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from statistics import median

from . import config
from .models import MarineForecast, MarineHour, NearMiss, SunTimes, Window, WindForecast

HOUR = timedelta(hours=1)

COMPASS_16 = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def compass(deg: float) -> str:
    return COMPASS_16[int((deg % 360) / 22.5 + 0.5) % 16]


def ang_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def vector_mean(degs: list[float]) -> float:
    x = sum(math.cos(math.radians(d)) for d in degs)
    y = sum(math.sin(math.radians(d)) for d in degs)
    return math.degrees(math.atan2(y, x)) % 360.0


def grade_for(value: float, target: float, yellow_floor: float | None = None) -> str | None:
    if yellow_floor is None:
        yellow_floor = target * config.YELLOW_FACTOR
    if value > target * config.RED_FACTOR:
        return "red"
    if value >= target:
        return "green"
    if value >= yellow_floor:
        return "yellow"
    return None


def downgrade(grade: str, steps: int = 1) -> str:
    idx = config.GRADE_ORDER.index(grade)
    return config.GRADE_ORDER[max(0, idx - steps)]


# ---------------------------------------------------------------- consensus

def _qualifying_by_hour(forecast: WindForecast, predicate) -> dict[datetime, dict]:
    """time -> {model_id: HourWind} for models whose hour meets the predicate."""
    out: dict[datetime, dict] = {}
    for model_id, series in forecast.models.items():
        for hw in series:
            if predicate(hw):
                out.setdefault(hw.time, {})[model_id] = hw
    return out


def _active_hours(hour_map: dict, sun: SunTimes, min_agree: int) -> list[datetime]:
    return sorted(
        t
        for t, models in hour_map.items()
        if len(models) >= min_agree and sun.daylight(t)
    )


def _group(times: list[datetime]) -> list[tuple[datetime, datetime]]:
    """Consecutive hourly timestamps -> (start, end) spans. End is exclusive
    (last hour + 1 h)."""
    spans = []
    for t in times:
        if spans and t - spans[-1][1] == timedelta(0):
            spans[-1][1] = t + HOUR
        elif spans and t == spans[-1][1]:
            spans[-1][1] = t + HOUR
        else:
            spans.append([t, t + HOUR])
    return [(s, e) for s, e in spans]


def _window_from_span(
    trigger_id: str,
    run_name: str,
    span: tuple[datetime, datetime],
    hour_map: dict,
    now: datetime,
    grade_target: float,
    yellow_floor: float | None = None,
) -> Window:
    start, end = span
    hours = [t for t in hour_map if start <= t < end]
    peak_time = max(
        hours, key=lambda t: (median(h.speed_kn for h in hour_map[t].values()), -t.timestamp())
    )
    peak_models = hour_map[peak_time]
    peak_median = median(h.speed_kn for h in peak_models.values())
    direction = vector_mean([h.dir_deg for h in peak_models.values()])
    grade = grade_for(peak_median, grade_target)
    if grade is None:
        # NE ocean's yellow band (10-15 kn, spec 6) is wider than the
        # generic 0.9 factor; an explicit floor extends yellow down to it.
        if yellow_floor is not None and peak_median >= yellow_floor:
            grade = "yellow"
        else:
            raise AssertionError(
                f"{trigger_id}: window peak {peak_median} below yellow floor; predicate bug"
            )
    offset = (start.date() - now.date()).days
    return Window(
        trigger_id=trigger_id,
        run_name=run_name,
        start=start,
        end=end,
        grade=grade,
        peak_time=peak_time,
        peak_median_kn=round(peak_median, 1),
        direction_deg=round(direction, 0),
        models_agreeing=len(peak_models),
        model_values={
            config.MODELS[m]: round(h.speed_kn, 1) for m, h in peak_models.items()
        },
        confidence=(
            "low (long range)"
            if offset >= config.LOW_CONFIDENCE_FROM_DAY_OFFSET
            else "normal"
        ),
    )


# ---------------------------------------------------------------- families

def lake_windows(
    wind: WindForecast, sun: SunTimes, now: datetime
) -> tuple[list[Window], list[NearMiss]]:
    """Spec 4.1. No ocean swell on the lake, so no 4.6 pass."""
    windows, misses = [], []
    for trigger_id, (run_name, arc, target, rare) in config.LAKE_RUNS.items():
        floor = target * config.YELLOW_FACTOR

        def pred(hw, arc=arc, floor=floor):
            return hw.speed_kn >= floor and arc.contains(hw.dir_deg)

        hour_map = _qualifying_by_hour(wind, pred)
        for span in _group(_active_hours(hour_map, sun, config.MIN_MODELS_AGREE)):
            w = _window_from_span(trigger_id, run_name, span, hour_map, now, target)
            if rare:
                w.title_tags.append("RARE")
            windows.append(w)
        misses.extend(_single_model_misses(trigger_id, hour_map, sun, windows))
    return windows, misses


def _swell_compatibility(
    w: Window, marine: MarineForecast, misses: list[NearMiss]
) -> Window | None:
    """Spec 4.6 for ocean downwinders. Returns the (possibly downgraded)
    window, or None when the swell kills it. South-family windows with swell
    from the south band never reach here (4.3 table wins)."""
    mh = marine.at(w.peak_time)
    w.swell_m = round(mh.swell_m, 2)
    w.swell_dir_deg = round(mh.swell_dir_deg, 0)
    if mh.swell_m < config.SWELL_IGNORE_BELOW_M:
        return w
    d = ang_diff(mh.swell_dir_deg, w.direction_deg)
    label = f"{mh.swell_m:.1f} m {compass(mh.swell_dir_deg)}"
    if d <= config.SWELL_ALIGNED_MAX_DEG:
        if mh.swell_m <= config.SWELL_ALIGNED_MAX_M:
            w.notes.append(f"aligned swell {label}")
            return w
        reason = "aligned_swell_too_big"
        detail = f"aligned swell {label} over {config.SWELL_ALIGNED_MAX_M} m"
    elif mh.swell_m >= config.SWELL_CROSS_KILL_M:
        reason = "cross_swell"
        detail = f"cross swell {label}, {d:.0f} deg off the wind"
    else:
        w.grade = downgrade(w.grade)
        w.title_tags.append(f"cross swell {label}")
        return w
    misses.append(
        NearMiss(
            trigger_id=w.trigger_id,
            date=w.start.date().isoformat(),
            start=w.start.isoformat(),
            end=w.end.isoformat(),
            reason=reason,
            detail=detail,
        )
    )
    return None


def south_windows(
    wind: WindForecast, marine: MarineForecast, sun: SunTimes, now: datetime
) -> tuple[list[Window], list[NearMiss]]:
    """Spec 4.3: south wind 20 kn+, swell size narrows the run list."""
    windows, misses = [], []
    floor = config.SOUTH_TARGET_KN * config.YELLOW_FACTOR

    def pred(hw):
        return hw.speed_kn >= floor and config.SOUTH_WIND_ARC.contains(hw.dir_deg)

    hour_map = _qualifying_by_hour(wind, pred)
    for span in _group(_active_hours(hour_map, sun, config.MIN_MODELS_AGREE)):
        w = _window_from_span(
            "south_ocean", "South runs", span, hour_map, now, config.SOUTH_TARGET_KN
        )
        mh = marine.at(w.peak_time)
        in_south_band = config.SOUTH_SWELL_ARC.contains(mh.swell_dir_deg)
        if in_south_band and mh.swell_m >= config.SWELL_IGNORE_BELOW_M:
            # 4.3 table wins: Hill 60 handles any size south swell and wind.
            w.swell_m = round(mh.swell_m, 2)
            w.swell_dir_deg = round(mh.swell_dir_deg, 0)
            if mh.swell_m < config.SOUTH_SWELL_SMALL_MAX_M:
                runs = config.SOUTH_RUNS_SMALL
            elif mh.swell_m <= config.SOUTH_SWELL_MEDIUM_MAX_M:
                runs = config.SOUTH_RUNS_MEDIUM
            else:
                runs = config.SOUTH_RUNS_LARGE
            w.notes.append(
                f"south swell {mh.swell_m:.1f} m {compass(mh.swell_dir_deg)}"
            )
        else:
            kept = _swell_compatibility(w, marine, misses)
            if kept is None:
                continue
            runs = config.SOUTH_RUNS_SMALL
            if w.swell_m is not None and not in_south_band and w.swell_m >= config.SWELL_IGNORE_BELOW_M:
                w.notes.append(
                    f"swell {w.swell_m:.1f} m {compass(w.swell_dir_deg)} outside the south band"
                )
        # Keep the base name; expose the qualifying spots so the dashboard and
        # calendar can list them individually rather than as one lumped run.
        w.spots = list(runs)
        windows.append(w)
    misses.extend(_single_model_misses("south_ocean", hour_map, sun, windows))
    return windows, misses


def _ne_active_map(wind: WindForecast, sun: SunTimes) -> dict[datetime, dict]:
    """Spec 4.5 ladder, per model: an hour is active once the required
    build hours have already blown within the same daylight qualifying run."""
    active: dict[datetime, dict] = {}
    for model_id, series in wind.models.items():
        run: list = []  # consecutive qualifying daylight hours for this model
        for hw in series:
            qualifies = (
                hw.speed_kn >= config.NE_FLOOR_KN
                and config.NE_WIND_ARC.contains(hw.dir_deg)
                and sun.daylight(hw.time)
            )
            if not qualifies:
                run = []
                continue
            if run and hw.time - run[-1].time != HOUR:
                run = []
            run.append(hw)
            for rung_speed, rung_hours in config.NE_LADDER:
                build = run[-(rung_hours + 1) : -1]
                if len(build) == rung_hours and all(
                    b.speed_kn >= rung_speed for b in build
                ):
                    active.setdefault(hw.time, {})[model_id] = hw
                    break
    return active


def ne_windows(
    wind: WindForecast, marine: MarineForecast, sun: SunTimes, now: datetime
) -> tuple[list[Window], list[NearMiss]]:
    windows, misses = [], []
    hour_map = _ne_active_map(wind, sun)
    for span in _group(_active_hours(hour_map, sun, config.MIN_MODELS_AGREE)):
        w = _window_from_span(
            "ne_ocean",
            "NE run (Easty / South Beach / Sandon)",
            span,
            hour_map,
            now,
            config.NE_TARGET_KN,
            yellow_floor=config.NE_FLOOR_KN,
        )
        if not config.NE_TRUE_ARC.contains(w.direction_deg):
            w.grade = downgrade(w.grade)
            w.title_tags.append(f"off-angle {compass(w.direction_deg)}")
        kept = _swell_compatibility(w, marine, misses)
        if kept is None:
            continue
        windows.append(w)
    misses.extend(_single_model_misses("ne_ocean", hour_map, sun, windows))
    return windows, misses


def _tide_spans(marine: MarineForecast) -> list[tuple[datetime, datetime, MarineHour]]:
    # Entrance only works on the run-out: high tide to +2 h, not before it.
    window = timedelta(hours=config.ENTRANCE_TIDE_WINDOW_H)
    return [(ht.time, ht.time + window, ht) for ht in marine.high_tides()]


def _baysurf_tide_spans(marine: MarineForecast) -> list[tuple[datetime, datetime, datetime, datetime]]:
    spans = []
    highs = marine.high_tides()
    for ht in highs:
        after = [h for h in marine.hours if h.time > ht.time]
        if not after:
            continue
        low = min(after, key=lambda h: h.sea_level_m)
        full_start = ht.time
        full_end = low.time + HOUR
        if full_end <= full_start:
            continue
        ideal_start = full_start + (full_end - full_start) / 2
        ideal_end = full_end
        spans.append((full_start, full_end, ideal_start, ideal_end))
    return spans


def _tide_height_cd(ht: MarineHour) -> float:
    """Modelled high-tide height referenced to chart datum (tide-table style)."""
    return round(ht.sea_level_m + config.PORT_KEMBLA_MSL_ABOVE_CD_M, 2)


def _intersect_tides(
    spans: list[tuple[datetime, datetime]], tides
) -> list[tuple[datetime, datetime, MarineHour]]:
    pieces = []
    for start, end in spans:
        for t_lo, t_hi, ht in tides:
            lo, hi = max(start, t_lo), min(end, t_hi)
            if hi - lo >= HOUR:
                pieces.append((lo, hi, ht))
    return pieces


def baysurf_windows(
    wind: WindForecast, marine: MarineForecast, sun: SunTimes, now: datetime
) -> tuple[list[Window], list[NearMiss]]:
    windows: list[Window] = []
    misses: list[NearMiss] = []
    tide_spans = _baysurf_tide_spans(marine)

    hour_map: dict[datetime, dict] = {}
    for model_id, series in wind.models.items():
        for hw in series:
            if not sun.daylight(hw.time):
                continue
            mh = marine.at(hw.time)
            if mh.swell_m < config.BAYSURF_SWELL_YELLOW_M:
                continue
            if not config.BAYSURF_SWELL_ARC.contains(mh.swell_dir_deg):
                continue
            light_ok = 4.0 <= hw.speed_kn <= config.BAYSURF_WIND_MAX_KN
            strong_ok = hw.speed_kn > config.BAYSURF_WIND_MAX_KN and config.BAYSURF_STRONG_WIND_ARC.contains(hw.dir_deg)
            if light_ok or strong_ok:
                hour_map.setdefault(hw.time, {})[model_id] = hw

    for span in _group(_active_hours(hour_map, sun, config.MIN_MODELS_AGREE)):
        start, end = span
        hours = [t for t in hour_map if start <= t < end]
        peak_time = max(hours, key=lambda t: (marine.at(t).swell_m, -t.timestamp()))
        peak_models = hour_map[peak_time]
        peak_median_kn = median(h.speed_kn for h in peak_models.values())
        direction = vector_mean([h.dir_deg for h in peak_models.values()])
        mh = marine.at(peak_time)
        grade = grade_for(
            mh.swell_m,
            config.BAYSURF_SWELL_TARGET_M,
            yellow_floor=config.BAYSURF_SWELL_YELLOW_M,
        )
        if grade is None:
            continue

        tide_span = next(
            (s for s in tide_spans if s[0] <= peak_time < s[1]),
            None,
        )
        if tide_span is None:
            continue

        full_start, full_end, ideal_start, ideal_end = tide_span
        if not (start < full_end and full_start < end):
            continue
        if not (start < ideal_end and ideal_start < end):
            grade = downgrade(grade)
            title_tag = "tide"
        else:
            title_tag = None

        offset = (start.date() - now.date()).days
        w = Window(
            trigger_id="baysurf",
            run_name="Baysurf",
            start=start,
            end=end,
            grade=grade,
            peak_time=peak_time,
            peak_median_kn=round(peak_median_kn, 1),
            direction_deg=round(direction, 0),
            models_agreeing=len(peak_models),
            model_values={config.MODELS[m]: round(h.speed_kn, 1) for m, h in peak_models.items()},
            swell_m=round(mh.swell_m, 2),
            swell_dir_deg=round(mh.swell_dir_deg, 0),
            confidence=(
                "low (long range)"
                if offset >= config.LOW_CONFIDENCE_FROM_DAY_OFFSET
                else "normal"
            ),
        )
        if title_tag is not None:
            w.title_tags.append(title_tag)
        windows.append(w)

    misses.extend(_single_model_misses("baysurf", hour_map, sun, windows))
    return windows, misses


def entrance_windows(
    wind: WindForecast, marine: MarineForecast, sun: SunTimes, now: datetime
) -> tuple[list[Window], list[NearMiss]]:
    """Spec 4.2: both modes need daylight and the high-tide window."""
    windows: list[Window] = []
    tides = _tide_spans(marine)

    # Mode 1: light west (or near calm) wind, E/NE swell, graded on swell.
    def m1_wind(hw):
        return (
            hw.speed_kn <= config.ENTRANCE_M1_WIND_MAX_KN
            and config.ENTRANCE_M1_WIND_ARC.contains(hw.dir_deg)
        ) or hw.speed_kn < config.ENTRANCE_M1_CALM_KN

    swell_floor = config.ENTRANCE_M1_SWELL_TARGET_M * config.YELLOW_FACTOR
    swell_ok = {
        h.time
        for h in marine.hours
        if h.swell_m >= swell_floor
        and config.ENTRANCE_M1_SWELL_ARC.contains(h.swell_dir_deg)
    }
    m1_map = {
        t: models
        for t, models in _qualifying_by_hour(wind, m1_wind).items()
        if t in swell_ok
    }
    m1_hours = _active_hours(m1_map, sun, config.MIN_MODELS_AGREE)
    for start, end, ht in _intersect_tides(_group(m1_hours), tides):
        hours = [t for t in m1_map if start <= t < end]
        peak_time = max(hours, key=lambda t: marine.at(t).swell_m)
        mh = marine.at(peak_time)
        grade = grade_for(mh.swell_m, config.ENTRANCE_M1_SWELL_TARGET_M)
        if grade is None:
            continue
        peak_models = m1_map[peak_time]
        offset = (start.date() - now.date()).days
        windows.append(
            Window(
                trigger_id="entrance_swell",
                run_name="Lake Entrance (swell)",
                start=start,
                end=end,
                grade=grade,
                peak_time=peak_time,
                peak_median_kn=round(
                    median(h.speed_kn for h in peak_models.values()), 1
                ),
                direction_deg=round(
                    vector_mean([h.dir_deg for h in peak_models.values()]), 0
                ),
                models_agreeing=len(peak_models),
                model_values={
                    config.MODELS[m]: round(h.speed_kn, 1)
                    for m, h in peak_models.items()
                },
                swell_m=round(mh.swell_m, 2),
                swell_dir_deg=round(mh.swell_dir_deg, 0),
                high_tide=ht.time.isoformat(),
                high_tide_m=_tide_height_cd(ht),
                confidence=(
                    "low (long range)"
                    if offset >= config.LOW_CONFIDENCE_FROM_DAY_OFFSET
                    else "normal"
                ),
            )
        )

    # Mode 2: strong NE/ENE wind, swell irrelevant, graded on wind.
    floor = config.ENTRANCE_M2_TARGET_KN * config.YELLOW_FACTOR

    def m2_wind(hw):
        return hw.speed_kn >= floor and config.ENTRANCE_M2_WIND_ARC.contains(hw.dir_deg)

    m2_map = _qualifying_by_hour(wind, m2_wind)
    m2_hours = _active_hours(m2_map, sun, config.MIN_MODELS_AGREE)
    for start, end, ht in _intersect_tides(_group(m2_hours), tides):
        w = _window_from_span(
            "entrance_ne",
            "Lake Entrance (NE wind)",
            (start, end),
            m2_map,
            now,
            config.ENTRANCE_M2_TARGET_KN,
        )
        w.high_tide = ht.time.isoformat()
        w.high_tide_m = _tide_height_cd(ht)
        windows.append(w)

    # Same window in both modes -> one event noting both (spec 4.2).
    merged: list[Window] = []
    for w in sorted(windows, key=lambda w: (w.start, w.trigger_id)):
        clash = next(
            (m for m in merged if m.start < w.end and w.start < m.end), None
        )
        if clash is None:
            merged.append(w)
        else:
            clash.end = max(clash.end, w.end)
            if config.GRADE_ORDER.index(w.grade) > config.GRADE_ORDER.index(clash.grade):
                clash.grade = w.grade
            clash.notes.append(f"also fires as {w.run_name} ({w.grade})")
    return merged, []


def _entrance_reverse_tide_spans(
    marine: MarineForecast,
) -> list[tuple[datetime, datetime, MarineHour, MarineHour]]:
    """Spec 4.8: the reverse run works the incoming tide, the opposite gate
    to the standard entrance runs. Opens config.ENTRANCE_REVERSE_START_AFTER_LOW_H
    after low tide, closes config.ENTRANCE_REVERSE_END_BEFORE_HIGH_H before the
    next high."""
    start_after = timedelta(hours=config.ENTRANCE_REVERSE_START_AFTER_LOW_H)
    end_before = timedelta(hours=config.ENTRANCE_REVERSE_END_BEFORE_HIGH_H)
    highs = marine.high_tides()
    spans = []
    for lt in marine.low_tides():
        after = [h for h in highs if h.time > lt.time]
        if not after:
            continue
        ht = min(after, key=lambda h: h.time)
        start, end = lt.time + start_after, ht.time - end_before
        if end > start:
            spans.append((start, end, lt, ht))
    return spans


def entrance_reverse_windows(
    wind: WindForecast, marine: MarineForecast, sun: SunTimes, now: datetime
) -> tuple[list[Window], list[NearMiss]]:
    """Spec 4.8: Entrance reverse run (Boronia Ave). W/NW wind, 20 kn+ (25 kn+
    is best), gated to the incoming tide rather than the run-out."""
    windows, misses = [], []
    floor = config.ENTRANCE_REVERSE_YELLOW_KN

    def pred(hw):
        return hw.speed_kn >= floor and config.ENTRANCE_REVERSE_WIND_ARC.contains(hw.dir_deg)

    hour_map = _qualifying_by_hour(wind, pred)
    tide_spans = _entrance_reverse_tide_spans(marine)
    for start, end in _group(_active_hours(hour_map, sun, config.MIN_MODELS_AGREE)):
        for t_lo, t_hi, lt, ht in tide_spans:
            lo, hi = max(start, t_lo), min(end, t_hi)
            if hi - lo < HOUR:
                continue
            w = _window_from_span(
                "entrance_reverse",
                "Entrance reverse run (Boronia Ave)",
                (lo, hi),
                hour_map,
                now,
                config.ENTRANCE_REVERSE_TARGET_KN,
                yellow_floor=config.ENTRANCE_REVERSE_YELLOW_KN,
            )
            if not config.ENTRANCE_REVERSE_TRUE_ARC.contains(w.direction_deg):
                w.grade = downgrade(w.grade)
                w.title_tags.append(f"off-angle {compass(w.direction_deg)}")
            w.high_tide = ht.time.isoformat()
            w.high_tide_m = _tide_height_cd(ht)
            w.notes.append(f"low tide {lt.time:%H:%M}")
            windows.append(w)
    misses.extend(_single_model_misses("entrance_reverse", hour_map, sun, windows))
    return windows, misses


def hill60_windows(
    south: list[Window], marine: MarineForecast, sun: SunTimes, now: datetime
) -> list[Window]:
    """Spec 4.4: large south swell alone fires Hill 60, any wind. Windows
    overlapping a south-wind event are folded into it instead."""
    floor = config.HILL60_SWELL_TARGET_M * config.YELLOW_FACTOR
    hours = sorted(
        h.time
        for h in marine.hours
        if h.swell_m >= floor
        and config.SOUTH_SWELL_ARC.contains(h.swell_dir_deg)
        and sun.daylight(h.time)
    )
    out = []
    for start, end in _group(hours):
        overlap = next(
            (s for s in south if s.start < end and start < s.end), None
        )
        span_hours = [t for t in hours if start <= t < end]
        peak_time = max(span_hours, key=lambda t: marine.at(t).swell_m)
        mh = marine.at(peak_time)
        if overlap is not None:
            overlap.notes.append(
                f"standalone Hill 60 swell run also fires: {mh.swell_m:.1f} m "
                f"{compass(mh.swell_dir_deg)}"
            )
            continue
        grade = grade_for(mh.swell_m, config.HILL60_SWELL_TARGET_M)
        if grade is None:
            continue
        offset = (start.date() - now.date()).days
        out.append(
            Window(
                trigger_id="hill60_swell",
                run_name="Hill 60 swell run",
                start=start,
                end=end,
                grade=grade,
                peak_time=peak_time,
                peak_median_kn=0.0,
                direction_deg=round(mh.swell_dir_deg, 0),
                models_agreeing=0,
                model_values={},
                swell_m=round(mh.swell_m, 2),
                swell_dir_deg=round(mh.swell_dir_deg, 0),
                notes=["swell event, wind not required"],
                confidence=(
                    "low (long range)"
                    if offset >= config.LOW_CONFIDENCE_FROM_DAY_OFFSET
                    else "normal"
                ),
            )
        )
    return out


def _single_model_misses(
    trigger_id: str, hour_map: dict, sun: SunTimes, accepted: list[Window]
) -> list[NearMiss]:
    """Spec 5: single-model hits are recorded but create no events."""
    solo = _active_hours(hour_map, sun, 1)
    misses = []
    for start, end in _group(solo):
        if any(
            w.trigger_id == trigger_id and w.start < end and start < w.end
            for w in accepted
        ):
            continue
        models = sorted(
            {m for t in hour_map for m in hour_map[t] if start <= t < end}
        )
        if len(models) >= config.MIN_MODELS_AGREE:
            # Enough models overall but never at the same hour; still a miss.
            detail = f"models never agree on the same hour: {models}"
        else:
            detail = f"only {models} sees it"
        misses.append(
            NearMiss(
                trigger_id=trigger_id,
                date=start.date().isoformat(),
                start=start.isoformat(),
                end=end.isoformat(),
                reason="single_model",
                detail=detail,
            )
        )
    return misses


def evaluate(
    lake_wind: WindForecast,
    entrance_wind: WindForecast,
    ocean_wind: WindForecast,
    marine: MarineForecast,
    sun: SunTimes,
    now: datetime,
) -> tuple[list[Window], list[NearMiss]]:
    windows: list[Window] = []
    misses: list[NearMiss] = []

    lw, lm = lake_windows(lake_wind, sun, now)
    windows += lw
    misses += lm

    ew, em = entrance_windows(entrance_wind, marine, sun, now)
    windows += ew
    misses += em

    erw, erm = entrance_reverse_windows(entrance_wind, marine, sun, now)
    windows += erw
    misses += erm

    sw, sm = south_windows(ocean_wind, marine, sun, now)
    windows += sw
    misses += sm

    windows += hill60_windows(sw, marine, sun, now)

    nw, nm = ne_windows(ocean_wind, marine, sun, now)
    windows += nw
    misses += nm

    bw, bm = baysurf_windows(ocean_wind, marine, sun, now)
    windows += bw
    misses += bm

    windows.sort(key=lambda w: (w.start, w.trigger_id))
    return windows, misses
