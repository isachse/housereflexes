"""The runner against a fake housevitals API (httpx.MockTransport)."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from housereflex.client import HousevitalsClient
from housereflex.config import Config
from housereflex.runner import Runner

TZ = ZoneInfo("Europe/Berlin")
TOKEN = "test-token-0123456789"


class FakeHousevitals:
    """Just enough of the housevitals REST API: values and overrides."""

    def __init__(self):
        self.values = {"inverter": {"battery_soc": 95, "grid_power": -3000},
                       "heatpump": {"dhw_temperature": 44}}
        self.overrides: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.refuse_put: int | None = None
        self.down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused")
        path, method = request.url.path, request.method
        self.calls.append((method, path))
        if method != "GET" and request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"detail": "Missing or wrong bearer token"})
        if path == "/api/v1/overrides":
            return httpx.Response(200, json={"overrides": self.overrides, "allowed": {}})
        parts = path.split("/")  # ['', 'api', 'v1', 'appliances', name, ...]
        name = parts[4]
        if parts[5] == "values":
            keys = request.url.params.get_list("keys")
            return httpx.Response(200, json={"appliance": name, "values": {
                k: {"label": k, "value": self.values[name][k], "age_s": 3} for k in keys}})
        key = parts[6]
        if method == "PUT":
            if self.refuse_put:
                return httpx.Response(self.refuse_put, json={"detail": "refused"})
            body = json.loads(request.content)
            self.overrides = [{"appliance": name, "key": key, **body}]
            return httpx.Response(200, json=self.overrides[0])
        if method == "DELETE":
            if not self.overrides:
                return httpx.Response(404, json={"detail": "No active override"})
            self.overrides = []
            return httpx.Response(200, json={"outcome": "restored"})
        return httpx.Response(405)


CONFIG = {
    "housevitals": {"url": "http://housevitals.test"},
    "dry_run": False,
    "interval_s": 60,
    "reflexes": [{
        "name": "dhw_pv_boost", "type": "pv_surplus_boost",
        "target": {"appliance": "heatpump", "key": "dhw_setpoint_min", "value": 50},
        "window": {"start": "10:00", "end": "16:00"},
        "source": {"appliance": "inverter"},
        "arm": {"hold_s": 600},
        "done": {"appliance": "heatpump", "key": "dhw_temperature", "min": 49},
    }],
}


class Clock:
    def __init__(self):
        self.now = datetime(2026, 6, 1, 11, 0, tzinfo=TZ)

    def __call__(self):
        return self.now

    def advance(self, minutes):
        self.now = self.now.replace(minute=self.now.minute + minutes)


def _runner(tmp_path, fake, dry_run=None, clock=None):
    config = Config.from_dict({**CONFIG, "state_file": str(tmp_path / "state.json")})
    client = HousevitalsClient(config.url, TOKEN, transport=httpx.MockTransport(fake.handler))
    return Runner(config, client, clock=clock or Clock(), dry_run=dry_run), client


async def test_boost_cycle(tmp_path):
    fake, clock = FakeHousevitals(), Clock()
    runner, client = _runner(tmp_path, fake, clock=clock)
    assert (await runner.tick())[0]["phase"] == "armed"
    clock.advance(10)
    report = (await runner.tick())[0]
    assert report["action"]["kind"] == "apply" and report["phase"] == "boosting"
    put = fake.overrides[0]
    assert put["owner"] == "housereflex/dhw_pv_boost" and put["value"] == 50
    assert put["until"] == "2026-06-01T16:00:00+02:00"

    fake.values["heatpump"]["dhw_temperature"] = 50
    clock.advance(30)
    report = (await runner.tick())[0]
    assert report["action"]["kind"] == "release" and report["phase"] == "done"
    assert fake.overrides == []
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["dhw_pv_boost"]["triggers"] == 1
    await client.close()

    # A restart the same day remembers the boost.
    runner2, client2 = _runner(tmp_path, fake, clock=clock)
    fake.values["heatpump"]["dhw_temperature"] = 44
    clock.advance(5)
    assert (await runner2.tick())[0]["phase"] == "done"
    await client2.close()


async def test_dry_run_writes_nothing(tmp_path):
    fake, clock = FakeHousevitals(), Clock()
    runner, client = _runner(tmp_path, fake, dry_run=True, clock=clock)
    await runner.tick()
    clock.advance(10)
    report = (await runner.tick())[0]
    assert report["action"]["kind"] == "apply" and report["phase"] == "boosting"
    assert all(method == "GET" for method, _ in fake.calls)
    assert not (tmp_path / "state.json").exists()
    await client.close()


async def test_refused_override_is_done_for_today(tmp_path):
    fake, clock = FakeHousevitals(), Clock()
    fake.refuse_put = 429
    runner, client = _runner(tmp_path, fake, clock=clock)
    await runner.tick()
    clock.advance(10)
    assert (await runner.tick())[0]["phase"] == "done"
    await client.close()


async def test_outage_pauses_and_recovers(tmp_path):
    fake, clock = FakeHousevitals(), Clock()
    runner, client = _runner(tmp_path, fake, clock=clock)
    fake.down = True
    assert await runner.tick() == []
    fake.down = False
    assert (await runner.tick())[0]["phase"] == "armed"
    await client.close()


async def test_running_override_is_recovered(tmp_path):
    fake, clock = FakeHousevitals(), Clock()
    fake.overrides = [{"appliance": "heatpump", "key": "dhw_setpoint_min",
                       "owner": "housereflex/dhw_pv_boost", "value": 50}]
    runner, client = _runner(tmp_path, fake, clock=clock)
    report = (await runner.tick())[0]
    assert report["phase"] == "boosting" and report["override_active"] is True
    await client.close()
