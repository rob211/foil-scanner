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


def grade_for(
    value: float,
    target: float,
    yellow_floor: float | None = None,
    watch_floor: float | None = None,
) -> str | None:
    """Colour for a value against a trigger's target (spec 6). `watch_floor`
    opts into the maybe band below yellow; without it the behaviour is
    unchanged and a sub-yellow value still grades None."""
    if yellow_floor is None:
        yellow_floor = target * config.YELLOW_FACTOR
    if value > target * config.RED_FACTOR:
        return "red"
    if value >= target:
        return "green"
    if value >= yellow_floor:
        return "yellow"
    if watch_floor is not None and value >= watch_floor:
        return "watch"
    return None


def downgrade(grade: str, steps: int = 1, floor: str | None = None) -> str:
    """Drop `steps` colour steps, never below `floor`. The floor defaults to
    yellow because spec 6 says the off-angle and cross-swell downgrades never
    drop a window below yellow; only callers that mean the maybe band (the
    off-tide entrance penalty) pass floor="watch"."""
    if floor is None:
        floor = config.DOWNGRADE_FLOOR
    idx = config.GRADE_ORDER.index(grade)
    return config.GRADE_ORDER[max(config.GRADE_ORDER.index(floor), idx - steps)]


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
        if spans and t == spans[-1][1]:
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
    watch_floor: float | None = None,
) -> Window:
    start, end = span
    hours = [t for t in hour_map if start <= t < end]
    peak_time = max(
        hours, key=lambda t: (median(h.speed_kn for h in hour_map[t].values()), -t.timestamp())
    )
    peak_models = hour_map[peak_time]
    peak_median = median(h.speed_kn for h in peak_models.values())
    direction = vector_mean([h.dir_deg for h in peak_models.values()])
    grade = grade_for(peak_median, grade_target, watch_floor=watch_floor)
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


# ------------------------------------------------------------------- watch

def _covered(t: datetime, windows: list[Window], trigger_id: str) -> bool:
    return any(
        w.trigger_id == trigger_id and w.start <= t < w.end for w in windows
    )


def watch_windows(
    trigger_id: str,
    run_name: str,
    wind: WindForecast,
    sun: SunTimes,
    now: datetime,
    arc,
    target: float,
    existing: list[Window],
    yellow_floor: float | None = None,
) -> list[Window]:
    """The maybe band (Rob, 10 Aug 2026): flag it on the calendar so the
    models can be looked at, without claiming the run is on.

    Two ways in, both graded watch:
      - strength between config.WATCH_FACTOR * target and the yellow floor,
        at the normal consensus; or
      - strength at or above the yellow floor but on fewer models than
        config.MIN_MODELS_AGREE, down to config.MIN_MODELS_WATCH.

    The second is what a model bust looks like from inside the forecast: on
    10 Aug only ICON reached 18.5 kn for the reverse run while the coast was
    doing 32, and one model's word was worth nothing anywhere in the output.
    """
    if yellow_floor is None:
        yellow_floor = target * config.YELLOW_FACTOR
    watch_floor = config.watch_floor_for(yellow_floor)

    def pred(hw):
        return hw.speed_kn >= watch_floor and arc.contains(hw.dir_deg)

    hour_map = _qualifying_by_hour(wind, pred)
    hours = [
        t
        for t in _active_hours(hour_map, sun, config.MIN_MODELS_WATCH)
        if not _covered(t, existing, trigger_id)
    ]
    out = []
    for span in _group(hours):
        w = _window_from_span(
            trigger_id, run_name, span, hour_map, now, target,
            yellow_floor=yellow_floor, watch_floor=watch_floor,
        )
        weak = w.peak_median_kn < yellow_floor
        thin = w.models_agreeing < config.MIN_MODELS_AGREE
        if not weak and not thin:
            # Strong enough and agreed on, yet no window covers it: something
            # downstream vetoed it deliberately - the 4.6 cross-swell kill, or
            # a family that folds windows together. Re-adding it as a maybe
            # would quietly overrule a safety rule, so leave it to near_misses
            # (the watch digest lists those too).
            continue
        if thin:
            w.watch = (
                f"{w.models_agreeing} of {len(config.MODELS)} models only "
                f"(needs {config.MIN_MODELS_AGREE})"
            )
        else:
            w.watch = f"{w.peak_median_kn:.0f} kn, needs {yellow_floor:.0f}"
        w.grade = "watch"
        out.append(w)
    return out


