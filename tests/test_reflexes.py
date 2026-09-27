"""The state machine of pv_surplus_boost, driven with plain observations."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from housereflex.config import Config, ConfigError, PvSurplusBoost
from housereflex.reflexes import ARMED, BOOSTING, DONE, IDLE, Observation, State, step

TZ = ZoneInfo("Europe/Berlin")
REFLEX = PvSurplusBoost.from_dict({
    "name": "dhw_pv_boost", "type": "pv_surplus_boost",
    "target": {"appliance": "heatpump", "key": "dhw_setpoint_min", "value": 50},
    "window": {"start": "10:00", "end": "16:00"},
    "source": {"appliance": "inverter"},
    "arm": {"min_battery_soc": 90, "min_export_w": 2500, "hold_s": 600},
    "done": {"appliance": "heatpump", "key": "dhw_temperature", "min": 49},
    "abort": {"max_import_w": 1000, "hold_s": 300},
})


def at(hh, mm, day=1):
    return datetime(2026, 6, day, hh, mm, tzinfo=TZ)


def obs(now, soc=95, grid=-3000, dhw=44, active=False):
    return Observation(now, soc, grid, dhw, active)


def run(state, *observations):
    return [step(REFLEX, state, o) for o in observations]


def test_arms_after_sustained_surplus_and_boosts_until_window_end():
    state = State()
    actions = run(state, obs(at(11, 0)), obs(at(11, 5)))
    assert actions == [None, None] and state.phase == ARMED
    action = step(REFLEX, state, obs(at(11, 10)))
    assert action.kind == "apply" and action.until == at(16, 0)
    assert "export 3000 W" in action.reason
    assert state.phase == BOOSTING and state.triggers == 1


def test_surplus_must_hold_without_interruption():
    state = State()
    run(state, obs(at(11, 0)), obs(at(11, 5), grid=-1000))  # a cloud
    assert state.phase == IDLE
    assert run(state, obs(at(11, 6)), obs(at(11, 12))) == [None, None]
    assert step(REFLEX, state, obs(at(11, 16))).kind == "apply"


@pytest.mark.parametrize("kwargs", [
    {"soc": 80},  # battery not full enough
    {"grid": 500},  # importing
    {"dhw": 49.5},  # tank already hot
    {"soc": None},  # value unavailable
    {"grid": None},
])
def test_no_boost_without_all_conditions(kwargs):
    state = State()
    assert run(state, *(obs(at(11, m), **kwargs) for m in range(0, 30, 5))) == [None] * 6
    assert state.phase == IDLE


def test_only_inside_the_window():
    state = State()
    assert run(state, obs(at(9, 45)), obs(at(9, 59))) == [None, None]
    assert state.phase == IDLE
    run(state, obs(at(15, 55)))
    assert step(REFLEX, state, obs(at(16, 5))) is None and state.phase == IDLE


def _boosting(state):
    run(state, obs(at(11, 0)), obs(at(11, 10)))
    assert state.phase == BOOSTING


def test_releases_when_the_tank_is_hot():
    state = State()
    _boosting(state)
    assert step(REFLEX, state, obs(at(11, 30), grid=0, active=True)) is None
    action = step(REFLEX, state, obs(at(12, 0), grid=0, dhw=49.2, active=True))
    assert action.kind == "release" and "dhw_temperature" in action.reason
    assert state.phase == DONE
    # Done for today, however much surplus there is.
    assert run(state, *(obs(at(13, m)) for m in range(0, 60, 10))) == [None] * 6


def test_aborts_on_sustained_import():
    state = State()
    _boosting(state)
    assert step(REFLEX, state, obs(at(11, 20), grid=1500, active=True)) is None
    assert step(REFLEX, state, obs(at(11, 22), grid=200, active=True)) is None  # resets
    assert step(REFLEX, state, obs(at(11, 23), grid=1500, active=True)) is None
    action = step(REFLEX, state, obs(at(11, 28), grid=1500, active=True))
    assert action.kind == "release" and "importing" in action.reason
    assert state.phase == DONE  # max_per_day = 1


def test_override_ending_in_housevitals_ends_the_boost():
    state = State()
    _boosting(state)
    assert step(REFLEX, state, obs(at(16, 0), active=False)) is None
    assert state.phase == DONE


def test_running_override_is_adopted_after_a_restart():
    state = State()
    assert step(REFLEX, state, obs(at(12, 0), grid=0, active=True)) is None
    assert state.phase == BOOSTING and state.triggers == 1


def test_new_day_starts_over():
    state = State()
    _boosting(state)
    step(REFLEX, state, obs(at(12, 0), dhw=50, active=True))
    assert state.phase == DONE
    run(state, obs(at(11, 0, day=2)))
    assert state.phase == ARMED and state.triggers == 0
    assert step(REFLEX, state, obs(at(11, 10, day=2))).kind == "apply"


def test_config_validation():
    base = {"name": "x", "type": "pv_surplus_boost", "source": {"appliance": "inverter"},
            "target": {"appliance": "hp", "key": "k", "value": 1}}
    assert Config.from_dict({"reflexes": [base]}).reflexes[0].window_end.hour == 16
    with pytest.raises(ConfigError, match="unknown option"):
        Config.from_dict({"reflexes": [{**base, "surprise": 1}]})
    with pytest.raises(ConfigError, match="unknown type"):
        Config.from_dict({"reflexes": [{**base, "type": "magic"}]})
    with pytest.raises(ConfigError, match="window.start"):
        Config.from_dict({"reflexes": [{**base, "window": {"start": "16:00", "end": "10:00"}}]})
    with pytest.raises(ConfigError, match="same register"):
        Config.from_dict({"reflexes": [base, {**base, "name": "y"}]})
    with pytest.raises(ConfigError, match="source.appliance"):
        Config.from_dict({"reflexes": [{**base, "source": {}}]})
    assert Config.from_dict({}).dry_run is True  # safe default


def test_token(monkeypatch, tmp_path):
    monkeypatch.delenv("HOUSEREFLEX_TOKEN", raising=False)
    config = Config()
    assert config.token() is None
    (tmp_path / "t").write_text("abcdefghijklmnopqrstuvwxyz\n")
    config.token_file = str(tmp_path / "t")
    assert config.token() == "abcdefghijklmnopqrstuvwxyz"
    monkeypatch.setenv("HOUSEREFLEX_TOKEN", "short")
    with pytest.raises(ConfigError, match="at least 16"):
        config.token()
