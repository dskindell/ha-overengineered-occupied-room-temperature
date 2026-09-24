# Overengineered Occupied-Room Temperature (OORT)

A [Home Assistant](https://www.home-assistant.io/) custom integration that keeps the rooms you're actually in at your chosen temperature, on a whole-home (single-zone) HVAC system.

OORT watches who and what is in each of your rooms and blends their temperature sensors into one occupancy-weighted number, which you point your thermostat's "current temperature" at instead of a single fixed sensor. It's the same idea as ecobee's "Follow Me" feature, but works with any thermostat that can take an external temperature sensor as its input — not just ecobee hardware.

This is a helper integration: it doesn't talk to any hardware itself. It reads sensors and template results you already have, and produces new sensors for your thermostat to use.

> **Status:** this integration is functional and tested, but has not yet had a production install verified by its author. Expect rough edges, and please open an issue if you find one.

## Table of contents

- [How it works](#how-it-works)
- [Is this for me?](#is-this-for-me)
- [Installation](#installation)
- [Configuration](#configuration)
  - [Create an instance](#create-an-instance)
  - [Instance defaults](#instance-defaults)
  - [Add a room](#add-a-room)
  - [Add a person](#add-a-person)
  - [Reconfiguring](#reconfiguring)
- [Entities](#entities)
- [Fallback and stale sensors](#fallback-and-stale-sensors)
- [Startup grace period](#startup-grace-period)
- [Pointing your thermostat at OORT](#pointing-your-thermostat-at-oort)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

## How it works

For each room you configure, OORT tracks a **weight** between 0 and roughly 1, driven by what's happening in that room right now:

| Status | Meaning | Target weight |
|---|---|---|
| `person` | A configured person is located in this room | *Person weight* (default `1.0`) |
| `occupied` | An occupancy sensor or template says the room is occupied, but no tracked person is in it | *Occupied weight* (default `0.5`) |
| `unoccupied` | Neither of the above | *Unoccupied weight* (default `0.001`) |
| `inactive` | The room's active template says to leave it out, or its temperature sensor is unusable | `0` |

The weight doesn't jump straight to its target — it moves there exponentially, over a configurable time constant (a "tau", in minutes) that's different for rising into a status and falling out of it. This smooths out someone briefly walking through a room, or a motion sensor's usual on/off flicker.

Every minute (and on every relevant state change), OORT recomputes each room's weight and combines all the rooms into one number:

```
temperature = Σ(room_weight × room_temperature) + ε × average(all_valid_room_temperatures)
              ─────────────────────────────────────────────────────────────────────────
              Σ(room_weight) + ε
```

The small extra term (weight `ε`, fixed at 1% of your smallest room's *unoccupied weight*) is a plain average of every room with a valid reading, whether or not that room is currently counted as active. It's negligible while any room has a meaningful weight, but it keeps the output sane — rather than undefined — if every room's weight has decayed to (near) zero. See [Fallback and stale sensors](#fallback-and-stale-sensors).

## Is this for me?

- You have **one whole-home HVAC system** (a single zone) and want it to prioritize the rooms people are actually using, rather than a single fixed thermostat location.
- Your thermostat entity (or the climate integration that drives it) can be configured to use an **external sensor as its current temperature** — for example Home Assistant's [Generic Thermostat](https://www.home-assistant.io/integrations/generic_thermostat/), a `dual_smart_thermostat`, or any similar `climate` integration with a "target sensor" / "current temperature sensor" option. OORT doesn't control climate entities directly; it only produces a temperature sensor for you to wire in yourself.
- You have (or are willing to set up) a temperature sensor per room, and some way to detect occupancy per room — motion/occupancy sensors, input booleans, templates, or per-person room-location sensors (anything whose state or an attribute is the name or ID of the Home Assistant area the person is in, such as a room-presence integration or a template sensor). A `person` or `device_tracker` entity on its own isn't enough: its state is a zone, not an area.
- You run **Home Assistant 2026.9 or later**. Tests run against 2026.9.3.

## Installation

There's no HACS listing yet (planned for later). Install manually:

1. Copy the `custom_components/overengineered_occupied_room_temperature` folder from this repository into your Home Assistant config's `custom_components/` directory, so you end up with `<config>/custom_components/overengineered_occupied_room_temperature/`.
2. Restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration**, and search for "Overengineered Occupied-Room Temperature".

## Configuration

Everything is configured through the UI (there is no YAML configuration). OORT has three layers:

- **Instance** — one HVAC zone. Its name feeds your entity IDs (e.g. an instance named "Home" produces `sensor.oort_home_temperature`). An instance also carries default taus, weights and the stale-temperature limit.
- **Room** (a subentry of the instance) — one area, its temperature sensor, its occupancy sources, and optional per-room overrides of the instance defaults.
- **Person** (a subentry of the instance) — one location sensor to track, and how to read an area out of it.

### Create an instance

**Settings → Devices & services → Add integration → Overengineered Occupied-Room Temperature** opens the "New OORT instance" form:

| Field | Description | Default |
|---|---|---|
| Name | Used in entity IDs, e.g. "Home" gives `sensor.oort_home_temperature`. | — (required) |

Under the collapsed **Defaults** section (times are in minutes; rooms can override any of these):

| Field | Description | Default |
|---|---|---|
| Person rise tau | How fast a room's weight rises when a tracked person arrives. | 3 |
| Person fall tau | How fast a room's weight falls after a tracked person leaves, if it was in `person` status. | 3 |
| Occupancy rise tau | How fast a room's weight rises when it becomes occupied (without a tracked person). | 10 |
| Occupancy fall tau | How fast a room's weight falls after occupancy ends, if it was in `occupied` status. | 8 |
| Deactivate tau | How fast an inactive room fades out. `0` = instantly. | 1 |
| Sensor dropout tau | How fast a room fades out when its temperature sensor becomes unavailable. `0` = instantly. | 5 |
| Person weight | Target weight while a tracked person is in the room. | 1.0 |
| Occupied weight | Target weight while the room is occupied but no tracked person is in it. | 0.5 |
| Unoccupied weight | Target weight otherwise. Must be greater than 0. | 0.001 |
| Stale temperature limit | How long (minutes) a room's last reading keeps being used after its temperature sensor drops out, before the room is excluded entirely. | 60 |

Validation: person and occupancy taus (rise/fall) must be greater than 0; deactivate and dropout taus can't be negative; weights can't be negative; the unoccupied weight and the stale limit must be greater than 0.

### Add a room

From the instance's page, **Add room**:

| Field | Description |
|---|---|
| Area | The Home Assistant area this room represents. Fixed once the room is created — you can't change a room's area later, only remove and re-add it. Each area can only be used once per instance (but the same area can be a room in a different instance). |
| Temperature sensor | A `sensor` entity with device class `temperature` that reports this room's current temperature. |
| Occupancy sensors | Any number of `binary_sensor` or `input_boolean` entities. The room counts as occupied while any of them is `on`. |
| Occupancy template | A template; when it evaluates true, the room also counts as occupied (in addition to the occupancy sensors). |
| Active template | A template; when it evaluates false, the room is left out and its weight fades to 0 (the `inactive` status), regardless of occupancy. Leave blank to always treat the room as active. |

Under the collapsed **Overrides** section, any of the same taus/weights as the instance defaults (leave a field blank to use the instance's default): person rise/fall tau, occupancy rise/fall tau, deactivate tau, sensor dropout tau, person weight, occupied weight, unoccupied weight. (The stale-temperature limit is instance-wide only and can't be overridden per room.)

If you save a room with no occupancy sensors, no occupancy template, and the instance has no people configured yet, you'll see a note that the room can never count as occupied — you can save it anyway (for example, to add sensors or people later) or go back and add a source.

### Add a person

From the instance's page, **Add person**:

| Field | Description |
|---|---|
| Name | Shown in each room's `people` attribute when this person is present. |
| Location sensor | A sensor whose state (or an attribute, next step) is the area this person is in. |
| The location is an | Whether that value is an **area name** or an **area ID**. |

The next step lets you optionally pick an **attribute** of the location sensor to read instead of its state (leave it blank to use the sensor's state directly).

A person only affects a room if the area they resolve to matches one of this instance's configured rooms — an area with no matching room, or a value that doesn't resolve to any Home Assistant area, is treated as "not present anywhere in this instance."

### Reconfiguring

Instances, rooms and people can all be reconfigured from the integration's page in **Settings → Devices & services**. Reconfiguring a room keeps its original area (the area field isn't shown). Any change to an instance, a room, or a person reloads the whole instance.

## Entities

Each instance creates one device ("OORT `<instance name>`") containing:

- One **`<Room> weight`** sensor per room: `sensor.oort_<instance>_<room>_weight`
- One **Temperature** sensor for the instance: `sensor.oort_<instance>_temperature`

(For example, an instance named "Home" with a "Kitchen" room gives `sensor.oort_home_kitchen_weight` and `sensor.oort_home_temperature`.)

### `<Room> weight` sensor

Its state is the room's current weight (a number between 0 and the room's person weight). Attributes:

| Attribute | Meaning |
|---|---|
| `status` | `inactive`, `person`, `occupied`, or `unoccupied` — see [How it works](#how-it-works). |
| `active` | The active template's current result (`true` if the room has no active template). |
| `temperature_available` | Whether the room's temperature sensor currently has a usable reading. |
| `temperature_stale` | `true` once the temperature sensor has been unavailable longer than the stale limit — see [Fallback and stale sensors](#fallback-and-stale-sensors). |
| `person_present` | Whether any tracked person currently resolves to this room. |
| `occupied` | The combined result of the room's occupancy sensors and occupancy template. |
| `people` | List of the names of people currently present in this room. |
| `target_weight` | The weight this room is currently moving toward. |
| `last_occupied_state` | The last status that was `person` or `occupied` (used to pick the correct fall tau); `null` if the room has never been occupied. |

Room weight is restored across a Home Assistant restart (the downtime itself isn't counted as elapsed time — the room resumes at the weight it had before shutdown rather than jumping as if time had passed).

### `Temperature` sensor

Its state is the instance's occupancy-weighted temperature, in your Home Assistant instance's configured temperature unit (sensor readings in other units are converted automatically). It becomes `unavailable` when nothing can be averaged — see below. Attributes:

| Attribute | Meaning |
|---|---|
| `total_weight` | Sum of the weights of rooms that contributed a valid temperature (excludes the fallback term's weight). |
| `contributing_rooms` | How many rooms contributed a valid temperature to the weighted average. |
| `fallback` | `true` when the plain-average fallback term outweighs every room's own weighted contribution — see below. |

## Fallback and stale sensors

Every room's temperature sensor is read continuously, independent of whether the room counts as active. If a room's sensor becomes unavailable, unknown, or reports a value or unit Home Assistant can't convert to a temperature, the room keeps using its **last known reading** while its weight fades out at the dropout tau. If the sensor stays unusable for longer than the instance's **stale temperature limit**, that room's `temperature_stale` attribute becomes `true` and it's dropped from the average entirely (it still keeps its weight and status, it just no longer contributes a temperature).

The overall `Temperature` sensor also has a small **fallback term**: a plain average of every room's current valid reading (regardless of whether that room is active), with a fixed weight equal to 1% of your smallest room's unoccupied weight. This term is normally negligible next to any room with real weight, but it keeps the sensor producing a sensible number — rather than becoming unavailable — while every room is fading toward zero (for example, right after startup, or if every room is deactivated at once). The `fallback` attribute turns `true` when this term's weight exceeds the sum of every room's own weight, which is your cue that the temperature is currently closer to a whole-home average than to an occupancy-weighted one.

The `Temperature` sensor becomes `unavailable` only when there's nothing at all to average — every room is either stale or has never reported a valid temperature.

## Startup grace period

When Home Assistant itself is starting, OORT holds every room's weight at whatever it was restored to (or 0, if there's nothing to restore) until **2 minutes** after Home Assistant has finished starting. Reloading or reconfiguring the integration while Home Assistant is already running doesn't start a grace period. Inputs, status and dropout tracking are still updated live during this hold — only the weight itself is frozen — so the very first minutes after a restart don't produce a temperature swung by rooms racing back up from 0.

## Pointing your thermostat at OORT

Wire the `Temperature` sensor into whatever climate integration is driving your HVAC. For example, with Home Assistant's built-in [Generic Thermostat](https://www.home-assistant.io/integrations/generic_thermostat/) (`configuration.yaml`):

```yaml
climate:
  - platform: generic_thermostat
    name: House
    heater: switch.hvac_heater
    target_sensor: sensor.oort_home_temperature
```

Any other climate integration with an equivalent "current/target temperature sensor" option — including UI-configured ones — works the same way: point that option at your instance's `sensor.oort_<instance>_temperature`.

## Known limitations

- Each instance creates a single device holding all of its room-weight and temperature sensors, rather than one device per room. Room sensors won't appear on their own room's area page in Home Assistant unless you manually assign each `<Room> weight` entity to that area (**Settings → Devices & services → Entities**).
- No HACS listing yet; manual installation only.
- A room's area can't be changed after the room is created — remove and re-add it instead.

## Troubleshooting

**Repairs shows "Some OORT rooms can never count as occupied".** This fires when a room has no occupancy sensors, no occupancy template, and the instance has no people configured — so that room can only ever sit at its unoccupied weight. Add occupancy sensors or a template to the room, or add a person to the instance who can be located in it. The issue clears automatically once either is true, or if the room is removed.

**The `Temperature` sensor is `unavailable`.** Every configured room's temperature sensor is either stale (unavailable for longer than the stale limit) or has never reported a usable value. Check each room's `temperature_available` and `temperature_stale` attributes to find the affected sensor(s).

## Development

Tests run against Home Assistant 2026.9.3 (via `pytest-homeassistant-custom-component`), which needs Python 3.14:

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/python -m pytest
```

This runs 61 tests: the pure-Python weighting/smoothing engine, the config flow, and end-to-end runtime tests against an in-memory Home Assistant.

Lint with [ruff](https://docs.astral.sh/ruff/):

```sh
uvx ruff check --select F,E9,B custom_components tests
```

## License

MIT — see [LICENSE](LICENSE).
