"""The state machine of pv_surplus_boost, driven with plain observations."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from housereflex.config import Config, ConfigError, PvSurplusBoost
from housereflex.reflexes import ARMED, BOOSTING, DONE, IDLE, Observation, State, explain, step

TZ = ZoneInfo("Europe/Berlin")
REFLEX = PvSurplusBoost.from_dict({
    "name": "dhw_pv_boost", "type": "pv_surplus_boost",
    "target": {"appliance": "heatpump", "key": "dhw_setpoint_min", "value": 55},
    "boost": {"max_duration_s": 10800, "min_duration_s": 1800, "latest_end": "18:00"},
    "source": {"appliance": "inverter"},
    "arm": {"min_battery_soc": 90, "min_export_w": 2500, "hold_s": 600},
    "done": {"appliance": "heatpump", "key": "dhw_temperature", "min": 54},
    "abort": {"max_deficit_w": 500, "hold_s": 300},
})


def at(hh, mm, day=1):
    return datetime(2026, 6, day, hh, mm, tzinfo=TZ)


def obs(now, soc=95, grid=-3000, dhw=44, active=False, battery=0):
    return Observation(now, soc, grid, dhw, active, battery)


def run(state, *observations):
    return [step(REFLEX, state, o) for o in observations]


def test_arms_after_sustained_surplus_and_boosts_for_max_duration():
    state = State()
    actions = run(state, obs(at(11, 0)), obs(at(11, 5)))
    assert actions == [None, None] and state.phase == ARMED
    action = step(REFLEX, state, obs(at(11, 10)))
    assert action.kind == "apply" and action.until == at(14, 10)  # 3 h
    assert "export 3000 W" in action.reason
    assert state.phase == BOOSTING and state.triggers == 1


def test_no_start_window_surplus_alone_triggers():
    state = State()
    run(state, obs(at(7, 30)))
    assert step(REFLEX, state, obs(at(7, 40))).kind == "apply"


def test_latest_end_caps_a_late_boost():
    state = State()
    run(state, obs(at(16, 0)))
    action = step(REFLEX, state, obs(at(16, 10)))
    assert action.until == at(18, 0)  # not 19:10


def test_no_boost_shortly_before_latest_end():
    state = State()
    assert run(state, *(obs(at(17, m)) for m in range(35, 60, 5))) == [None] * 5
    assert state.phase == IDLE
    assert explain(REFLEX, state, obs(at(17, 40))).startswith("idle: 30 min left before 18:00 (no)")


def test_surplus_must_hold_without_interruption():
    state = State()
    run(state, obs(at(11, 0)), obs(at(11, 5), grid=-1000))  # a cloud
    assert state.phase == IDLE
    assert run(state, obs(at(11, 6)), obs(at(11, 12))) == [None, None]
    assert step(REFLEX, state, obs(at(11, 16))).kind == "apply"


@pytest.mark.parametrize("kwargs", [
    {"soc": 80},  # battery not full enough
    {"grid": 500},  # importing
    {"dhw": 54.5},  # tank already hot
    {"soc": None},  # value unavailable
    {"grid": None},
])
def test_no_boost_without_all_conditions(kwargs):
    state = State()
    assert run(state, *(obs(at(11, m), **kwargs) for m in range(0, 30, 5))) == [None] * 6
    assert state.phase == IDLE


def _boosting(state):
    run(state, obs(at(11, 0)), obs(at(11, 10)))
    assert state.phase == BOOSTING


def test_releases_when_the_tank_is_hot():
    state = State()
    _boosting(state)
    assert step(REFLEX, state, obs(at(11, 30), grid=0, active=True)) is None
    action = step(REFLEX, state, obs(at(12, 0), grid=0, dhw=54.2, active=True))
    assert action.kind == "release" and "dhw_temperature" in action.reason
    assert state.phase == DONE
    # Done for today, however much surplus there is.
    assert run(state, *(obs(at(13, m)) for m in range(0, 60, 10))) == [None] * 6


def test_releases_at_the_time_limit():
    state = State()
    _boosting(state)  # until 14:10
    assert step(REFLEX, state, obs(at(14, 5), grid=0, active=True)) is None
    action = step(REFLEX, state, obs(at(14, 10), grid=0, active=True))
    assert action.kind == "release" and "time limit" in action.reason and state.phase == DONE


def test_aborts_when_the_battery_covers_the_heat_pump():
    state = State()
    _boosting(state)
    # Clouds: no grid import, but the battery discharges 2.4 kW for the heat pump.
    assert step(REFLEX, state, obs(at(11, 20), grid=10, battery=2400, active=True)) is None
    assert step(REFLEX, state, obs(at(11, 22), grid=0, battery=-800, active=True)) is None  # resets
    assert step(REFLEX, state, obs(at(11, 23), grid=10, battery=2400, active=True)) is None
    action = step(REFLEX, state, obs(at(11, 28), grid=10, battery=2400, active=True))
    assert action.kind == "release" and "grid and battery" in action.reason
    assert state.phase == DONE  # max_per_day = 1


def test_grid_import_alone_also_aborts():
    state = State()
    _boosting(state)
    step(REFLEX, state, obs(at(11, 20), grid=1500, battery=None, active=True))
    assert step(REFLEX, state, obs(at(11, 25), grid=1500, battery=None, active=True)).kind == "release"


def test_override_ending_in_housevitals_ends_the_boost():
    state = State()
    _boosting(state)
    assert step(REFLEX, state, obs(at(14, 10), active=False)) is None
    assert state.phase == DONE


def test_running_override_is_adopted_after_a_restart():
    state = State()
    assert step(REFLEX, state, obs(at(12, 0), grid=0, active=True)) is None
    assert state.phase == BOOSTING and state.triggers == 1
    # End time unknown after a restart: latest_end is the fallback.
    assert step(REFLEX, state, obs(at(17, 59), grid=0, active=True)) is None
    assert step(REFLEX, state, obs(at(18, 0), grid=0, active=True)).kind == "release"


def test_new_day_starts_over():
    state = State()
    _boosting(state)
    step(REFLEX, state, obs(at(12, 0), dhw=55, active=True))
    assert state.phase == DONE
    run(state, obs(at(11, 0, day=2)))
    assert state.phase == ARMED and state.triggers == 0
    assert step(REFLEX, state, obs(at(11, 10, day=2))).kind == "apply"


def test_explain_names_the_blocking_condition():
    state = State()
    o = obs(at(14, 0), soc=96, grid=-7700, dhw=54.3)
    step(REFLEX, state, o)
    line = explain(REFLEX, state, o)
    assert line.startswith("idle: 30 min left before 18:00 (yes)")
    assert "battery 96 % >= 90 % (yes)" in line and "export 7700 W >= 2500 W (yes)" in line
    assert "dhw_temperature 54.3 < 54 (no)" in line and "boosts today 0/1" in line
    assert "battery unknown (no)" in explain(REFLEX, state, obs(at(14, 0), soc=None))
    boosting = State()
    _boosting(boosting)
    line = explain(REFLEX, boosting, obs(at(11, 30), grid=100, battery=900, active=True))
    assert line.startswith("boosting: until 14:10, deficit 1000 W <= 500 W (no)")


def test_config_validation():
    base = {"name": "x", "type": "pv_surplus_boost", "source": {"appliance": "inverter"},
            "target": {"appliance": "hp", "key": "k", "value": 1}}
    reflex = Config.from_dict({"reflexes": [base]}).reflexes[0]
    assert reflex.max_duration_s == 10800 and reflex.latest_end.hour == 18
    assert reflex.battery_key == "battery_power"
    with pytest.raises(ConfigError, match="unknown option"):
        Config.from_dict({"reflexes": [{**base, "surprise": 1}]})
    with pytest.raises(ConfigError, match="replaced by 'boost'"):
        Config.from_dict({"reflexes": [{**base, "window": {"start": "10:00", "end": "16:00"}}]})
    with pytest.raises(ConfigError, match="max_deficit_w"):
        Config.from_dict({"reflexes": [{**base, "abort": {"max_import_w": 1000}}]})
    with pytest.raises(ConfigError, match="min_duration_s"):
        Config.from_dict({"reflexes": [{**base, "boost": {"max_duration_s": 600}}]})
    with pytest.raises(ConfigError, match="unknown type"):
        Config.from_dict({"reflexes": [{**base, "type": "magic"}]})
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