def flag_thin_consensus(windows: list[Window]) -> list[Window]:
    """Mark windows carried by the bare minimum number of models.

    config.MIN_MODELS_AGREE is 2 of 4 and stays there - the point is not to
    demand more agreement, it is to stop a 2/4 window looking identical to a
    4/4 one. On 11 Aug the green lake call rested on ECMWF and ICON alone
    while GFS and UKMO never reached the floor; it happened to be right, but
    nothing on the calendar said it was a thinner call than the grade
    implied.

    Watch windows are left alone: they already carry their own reason, and
    saying "2 of 4 models" beside "1 of 4 models only" reads as a
    contradiction.
    """
    total = len(config.MODELS)
    for w in windows:
        if w.grade == "watch" or w.models_agreeing != config.MIN_MODELS_AGREE:
            continue
        if total <= config.MIN_MODELS_AGREE:
            continue          # nothing to be thin about
        w.title_tags.append(f"{w.models_agreeing} of {total} models")
        w.notes.append(
            f"only {w.models_agreeing} of {total} models reached the trigger; "
            "the others did not see it"
        )
    return windows


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
        for w in watch_windows(
            trigger_id, run_name, wind, sun, now, arc, target, windows
        ):
            if rare:
                w.title_tags.append("RARE")
            windows.append(w)
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
    # Watch windows skip the 4.6 swell pass deliberately: a maybe is a prompt
    # to go and look at the models, and killing it on swell would hide the
    # very days worth looking at.
    windows += watch_windows(
        "south_ocean", "South runs", wind, sun, now,
        config.SOUTH_WIND_ARC, config.SOUTH_TARGET_KN, windows,
    )
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
    lows = marine.low_tides()
    for ht in highs:
        # The very next low after this high, not the lowest point anywhere
        # in the rest of the forecast - min(..., key=sea_level_m) over all
        # remaining hours picked up whichever day's low was deepest, which
        # could be a spring low days later, stretching the "falling tide"
        # window across multiple tide cycles (found 2026-08-04 review).
        after = [lt for lt in lows if lt.time > ht.time]
        if not after:
            continue
        low = min(after, key=lambda h: h.time)
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
    return round(ht.sea_level_m + config.TIDE_HEIGHT_OFFSET_M, 2)


def _no_go_spans(marine: MarineForecast) -> list[tuple[datetime, datetime]]:
    """The last config.ENTRANCE_NO_GO_BEFORE_LOW_H hours before each low: peak
    ebb, with the whole lake draining through the entrance. Rob's hard no."""
    window = timedelta(hours=config.ENTRANCE_NO_GO_BEFORE_LOW_H)
    return [(lt.time - window, lt.time) for lt in marine.low_tides()]


