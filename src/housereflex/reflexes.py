"""Reflex logic: a pure state machine per reflex, fed with observations.

No I/O here, so every decision can be tested with plain values and a fake clock.

    idle ──surplus──► armed ──held for arm_hold_s──► boosting ──────────────► done
      ▲                 │                              │  tank hot, or max_duration_s
      └──no surplus─────┘      deficit too long ───────┘  / latest_end reached
                               (idle again if the daily limit allows)

A boost ends at whichever comes first: `max_duration_s` after its start or
`latest_end`. That end is also the override's end time in housevitals, so the heat
pump returns to normal even if housereflex stops.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .config import PvSurplusBoost

IDLE, ARMED, BOOSTING, DONE = "idle", "armed", "boosting", "done"


@dataclass
class Observation:
    now: datetime  # time-zone aware, local
    battery_soc: float | None
    grid_power: float | None  # W, positive = import, negative = export
    done_value: float | None  # e.g. the hot water temperature
    override_active: bool  # housevitals currently holds this reflex's override
    battery_power: float | None = None  # W, positive = discharging, negative = charging

    @property
    def deficit(self) -> float | None:
        """Power the house draws from grid and battery together (W); negative = surplus."""
        if self.grid_power is None:
            return None
        return self.grid_power + (self.battery_power or 0.0)


@dataclass
class State:
    phase: str = IDLE
    since: float | None = None  # when the current phase started
    deficit_since: float | None = None  # while boosting: deficit above the limit since
    day: str | None = None  # local date the trigger count belongs to
    triggers: int = 0
    until: float | None = None  # while boosting: end of the boost


@dataclass(frozen=True)
class Action:
    kind: str  # "apply" or "release"
    reason: str
    until: datetime | None = None  # apply: end of the override


def latest_end(reflex: PvSurplusBoost, now: datetime) -> datetime:
    return datetime.combine(now.date(), reflex.latest_end, now.tzinfo)


def boost_end(reflex: PvSurplusBoost, now: datetime) -> datetime:
    """End of a boost starting now: max_duration_s or latest_end, whichever is first."""
    return min(now + timedelta(seconds=reflex.max_duration_s), latest_end(reflex, now))


def step(reflex: PvSurplusBoost, state: State, obs: Observation) -> Action | None:
    """Advance the state for one observation; returns what to do, if anything."""
    ts = obs.now.timestamp()
    today = obs.now.date().isoformat()
    if state.day != today:
        state.day, state.triggers = today, 0
        if state.phase == DONE:
            _enter(state, IDLE, ts)

    if obs.override_active and state.phase != BOOSTING:
        # Running override found (e.g. after a restart, or a release that failed): adopt it.
        _enter(state, BOOSTING, ts)
        state.triggers = max(state.triggers, 1)
        state.until = None  # unknown here; housevitals holds the real end time

    if state.phase == BOOSTING:
        return _boosting(reflex, state, obs, ts)
    if state.phase == DONE:
        return None

    if not _ready(reflex, state, obs):
        if state.phase != IDLE:
            _enter(state, IDLE, ts)
        return None
    if state.phase == IDLE:
        _enter(state, ARMED, ts)
        return None
    if ts - state.since >= reflex.arm_hold_s:
        _enter(state, BOOSTING, ts)
        state.triggers += 1
        until = boost_end(reflex, obs.now)
        state.until = until.timestamp()
        return Action("apply", f"PV surplus: battery {obs.battery_soc:g} %, "
                               f"export {-obs.grid_power:g} W for {reflex.arm_hold_s / 60:g} min", until)
    return None


def _ready(reflex: PvSurplusBoost, state: State, obs: Observation) -> bool:
    remaining = (latest_end(reflex, obs.now) - obs.now).total_seconds()
    return (
        remaining >= reflex.min_duration_s
        and state.triggers < reflex.max_per_day
        and not _tank_done(reflex, obs)
        and obs.battery_soc is not None and obs.battery_soc >= reflex.min_battery_soc
        and obs.grid_power is not None and -obs.grid_power >= reflex.min_export_w
    )


def _tank_done(reflex: PvSurplusBoost, obs: Observation) -> bool:
    return (reflex.done_min is not None and obs.done_value is not None
            and obs.done_value >= reflex.done_min)


def explain(reflex: PvSurplusBoost, state: State, obs: Observation) -> str:
    """One line on where the reflex stands and which condition holds it back."""

    def check(label: str, value: float | None, unit: str, op: str, limit: float, ok: bool) -> str:
        if value is None:
            return f"{label} unknown (no)"
        return f"{label} {value:g}{unit} {op} {limit:g}{unit} ({'yes' if ok else 'no'})"

    if state.phase == BOOSTING:
        end = (datetime.fromtimestamp(state.until, obs.now.tzinfo) if state.until
               else latest_end(reflex, obs.now))
        parts = [f"until {end:%H:%M}",
                 check("deficit", obs.deficit, " W", "<=", reflex.max_deficit_w,
                       obs.deficit is None or obs.deficit <= reflex.max_deficit_w)]
    else:
        remaining = (latest_end(reflex, obs.now) - obs.now).total_seconds()
        export = None if obs.grid_power is None else -obs.grid_power
        parts = [
            f"{reflex.min_duration_s / 60:g} min left before {reflex.latest_end:%H:%M} "
            f"({'yes' if remaining >= reflex.min_duration_s else 'no'})",
            check("battery", obs.battery_soc, " %", ">=", reflex.min_battery_soc,
                  obs.battery_soc is not None and obs.battery_soc >= reflex.min_battery_soc),
            check("export", export, " W", ">=", reflex.min_export_w,
                  export is not None and export >= reflex.min_export_w),
        ]
    if reflex.done_min is not None:
        parts.append(check(reflex.done_key, obs.done_value, "", "<", reflex.done_min,
                           obs.done_value is not None and obs.done_value < reflex.done_min))
    parts.append(f"boosts today {state.triggers}/{reflex.max_per_day}")
    return f"{state.phase}: " + ", ".join(parts)


def _boosting(reflex: PvSurplusBoost, state: State, obs: Observation, ts: float) -> Action | None:
    if not obs.override_active:
        _enter(state, DONE, ts)  # ended in housevitals (expired or removed)
        return None
    if _tank_done(reflex, obs):
        _enter(state, DONE, ts)
        return Action("release", f"{reflex.done_key} reached {obs.done_value:g}")
    end = state.until or latest_end(reflex, obs.now).timestamp()
    if ts >= end:
        _enter(state, DONE, ts)
        return Action("release", "time limit reached")
    deficit = obs.deficit
    if deficit is not None and deficit > reflex.max_deficit_w:
        state.deficit_since = state.deficit_since or ts
        if ts - state.deficit_since >= reflex.abort_hold_s:
            _enter(state, IDLE if state.triggers < reflex.max_per_day else DONE, ts)
            return Action("release", f"drawing {deficit:g} W from grid and battery for "
                                     f"{reflex.abort_hold_s / 60:g} min")
    else:
        state.deficit_since = None
    return None


def _enter(state: State, phase: str, ts: float) -> None:
    state.phase, state.since, state.deficit_since = phase, ts, None
    if phase != BOOSTING:
        state.until = None
