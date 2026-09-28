"""Reflex logic: a pure state machine per reflex, fed with observations.

No I/O here, so every decision can be tested with plain values and a fake clock.

    idle ──conditions true──► armed ──held for arm_hold_s──► boosting ──► done
      ▲                         │                              │ tank hot / window over
      └──conditions false───────┘       import too high ───────┘ (idle again if the
                                                                  daily limit allows)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .config import PvSurplusBoost

IDLE, ARMED, BOOSTING, DONE = "idle", "armed", "boosting", "done"


@dataclass
class Observation:
    now: datetime  # time-zone aware, local
    battery_soc: float | None
    grid_power: float | None  # W, positive = import, negative = export
    done_value: float | None  # e.g. the hot water temperature
    override_active: bool  # housevitals currently holds this reflex's override


@dataclass
class State:
    phase: str = IDLE
    since: float | None = None  # when the current phase started
    import_since: float | None = None  # while boosting: import above the limit since
    day: str | None = None  # local date the trigger count belongs to
    triggers: int = 0


@dataclass(frozen=True)
class Action:
    kind: str  # "apply" or "release"
    reason: str
    until: datetime | None = None  # apply: end of the override


def step(reflex: PvSurplusBoost, state: State, obs: Observation) -> Action | None:
    """Advance the state for one observation; returns what to do, if anything."""
    ts = obs.now.timestamp()
    today = obs.now.date().isoformat()
    if state.day != today:
        state.day, state.triggers = today, 0
        if state.phase == DONE:
            _enter(state, IDLE, ts)
    in_window = reflex.window_start <= obs.now.time() < reflex.window_end

    if obs.override_active and state.phase != BOOSTING:
        # Running override found (e.g. after a restart, or a release that failed): adopt it.
        _enter(state, BOOSTING, ts)
        state.triggers = max(state.triggers, 1)

    if state.phase == BOOSTING:
        return _boosting(reflex, state, obs, ts, in_window)
    if state.phase == DONE:
        return None

    tank_done = (reflex.done_min is not None and obs.done_value is not None
                 and obs.done_value >= reflex.done_min)
    ready = (
        in_window
        and state.triggers < reflex.max_per_day
        and not tank_done
        and obs.battery_soc is not None and obs.battery_soc >= reflex.min_battery_soc
        and obs.grid_power is not None and -obs.grid_power >= reflex.min_export_w
    )
    if not ready:
        if state.phase != IDLE:
            _enter(state, IDLE, ts)
        return None
    if state.phase == IDLE:
        _enter(state, ARMED, ts)
        return None
    if ts - state.since >= reflex.arm_hold_s:
        _enter(state, BOOSTING, ts)
        state.triggers += 1
        until = datetime.combine(obs.now.date(), reflex.window_end, obs.now.tzinfo)
        return Action("apply", f"PV surplus: battery {obs.battery_soc:g} %, "
                               f"export {-obs.grid_power:g} W for {reflex.arm_hold_s / 60:g} min", until)
    return None


def explain(reflex: PvSurplusBoost, state: State, obs: Observation) -> str:
    """One line on where the reflex stands and which condition holds it back."""

    def check(label: str, value: float | None, unit: str, op: str, limit: float, ok: bool) -> str:
        if value is None:
            return f"{label} unknown (no)"
        return f"{label} {value:g}{unit} {op} {limit:g}{unit} ({'yes' if ok else 'no'})"

    window = f"{reflex.window_start:%H:%M}-{reflex.window_end:%H:%M}"
    in_window = reflex.window_start <= obs.now.time() < reflex.window_end
    export = None if obs.grid_power is None else -obs.grid_power
    parts = [
        f"window {window} ({'yes' if in_window else 'no'})",
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


def _boosting(reflex: PvSurplusBoost, state: State, obs: Observation, ts: float,
              in_window: bool) -> Action | None:
    if not obs.override_active:
        _enter(state, DONE, ts)  # ended in housevitals (expired or removed)
        return None
    if reflex.done_min is not None and obs.done_value is not None and obs.done_value >= reflex.done_min:
        _enter(state, DONE, ts)
        return Action("release", f"{reflex.done_key} reached {obs.done_value:g}")
    if not in_window:
        _enter(state, DONE, ts)
        return Action("release", "time window over")
    if obs.grid_power is not None and obs.grid_power > reflex.max_import_w:
        state.import_since = state.import_since or ts
        if ts - state.import_since >= reflex.abort_hold_s:
            _enter(state, IDLE if state.triggers < reflex.max_per_day else DONE, ts)
            return Action("release", f"importing {obs.grid_power:g} W for "
                                     f"{reflex.abort_hold_s / 60:g} min")
    else:
        state.import_since = None
    return None


def _enter(state: State, phase: str, ts: float) -> None:
    state.phase, state.since, state.import_since = phase, ts, None