def _subtract(
    span: tuple[datetime, datetime], blocks: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """`span` with `blocks` cut out of it. Survivors shorter than an hour are
    dropped, matching the minimum window everywhere else."""
    pieces = [span]
    for b_lo, b_hi in blocks:
        nxt = []
        for lo, hi in pieces:
            if b_hi <= lo or b_lo >= hi:
                nxt.append((lo, hi))
                continue
            if lo < b_lo:
                nxt.append((lo, min(hi, b_lo)))
            if hi > b_hi:
                nxt.append((max(lo, b_hi), hi))
        pieces = nxt
    return [(lo, hi) for lo, hi in pieces if hi - lo >= HOUR]


def _phase_spans(
    spans: list[tuple[datetime, datetime]],
    preferred: list[tuple[datetime, datetime, object]],
    no_go: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime, object, str, tuple | None]]:
    """Spec 4.2's tide rule (Rob, 11 Aug 2026). Three phases, not a gate:

    - **no go**: the last ENTRANCE_NO_GO_BEFORE_LOW_H hours before a low.
      Too much outflow. Cut out of the window entirely.
    - **preferred**: overlapping high tide to +ENTRANCE_TIDE_WINDOW_H, the
      run-out. Full rating.
    - **workable**: any other tide. The run is on, just not at its best, so
      it keeps its event and drops ENTRANCE_OFF_TIDE_DOWNGRADE steps.

    Returns (start, end, tide, phase, preferred_span). The window is *not*
    clipped to the preferred period: the whole workable stretch is runnable,
    so the event spans it and the description names the best part of it.
    """
    out: list[tuple[datetime, datetime, object, str, tuple | None]] = []
    for span in spans:
        for lo, hi in _subtract(span, no_go):
            best = None
            for p_lo, p_hi, tag in preferred:
                overlap = min(hi, p_hi) - max(lo, p_lo)
                if overlap >= HOUR and (best is None or overlap > best[0]):
                    best = (overlap, p_lo, p_hi, tag)
            if best is not None:
                # Split rather than grade the whole stretch off its best part
                # (Rob, 11 Aug 2026): the run-out becomes its own full-rating
                # event and the shoulders become downgraded ones, so the
                # calendar shows when it is actually good instead of one long
                # block carrying a single colour.
                _, p_lo, p_hi, tag = best
                inside = (max(lo, p_lo), min(hi, p_hi))
                shoulders = [(lo, inside[0]), (inside[1], hi)]
                out.append((inside[0], inside[1], tag, "preferred", None))
                for s_lo, s_hi in shoulders:
                    # Sub-hour remainders are dropped, matching the minimum
                    # window everywhere else rather than emitting a stub.
                    if s_hi - s_lo >= HOUR:
                        out.append((s_lo, s_hi, tag, "workable", None))
            elif preferred:
                mid = lo + (hi - lo) / 2
                nearest = min(
                    preferred,
                    key=lambda p: abs(((p[0] + (p[1] - p[0]) / 2) - mid).total_seconds()),
                )
                out.append((lo, hi, nearest[2], "workable", None))
            else:
                # No tide to label it with, but the run itself is not in
                # doubt - the no-go has already been subtracted. This used to
                # fall off the end of the elif and drop qualifying conditions
                # with no window AND no near miss, which is the silent calm
                # week spec 8 exists to prevent. high_tides() ignores the
                # first and last samples, so a high on the edge of the
                # forecast axis is invisible and this is reachable.
                out.append((lo, hi, None, "workable", None))
    return out


