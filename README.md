# housereflex

`housereflex` is an open-source (MIT) service that reacts automatically to the state
of a house's energy system. Its first reflex turns PV surplus into hot water: when the
battery is full and power is flowing into the grid, it raises a heat pump's hot-water
setpoint for a while, so the surplus is stored as heat instead of being exported.

It is the acting counterpart to [housevitals](https://github.com/isachse/housevitals-mcp),
which measures. housereflex never talks to a device: it reads live values from
housevitals and asks housevitals for **overrides**, which housevitals checks, writes
through its single Modbus connection per device, and undoes when they end.

![Architecture](docs/architecture.svg)

## Why a separate service

* **One Modbus client.** Heat pump gateways often accept only one TCP connection.
  housevitals already holds it, with a lock per device; a second client would compete
  with it. housereflex only uses HTTP.
* **Deciding vs. doing.** housevitals is a generic data layer with a guarded write path
  (allow-list, bounds, time limit, write budget, automatic restore). The rules for when
  to act are specific to a house and belong here.
* **Fails safe.** Every override has an end time held by housevitals. If housereflex
  crashes or is stopped, housevitals still ends the override and restores the previous
  value. If housevitals is down, housereflex pauses.

## Reflexes

### `pv_surplus_boost`

Raises a setpoint while there is sustained PV surplus.

| Phase | Enters when | Does |
|-------|-------------|------|
| `idle` | outside the time window, no surplus, the tank is already hot, or today's limit is reached | nothing |
| `armed` | in the window: battery ≥ `min_battery_soc` and export ≥ `min_export_w` | waits until the surplus has held for `arm.hold_s` without interruption |
| `boosting` | surplus held long enough | `PUT` override (target value until the end of the window) |
| `done` | `done.key` ≥ `done.min` (e.g. tank hot), window over, or the override ended in housevitals | `DELETE` override (restores the previous value); nothing more today |

While boosting, an import above `abort.max_import_w` for `abort.hold_s` (clouds) ends
the override as well; the reflex may arm again if `max_per_day` allows. Values that are
stale or unavailable never count as surplus. A running override is recognized after a
restart (by its owner `housereflex/<name>`), and the number of boosts per day is kept in
the state file.

Example for a BLW NEO heat pump: the controller keeps the hot water at the normal
setpoint during its time program and at the minimum setpoint (e.g. 42 °C) otherwise.
Overriding `dhw_setpoint_min` with 50 °C makes it heat right away, whatever the time
program says; afterwards the minimum is back at 42 °C. The heat pump's own time program
stays the fallback.

## Installation

Requires Python ≥ 3.11 and a running housevitals with the control API enabled.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Configuration

### housevitals

Allow-list the register in housevitals' `devices.json` and give it a control token
(see its README, section "Control API"):

```json
{ "name": "heatpump2", "profile": "neo", "host": "…",
  "overrides": { "dhw_setpoint_min": { "min": 40, "max": 55, "max_duration_s": 21600 } } }
```

`max_duration_s` must cover the reflex's time window (10–16 h = 6 h).

### housereflex

Copy [reflexes.example.json](reflexes.example.json) to `reflexes.json` (ignored by git)
and adjust names and thresholds:

| Option | Default | Description |
|--------|---------|-------------|
| `housevitals.url` | `http://127.0.0.1:8080` | housevitals service |
| `housevitals.token_file` | – | File with the control token (or env `HOUSEREFLEX_TOKEN`); on the same host this is housevitals' token file |
| `timezone` | `Europe/Berlin` | Time windows and days |
| `interval_s` | `60` | Seconds between rounds (≥ 10) |
| `dry_run` | `true` | Only log what would be done; `--live` or `false` to act |
| `state_file` | `~/.local/state/housereflex/state.json` | Boosts per day |
| `owner_prefix` | `housereflex` | Owner shown in housevitals: `<prefix>/<reflex name>` |

Per reflex (`type: pv_surplus_boost`):

| Option | Default | Description |
|--------|---------|-------------|
| `name` | – | Unique name (letters, digits, `_`) |
| `enabled` | `true` | |
| `target` | – | `appliance`, `key`, `value` of the override (names or aliases as in housevitals) |
| `window` | `10:00`–`16:00` | Local time window; the override ends at `window.end` at the latest |
| `source` | – | `appliance` with the battery and meter (the inverter); keys `battery_soc`, `grid_power` (positive = import) |
| `arm` | 90 %, 2500 W, 600 s | `min_battery_soc`, `min_export_w`, `hold_s` |
| `done` | – | `appliance`, `key`, `min`: end early when reached (e.g. `dhw_temperature` ≥ 49) |
| `abort` | 1000 W, 300 s | `max_import_w`, `hold_s` |
| `max_per_day` | `1` | Boosts per day |

Two enabled reflexes may not override the same register.

## Running

Try it once; this prints what each reflex sees and would do:

```bash
.venv/bin/housereflex --config reflexes.json --once --dry-run
```

Run in dry-run mode for a few days and read the log; then set `"dry_run": false` (or pass
`--live`).

As a LaunchAgent ([deploy/local.housereflex.plist](deploy/local.housereflex.plist)),
filling in the paths from the project directory:

```bash
sed -e "s#__PROJECT_DIR__#$PWD#g" -e "s#__HOME__#$HOME#g" deploy/local.housereflex.plist > ~/Library/LaunchAgents/local.housereflex.plist
```

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.housereflex.plist
```

Restart after config changes:

```bash
launchctl kickstart -k gui/$(id -u)/local.housereflex
```

Logs go to `~/Library/Logs/housereflex.log`: one line per phase change and per action.
Overrides show up in housevitals: `GET /api/v1/overrides`, its log, and the metric
`housevitals_override_active{owner="housereflex/…"}` for Grafana.

## Privacy

The repository contains no addresses, tokens or house data. `reflexes.json`, token
files and the state file stay local (`.gitignore`, state under `~/.local/state`).
Tokens are read from a file or an environment variable, never from the config file.

## Code structure

| Module | Responsibility |
|--------|----------------|
| `config.py` | Configuration, validation, token |
| `client.py` | housevitals REST client (values, overrides) |
| `reflexes.py` | Reflex state machines, pure and without I/O |
| `runner.py` | Loop: observe, step, act; state file |
| `cli.py` | Command line `housereflex` |

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

The tests use a fake housevitals API; no service or hardware is needed.
