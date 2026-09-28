"""Configuration: where housevitals runs, how to authenticate, and the reflexes."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

MIN_TOKEN_LENGTH = 16


class ConfigError(Exception):
    """Invalid configuration."""


def _check_keys(where: str, data: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(f"{where}: unknown option(s): {', '.join(sorted(unknown))}")


def _time(where: str, value: Any) -> time:
    try:
        return time.fromisoformat(str(value).zfill(5))
    except ValueError as err:
        raise ConfigError(f"{where}: expected HH:MM, got '{value}'") from err


@dataclass(frozen=True)
class Target:
    """The register a reflex overrides through housevitals."""

    appliance: str
    key: str
    value: float | int | str


@dataclass(frozen=True)
class PvSurplusBoost:
    """Raise a setpoint while there is sustained PV surplus, so a heat pump turns the
    surplus into stored heat (hot water or buffer) instead of feeding it into the grid.

    Arms when the battery is at least `min_battery_soc` and at least `min_export_w`
    are exported for `arm_hold_s`; there is no start window, surplus implies daylight.
    The override then lasts until whichever comes first: `max_duration_s` after the
    start or `latest_end` (local time). It is ended early when `done_key` reaches
    `done_min` (e.g. the tank is hot) or when the house draws more than
    `max_deficit_w` from grid and battery together for `abort_hold_s` (clouds, dusk).
    A boost is not started if less than `min_duration_s` remain before `latest_end`.
    """

    name: str
    target: Target
    source: str  # appliance providing battery and grid values (the inverter)
    max_duration_s: float = 10800.0
    min_duration_s: float = 1800.0
    latest_end: time = time(18, 0)
    min_battery_soc: float = 90.0
    min_export_w: float = 2500.0
    arm_hold_s: float = 600.0
    done_appliance: str | None = None
    done_key: str | None = None
    done_min: float | None = None
    max_deficit_w: float = 500.0
    abort_hold_s: float = 300.0
    max_per_day: int = 1
    enabled: bool = True
    # Keys of the source appliance (housevitals data points).
    soc_key: str = "battery_soc"
    grid_key: str = "grid_power"  # positive = import, negative = export
    battery_key: str = "battery_power"  # positive = discharging, negative = charging

    OPTIONS = {"name", "type", "enabled", "target", "boost", "source", "arm", "done",
               "abort", "max_per_day"}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PvSurplusBoost:
        name = str(data.get("name") or "")
        where = f"Reflex '{name}'"
        if not name.replace("_", "").isalnum():
            raise ConfigError("Every reflex needs a 'name' of letters, digits and _")
        if "window" in data:
            raise ConfigError(f"{where}: 'window' was replaced by 'boost' "
                              "(max_duration_s, min_duration_s, latest_end)")
        _check_keys(where, data, cls.OPTIONS)
        target = data.get("target") or {}
        _check_keys(f"{where}, target", target, {"appliance", "key", "value"})
        if not all(k in target for k in ("appliance", "key", "value")):
            raise ConfigError(f"{where}: target needs appliance, key and value")
        boost = data.get("boost") or {}
        _check_keys(f"{where}, boost", boost, {"max_duration_s", "min_duration_s", "latest_end"})
        source = data.get("source") or {}
        _check_keys(f"{where}, source", source,
                    {"appliance", "battery_soc", "grid_power", "battery_power"})
        if not source.get("appliance"):
            raise ConfigError(f"{where}: source.appliance (the inverter) is required")
        arm = data.get("arm") or {}
        _check_keys(f"{where}, arm", arm, {"min_battery_soc", "min_export_w", "hold_s"})
        done = data.get("done") or {}
        _check_keys(f"{where}, done", done, {"appliance", "key", "min"})
        if done and not all(k in done for k in ("appliance", "key", "min")):
            raise ConfigError(f"{where}: done needs appliance, key and min")
        abort = data.get("abort") or {}
        if "max_import_w" in abort:
            raise ConfigError(f"{where}: abort.max_import_w was replaced by abort.max_deficit_w "
                              "(grid import plus battery discharge)")
        _check_keys(f"{where}, abort", abort, {"max_deficit_w", "hold_s"})
        reflex = cls(
            name=name,
            target=Target(str(target["appliance"]), str(target["key"]), target["value"]),
            source=str(source["appliance"]),
            max_duration_s=float(boost.get("max_duration_s", 10800)),
            min_duration_s=float(boost.get("min_duration_s", 1800)),
            latest_end=_time(f"{where}, boost.latest_end", boost.get("latest_end", "18:00")),
            soc_key=str(source.get("battery_soc", "battery_soc")),
            grid_key=str(source.get("grid_power", "grid_power")),
            battery_key=str(source.get("battery_power", "battery_power")),
            min_battery_soc=float(arm.get("min_battery_soc", 90)),
            min_export_w=float(arm.get("min_export_w", 2500)),
            arm_hold_s=float(arm.get("hold_s", 600)),
            done_appliance=str(done["appliance"]) if done else None,
            done_key=str(done["key"]) if done else None,
            done_min=float(done["min"]) if done else None,
            max_deficit_w=float(abort.get("max_deficit_w", 500)),
            abort_hold_s=float(abort.get("hold_s", 300)),
            max_per_day=int(data.get("max_per_day", 1)),
            enabled=bool(data.get("enabled", True)),
        )
        if not 0 < reflex.min_duration_s <= reflex.max_duration_s:
            raise ConfigError(f"{where}: need 0 < boost.min_duration_s <= boost.max_duration_s")
        if reflex.max_per_day < 1:
            raise ConfigError(f"{where}: max_per_day must be at least 1")
        return reflex


REFLEX_TYPES = {"pv_surplus_boost": PvSurplusBoost}


@dataclass
class Config:
    url: str = "http://127.0.0.1:8080"
    token_file: str | None = None
    timezone: str = "Europe/Berlin"
    interval_s: float = 60.0
    status_interval_s: float = 1800.0  # status line per reflex; 0 = only on phase changes
    dry_run: bool = True
    owner_prefix: str = "housereflex"
    state_file: str = "~/.local/state/housereflex/state.json"
    reflexes: list[PvSurplusBoost] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def owner(self, reflex: PvSurplusBoost) -> str:
        return f"{self.owner_prefix}/{reflex.name}"

    def token(self) -> str | None:
        """HOUSEREFLEX_TOKEN, else the token file. Never part of the config file."""
        token = os.environ.get("HOUSEREFLEX_TOKEN")
        if not token and self.token_file:
            try:
                token = Path(self.token_file).expanduser().read_text(encoding="utf-8")
            except OSError as err:
                raise ConfigError(f"Cannot read token_file: {err}") from err
        token = (token or "").strip() or None
        if token is not None and len(token) < MIN_TOKEN_LENGTH:
            raise ConfigError(f"The token must have at least {MIN_TOKEN_LENGTH} characters")
        return token

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Config:
        _check_keys("Config", data, {"housevitals", "timezone", "interval_s", "status_interval_s", "dry_run",
                                     "owner_prefix", "state_file", "reflexes"})
        hv = data.get("housevitals") or {}
        _check_keys("housevitals", hv, {"url", "token_file"})
        reflexes = []
        for item in data.get("reflexes", []):
            kind = item.get("type")
            if kind not in REFLEX_TYPES:
                raise ConfigError(f"Reflex '{item.get('name')}': unknown type '{kind}', "
                                  f"expected one of {', '.join(REFLEX_TYPES)}")
            reflexes.append(REFLEX_TYPES[kind].from_dict(item))
        names = [r.name for r in reflexes]
        if len(names) != len(set(names)):
            raise ConfigError("Reflex names must be unique")
        targets = [(r.target.appliance, r.target.key) for r in reflexes if r.enabled]
        if len(targets) != len(set(targets)):
            raise ConfigError("Two enabled reflexes override the same register")
        config = cls(
            url=str(hv.get("url", cls.url)).rstrip("/"),
            token_file=hv.get("token_file"),
            timezone=str(data.get("timezone", cls.timezone)),
            interval_s=float(data.get("interval_s", cls.interval_s)),
            status_interval_s=float(data.get("status_interval_s", cls.status_interval_s)),
            dry_run=bool(data.get("dry_run", True)),
            owner_prefix=str(data.get("owner_prefix", cls.owner_prefix)),
            state_file=str(data.get("state_file", cls.state_file)),
            reflexes=reflexes,
        )
        try:
            config.tz
        except Exception as err:
            raise ConfigError(f"Unknown time zone '{config.timezone}'") from err
        if config.interval_s < 10:
            raise ConfigError("interval_s must be at least 10")
        if config.status_interval_s < 0:
            raise ConfigError("status_interval_s must not be negative")
        return config


def load_config(path: str | Path) -> Config:
    path = Path(path).expanduser()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise ConfigError(f"Config file not found: {path}") from err
    except json.JSONDecodeError as err:
        raise ConfigError(f"Invalid JSON in {path}: {err}") from err
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    return Config.from_dict(data)