def _entrance_phases(spans, marine: MarineForecast):
    """Spec 4.2: preferred is the run-out, high tide to +2 h."""
    return _phase_spans(
        spans,
        [
            (h.time, h.time + timedelta(hours=config.ENTRANCE_TIDE_WINDOW_H), h)
            for h in marine.high_tides()
        ],
        _no_go_spans(marine),
    )


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
            light_ok = config.BAYSURF_WIND_MIN_KN <= hw.speed_kn <= config.BAYSURF_WIND_MAX_KN
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

        # Pick the falling-tide span this window sits in by how much of the
        # window it actually covers. This used to be a point test on peak_time
        # (`s[0] <= peak_time < s[1]`), which assumed tide boundaries land on
        # the hour; now that high/low tides are interpolated to sub-hourly
        # times (models.MarineForecast._extrema) a high at 10:20 made a
        # 10:00-14:00 window match nothing at all.
        def _overlap(s, start=start, end=end):
            return max(
                timedelta(0), min(end, s[1]) - max(start, s[0])
            ).total_seconds()

        tide_span = max(tide_spans, key=_overlap, default=None)
        if tide_span is None or _overlap(tide_span) <= 0:
            continue

        full_start, full_end, ideal_start, ideal_end = tide_span
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

    # Mode 1: E/NE swell on the run-out, graded on swell. Wind sorts the
    # result rather than deciding whether there is one - see the note on
    # ENTRANCE_M1_WIND_MAX_KN.
    def m1_favourable(hw):
        return (
            config.ENTRANCE_M1_WIND_ARC.contains(hw.dir_deg)
            or hw.speed_kn <= config.ENTRANCE_M1_WIND_MAX_KN
        )

    def m1_wind(hw):
        # Everything except unfavourable AND strong enough to ruin it. A
        # light onshore is only a downgrade; a hard offshore still grooms the
        # face. Only the pair together is a no-go.
        return m1_favourable(hw) or hw.speed_kn < config.ENTRANCE_WIND_NO_GO_KN

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
    for start, end, ht, tide_state, _ in _entrance_phases(_group(m1_hours), marine):
        hours = [t for t in m1_map if start <= t < end]
        peak_time = max(hours, key=lambda t: marine.at(t).swell_m)
        mh = marine.at(peak_time)
        grade = grade_for(
            mh.swell_m,
            config.ENTRANCE_M1_SWELL_TARGET_M,
            watch_floor=config.watch_floor_for(
                config.ENTRANCE_M1_SWELL_TARGET_M * config.YELLOW_FACTOR
            ),
        )
        if grade is None:
            continue
        peak_models = m1_map[peak_time]
        # Unfavourable wind makes it a watch, not a run: the swell and the
        # tide are still there, so it is worth a look rather than a deletion.
        wind_ok = any(m1_favourable(h) for h in peak_models.values())
        if not wind_ok:
            grade = "watch"
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
                high_tide=ht.time.isoformat() if ht is not None else None,
                high_tide_m=_tide_height_cd(ht) if ht is not None else None,
                tide_state=tide_state,
                watch=None if wind_ok else "wind not favourable for the swell",
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
    for start, end, ht, tide_state, _ in _entrance_phases(_group(m2_hours), marine):
        w = _window_from_span(
            "entrance_ne",
            "Lake Entrance (NE wind)",
            (start, end),
            m2_map,
            now,
            config.ENTRANCE_M2_TARGET_KN,
        )
        if ht is not None:
            w.high_tide = ht.time.isoformat()
            w.high_tide_m = _tide_height_cd(ht)
        w.tide_state = tide_state
        windows.append(w)

    # Off-tide is a penalty, not a veto (config.ENTRANCE_OFF_TIDE_DOWNGRADE).
    # Applied before the merge below so a merged pair grades off the penalised
    # values rather than sneaking a full-rating grade through.
    for w in windows:
        if w.tide_state == "workable" and config.ENTRANCE_OFF_TIDE_DOWNGRADE:
            w.grade = downgrade(
                w.grade, config.ENTRANCE_OFF_TIDE_DOWNGRADE, floor="watch"
            )
            w.title_tags.append("off-tide")
            w.notes.append(
                "runnable but not the preferred run-out (high tide to +2 h)"
            )
            if w.grade == "watch":
                # A window can be a watch for more than one reason - an
                # onshore breeze AND the wrong tide - and overwriting here
                # dropped whichever got there first.
                w.watch = "; ".join(x for x in (w.watch, "off tide") if x)

    # Same window in both modes -> one event noting both (spec 4.2).
    merged: list[Window] = []
    for w in sorted(windows, key=lambda w: (w.start, w.trigger_id)):
        clash = next(
            (
                m
                for m in merged
                if m.start < w.end and w.start < m.end
                and m.tide_state == w.tide_state
            ),
            None,
        )
        if clash is None:
            merged.append(w)
        else:
            clash.end = max(clash.end, w.end)
            if config.GRADE_ORDER.index(w.grade) > config.GRADE_ORDER.index(clash.grade):
                clash.grade = w.grade
            # Mode 2 (entrance_ne) never sets swell fields; if it survives as
            # clash over a merged-away Mode 1 (entrance_swell), the swell
            # numbers that justified Mode 1 used to be lost entirely bar a
            # bare grade mention in the note below.
            if clash.swell_m is None and w.swell_m is not None:
                clash.swell_m = w.swell_m
                clash.swell_dir_deg = w.swell_dir_deg
            clash.notes.append(f"also fires as {w.run_name} ({w.grade})")

    # This used to be a hardcoded `return merged, []`: the entrance was the
    # one family that recorded no near misses at all, so when it went quiet
    # there was nothing anywhere saying why (10 Aug 2026 review).
    # "Valid" now means anywhere the entrance can run at all, which under the
    # 11 Aug rule is everything except the no-go before each low - not just
    # the preferred run-out. Reporting a single-model miss outside those is
    # correct; reporting one inside them would blame model disagreement for
    # what is really too much outflow.
    horizon = (marine.hours[0].time, marine.hours[-1].time + HOUR)
    valid = _subtract(horizon, _no_go_spans(marine))
    misses = _single_model_misses(
        "entrance_swell", m1_map, sun, merged, valid_spans=valid,
    ) + _single_model_misses(
        "entrance_ne", m2_map, sun, merged, valid_spans=valid,
    )
    for w in merged:
        if w.tide_state == "workable" and w.high_tide is not None:
            misses.append(
                NearMiss(
                    trigger_id=w.trigger_id,
                    date=w.start.date().isoformat(),
                    start=w.start.isoformat(),
                    end=w.end.isoformat(),
                    reason="off_tide",
                    detail=(
                        f"runnable but outside the preferred run-out "
                        f"({datetime.fromisoformat(w.high_tide):%H:%M}-"
                        f"{datetime.fromisoformat(w.high_tide) + timedelta(hours=config.ENTRANCE_TIDE_WINDOW_H):%H:%M}"
                        f"); kept as {w.grade}"
                    ),
                )
            )
    return merged, misses


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
    # Same no-go as the standard runs - peak ebb is peak ebb whichever
    # direction you are running - but its own preferred window, the run-in.
    for lo, hi, tides, tide_state, _best in _phase_spans(
        _group(_active_hours(hour_map, sun, config.MIN_MODELS_AGREE)),
        [(t_lo, t_hi, (lt, ht)) for t_lo, t_hi, lt, ht in tide_spans],
        _no_go_spans(marine),
    ):
        # tides is None when the horizon holds no run-in window to name -
        # the run is still valid, it just cannot be labelled with a tide.
        lt, ht = tides if tides is not None else (None, None)
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
        if ht is not None:
            w.high_tide = ht.time.isoformat()
            w.high_tide_m = _tide_height_cd(ht)
        if lt is not None:
            w.notes.append(f"low tide {lt.time:%H:%M}")
        w.tide_state = tide_state
        if tide_state == "workable" and config.ENTRANCE_OFF_TIDE_DOWNGRADE:
            # Same call as the standard entrance runs: the incoming-tide gate
            # opened at 13:00 on 10 Aug while the wind was already firing from
            # 09:30, so a hard gate here throws away the front of a blow.
            w.grade = downgrade(
                w.grade, config.ENTRANCE_OFF_TIDE_DOWNGRADE, floor="watch"
            )
            w.title_tags.append("off-tide")
            w.notes.append("runnable but not the preferred run-in")
            if w.grade == "watch":
                w.watch = "off tide"
        windows.append(w)
    # Near misses are computed against the real windows only, before the watch
    # pass appends to `windows`. near_misses is the tuning record (spec 9) and
    # a watch event covering the same hours must not delete its entry there;
    # the two feed different consumers.
    misses.extend(
        _single_model_misses(
            "entrance_reverse",
            hour_map,
            sun,
            windows,
            valid_spans=[(t_lo, t_hi) for t_lo, t_hi, _, _ in tide_spans],
        )
    )
    windows += watch_windows(
        "entrance_reverse",
        "Entrance reverse run (Boronia Ave)",
        wind, sun, now,
        config.ENTRANCE_REVERSE_WIND_ARC,
        config.ENTRANCE_REVERSE_TARGET_KN,
        windows,
        yellow_floor=config.ENTRANCE_REVERSE_YELLOW_KN,
    )
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
    trigger_id: str,
    hour_map: dict,
    sun: SunTimes,
    accepted: list[Window],
    valid_spans: list[tuple[datetime, datetime]] | None = None,
) -> list[NearMiss]:
    """Spec 5: single-model hits are recorded but create no events.

    valid_spans restricts reporting to periods where the trigger could have
    fired at all (e.g. inside a tide gate). Without it, wind that qualifies
    on every model but only outside the gate gets misreported as a model-
    agreement problem, which it isn't."""
    solo = _active_hours(hour_map, sun, 1)
    misses = []
    for start, end in _group(solo):
        if any(
            w.trigger_id == trigger_id and w.start < end and start < w.end
            for w in accepted
        ):
            continue
        if valid_spans is not None and not any(
            s_lo < end and start < s_hi for s_lo, s_hi in valid_spans
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
