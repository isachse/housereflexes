"""The loop: observe through housevitals, step every reflex, carry out its actions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .client import HousevitalsClient, HousevitalsError
from .config import Config, PvSurplusBoost
from .reflexes import BOOSTING, DONE, IDLE, Action, Observation, State, explain, step

_LOGGER = logging.getLogger(__name__)


class Runner:
    def __init__(self, config: Config, client: HousevitalsClient,
                 clock: Callable[[], datetime] | None = None, dry_run: bool | None = None):
        self.config = config
        self.client = client
        self.clock = clock or (lambda: datetime.now(config.tz))
        self.dry_run = config.dry_run if dry_run is None else dry_run
        self.reflexes = [r for r in config.reflexes if r.enabled]
        self.state_file = Path(config.state_file).expanduser() if config.state_file else None
        self.states: dict[str, State] = {r.name: State() for r in self.reflexes}
        self._reachable = True
        self._last_status: dict[str, float] = {}  # reflex -> time of the last status line
        self._load()

    async def run(self) -> None:
        mode = "dry run (no overrides are written)" if self.dry_run else "live"
        _LOGGER.info("housereflex started, %s: %s", mode,
                     ", ".join(r.name for r in self.reflexes) or "no enabled reflexes")
        while True:
            try:
                await self.tick()
            except Exception:
                _LOGGER.exception("Unexpected error; continuing")
            await asyncio.sleep(self.config.interval_s)

    async def tick(self) -> list[dict[str, Any]]:
        """One round. Returns what each reflex saw and did (for --once and tests)."""
        try:
            leases = [] if self.dry_run else await self.client.overrides()
        except HousevitalsError as err:
            self._set_reachable(False, err)
            return []
        values = await self._values()
        self._set_reachable(True)
        report = []
        for reflex in self.reflexes:
            state = self.states[reflex.name]
            owner = self.config.owner(reflex)
            active = (state.phase == BOOSTING if self.dry_run else
                      any(l["owner"] == owner and l["key"] == reflex.target.key for l in leases))
            source = values.get(reflex.source, {})
            obs = Observation(
                now=self.clock(),
                battery_soc=source.get(reflex.soc_key),
                grid_power=source.get(reflex.grid_key),
                done_value=values.get(reflex.done_appliance, {}).get(reflex.done_key),
                override_active=active,
            )
            before = state.phase
            action = step(reflex, state, obs)
            why = explain(reflex, state, obs)
            if state.phase != before:
                _LOGGER.info("%s: %s -> %s (%s)", reflex.name, before, state.phase, why)
                self._last_status[reflex.name] = obs.now.timestamp()
            else:
                self._status(reflex.name, obs.now.timestamp(), why)
            if action is not None:
                await self._act(reflex, state, action)
            report.append({"reflex": reflex.name, "phase": state.phase, "why": why,
                           "triggers_today": state.triggers,
                           "battery_soc": obs.battery_soc, "grid_power": obs.grid_power,
                           "done_value": obs.done_value, "override_active": obs.override_active,
                           "action": None if action is None else
                           {"kind": action.kind, "reason": action.reason,
                            "until": action.until.isoformat() if action.until else None}})
        self._save()
        return report

    def _status(self, name: str, ts: float, why: str) -> None:
        """A status line every status_interval_s, so quiet days leave a trace."""
        interval = self.config.status_interval_s
        if interval and ts - self._last_status.get(name, float("-inf")) >= interval:
            _LOGGER.info("%s: %s", name, why)
            self._last_status[name] = ts

    async def _values(self) -> dict[str, dict[str, Any]]:
        """Fetch every needed key, one request per appliance. Unreadable -> None."""
        wanted: dict[str, set[str]] = {}
        for r in self.reflexes:
            wanted.setdefault(r.source, set()).update({r.soc_key, r.grid_key})
            if r.done_appliance and r.done_key:
                wanted.setdefault(r.done_appliance, set()).add(r.done_key)
        out: dict[str, dict[str, Any]] = {}
        for appliance, keys in wanted.items():
            try:
                out[appliance] = await self.client.values(appliance, sorted(keys))
            except HousevitalsError as err:
                _LOGGER.warning("Cannot read %s: %s", appliance, err)
                out[appliance] = {}
        return out

    async def _act(self, reflex: PvSurplusBoost, state: State, action: Action) -> None:
        target, owner = reflex.target, self.config.owner(reflex)
        verb = "Would" if self.dry_run else "Now"
        if action.kind == "apply":
            _LOGGER.info("%s: %s override %s/%s = %s until %s (%s)", reflex.name, verb.lower(),
                         target.appliance, target.key, target.value,
                         action.until.strftime("%H:%M"), action.reason)
            if self.dry_run:
                return
            try:
                await self.client.put_override(target.appliance, target.key, target.value, owner,
                                               action.until.isoformat(timespec="seconds"),
                                               action.reason)
            except HousevitalsError as err:
                # A refusal (conflict, budget, not allowed) will not change today; an
                # outage might, so the reflex may arm again.
                refused = err.status is not None and 400 <= err.status < 500
                state.phase = DONE if refused else IDLE
                if not refused:
                    state.triggers -= 1
                _LOGGER.warning("%s: override failed (%s): %s", reflex.name,
                                "done for today" if refused else "will retry", err)
        else:
            _LOGGER.info("%s: %s end override %s/%s (%s)", reflex.name, verb.lower(),
                         target.appliance, target.key, action.reason)
            if self.dry_run:
                return
            try:
                await self.client.delete_override(target.appliance, target.key, owner)
            except HousevitalsError as err:
                if err.status != 404:  # 404: it has already ended
                    _LOGGER.warning("%s: ending the override failed, it ends on its own at the "
                                    "end of the window: %s", reflex.name, err)

    def _set_reachable(self, reachable: bool, err: Exception | None = None) -> None:
        if reachable != self._reachable:
            if reachable:
                _LOGGER.warning("housevitals reachable again")
            else:
                _LOGGER.warning("housevitals not reachable, reflexes paused: %s", err)
        self._reachable = reachable

    def _load(self) -> None:
        if self.state_file is None or not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            _LOGGER.warning("Ignoring unreadable state file %s: %s", self.state_file, err)
            return
        for name, item in data.items():
            if name in self.states:
                # Only the daily count survives a restart; a running override is
                # recovered from housevitals, anything else starts over.
                self.states[name] = State(phase=DONE if item.get("phase") == DONE else IDLE,
                                          day=item.get("day"), triggers=int(item.get("triggers", 0)))

    def _save(self) -> None:
        if self.state_file is None or self.dry_run:
            return
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({n: asdict(s) for n, s in self.states.items()}, indent=1),
                       encoding="utf-8")
        os.replace(tmp, self.state_file)


__all__ = ["Runner", "IDLE", "BOOSTING", "DONE"]
