# Overengineered Occupied-Room Temperature (OORT)

A [Home Assistant](https://www.home-assistant.io/) custom integration that keeps the rooms you're actually in at your chosen temperature, on a whole-home (single-zone) HVAC system.

OORT watches who and what is in each of your rooms and blends their temperature sensors into one occupancy-weighted number, which you point your thermostat's "current temperature" at instead of a single fixed sensor. It's the same idea as ecobee's "Follow Me" feature, but works with any thermostat that can take an external temperature sensor as its input — not just ecobee hardware.

OORT doesn't talk to any hardware itself. It reads sensors and template results you already have, and produces new sensors for your thermostat to use.

> **Status:** this integration is functional and tested, but has not yet had a production install verified by its author. Expect rough edges, and please open an issue if you find one.

## Table of contents

- [How it works](#how-it-works)
- [Is this for me?](#is-this-for-me)
- [Installation](#installation)
- [Configuration](#configuration)
  - [Create a zone](#create-a-zone)
  - [Change a zone later](#change-a-zone-later)
  - [Defaults](#defaults)
  - [Rooms](#rooms)
  - [People](#people)
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
| `inactive` | The room is "open" (an opening entity is on, or its opening template is true), or its temperature sensor is unusable | `0` |

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

Everything is configured through the UI (there is no YAML configuration). OORT is organised in **zones**:

- **Zone** — one HVAC system and the rooms it serves. Its name feeds your entity IDs (a zone named "Home" produces `sensor.oort_home_temperature`). Most homes need one zone; add more for multi-zone HVAC. Each zone is completely independent.
- **Rooms** — each room is one Home Assistant area with its temperature sensor, its occupancy sources, and optional overrides of the zone's defaults.
- **People** (optional) — each person is an entity that says which area they're in.

Rooms and people belong to their zone and are managed only from that zone's menu.

### Create a zone

**Settings → Devices & services → Add integration → Overengineered Occupied-Room Temperature.**

1. **Name the zone** (for example "Home"). The name must not already be used by another OORT zone.
2. **The zone menu** opens, showing the rooms and people added so far:
   - **Defaults** — the zone's taus, weights and stale-temperature limit (see below). Optional: the defaults work for most homes.
   - **Rooms** — add, edit or remove rooms.
   - **People** — add, edit or remove people.
   - **Finish** — creates the zone. It appears once the zone has at least one room.

Nothing is saved until you choose **Finish**; closing the dialog earlier discards the new zone.

### Change a zone later

On the integration's page (**Settings → Devices & services → Overengineered Occupied-Room Temperature**), press **Configure** on the zone. The same menu opens, pre-filled, with **Save** instead of Finish (again only while the zone has at least one room). Saving reloads the zone. Closing without saving changes nothing.

To rename a zone, use Home Assistant's own **Rename** in the zone's ⋮ menu. The device name follows; existing entity IDs keep the old name (rename them in the entity settings if you want).

### Defaults

Times are in minutes. A **tau** is a time constant: roughly how long a room's weight takes to get two-thirds of the way to its new value — smaller reacts faster, larger is smoother. **Weights** say how much each room counts; only their ratios matter.

| Field | Description | Default |
|---|---|---|
| Person rise tau | How fast a room's weight rises when a tracked person arrives. | 3 |
| Person fall tau | How fast it falls after the last tracked person leaves — including while an occupancy sensor there is still on. Larger keeps a room counted during short trips out. If the room is briefly inactive during that fall (for example while its temperature sensor reconnects after a restart), it finishes the fall at the occupancy rise speed instead. | 3 |
| Occupancy rise tau | How fast it rises when an occupancy sensor or template says someone is there (and no tracked person is). | 10 |
| Occupancy fall tau | How fast it falls after occupancy ends. | 8 |
| Deactivate tau | How fast a room fades out when it becomes "open" (an opening entity turns on, or the opening template turns true). `0` = instantly. | 1 |
| Sensor dropout tau | How fast a room fades out when its temperature sensor becomes unavailable. `0` = instantly. | 5 |
| Person weight | Target weight while a tracked person is in the room. | 1.0 |
| Occupied weight | Target weight while the room is occupied but no tracked person is in it. | 0.5 |
| Unoccupied weight | Target weight otherwise. Must be at least 0.001. | 0.001 |
| Stale temperature limit | How long a room's last reading keeps being used after its temperature sensor was last seen working, before the room is left out. Long enough to ride out a restart or an integration reload. Zone-wide only. | 5 |

Validation: person and occupancy taus must be greater than 0; deactivate and dropout taus can't be negative; weights can't be negative; the unoccupied weight must be at least 0.001 and the stale limit greater than 0.

### Rooms

**Rooms** shows a list: your rooms, then **Add a new room** and **Done**. Pick a room to edit it; saving a room brings you back to the list.

| Field | Description |
|---|---|
| Area | The Home Assistant area this room is. Chosen when the room is added and fixed after that — to use a different area, remove the room and add a new one. An area can be a room once per zone (the same area can also be a room in another zone). |
| Temperature sensor | A `sensor` with device class `temperature` measuring this room. |
| Occupancy sensors | Any number of `binary_sensor` or `input_boolean` entities. The room counts as occupied while any of them is `on`. |
| Occupancy template | When true, the room also counts as occupied. |
| Opening entities | Any number of `binary_sensor` or `input_boolean` entities, like window or door contacts. While any of them is `on`, the room is left out and its weight fades to 0 (the `inactive` status). `unavailable` or `unknown` counts as closed. |
| Opening template | When it renders true, the room is also left out. True means `true`, `on`, `yes`, `enable`, or any number other than `0` — Home Assistant's usual rule, so a template that returns a number (a temperature, a count) by mistake keeps the room out. Anything else — including `unavailable`, `unknown` or a template error — counts as false. Errors, and results that aren't a recognisable true/false, are logged. Leave both opening fields blank to always count the room. |
| Overrides (collapsed) | Any of the taus and weights above, for this room only. Leave a field blank to use the zone's default. |
| Remove this room | Only when editing: removes the room when you submit. |

If you save a room with no occupancy sensors and no occupancy template while the zone has no people yet, a note says the room can never count as occupied. Continue to save it anyway — for example if you're about to add people.

### People

**People** works the same way: your people, then **Add a new person** and **Done**. Adding or editing a person takes two steps:

1. **Name** (shown in each room's `people` attribute) and **location entity** — an entity whose state, or one of its attributes, is the area the person is in. When editing, a **Remove this person** switch appears here.
2. **Attribute** (optional — leave blank to use the entity's state) and whether the value is an **area name** (e.g. "Living Room") or an **area ID** (e.g. `living_room`). The entity's current state is shown to help.

A person only affects a room if their value matches one of this zone's rooms. Anything else — an area with no room here, `not_home`, `unavailable`, a value that isn't an area — counts as "not in any room". A `person` or `device_tracker` entity on its own reports a zone (like `home`), not a room, so it isn't enough.

## Entities

Each zone creates one device ("OORT `<zone name>`") containing:

- One **`<Room> weight`** sensor per room: `sensor.oort_<zone>_<room>_weight`
- One **Temperature** sensor for the zone: `sensor.oort_<zone>_temperature`

(For example, a zone named "Home" with a "Kitchen" room gives `sensor.oort_home_kitchen_weight` and `sensor.oort_home_temperature`.)

### `<Room> weight` sensor

Its state is the room's current weight (a number between 0 and the largest of the room's weights), stored to 4 decimal places. It's written once a minute, and straight away when the room's status or inputs change — not on every temperature reading. Attributes:

| Attribute | Meaning |
|---|---|
| `area_id` | The ID of the room's Home Assistant area. |
| `status` | `inactive`, `person`, `occupied`, or `unoccupied` — see [How it works](#how-it-works). |
| `active` | `false` while the room is "open" (an opening entity is on or the opening template is true); otherwise `true`. |
| `temperature_available` | Whether the room's temperature sensor currently has a usable reading. |
| `temperature_stale` | `true` once the temperature sensor has been unavailable longer than the stale limit — see [Fallback and stale sensors](#fallback-and-stale-sensors). |
| `person_present` | Whether any tracked person currently resolves to this room. |
| `occupied` | The combined result of the room's occupancy sensors and occupancy template. |
| `people` | List of the names of people currently present in this room. |
| `target_weight` | The weight this room is currently moving toward. |
| `tau` | The time constant (minutes) currently used to move toward `target_weight`. |
| `tau_name` | Which tau that is: `person_rise`, `person_fall`, `occupancy_rise`, `occupancy_fall`, `deactivate` or `dropout` — so you can see both the direction and the reason. |
| `last_occupied_state` | The last status that was `person` or `occupied` (used to pick the correct fall tau); `null` if the room has never been occupied. |

Room weight is restored across a Home Assistant restart (the downtime itself isn't counted as elapsed time — the room resumes at the weight it had before shutdown rather than jumping as if time had passed).

### `Temperature` sensor

Its state is the zone's occupancy-weighted temperature, rounded to 0.1°, in the temperature unit Home Assistant was set to when you created the zone (sensor readings in other units are converted automatically). The zone keeps that unit even if you later change Home Assistant's unit system; Home Assistant converts it for display as it does for any other temperature sensor. It becomes `unavailable` when nothing can be averaged — see below. Attributes:

| Attribute | Meaning |
|---|---|
| `total_weight` | Sum of the weights of rooms that contributed a valid temperature (excludes the fallback term's weight), to 4 decimal places. Not recorded in history. |
| `contributing_rooms` | How many rooms contributed a valid temperature to the weighted average. |
| `fallback` | `true` when the plain-average fallback term outweighs every room's own weighted contribution — see below. |

## Fallback and stale sensors

Every room's temperature sensor is read continuously, independent of whether the room counts as active. If a room's sensor becomes unavailable, unknown, or reports a value or unit Home Assistant can't convert to a temperature, the room keeps using its **last known reading** while its weight fades out at the dropout tau. If the sensor stays unusable for longer than the zone's **stale temperature limit** (counted from when it was last seen working), that room's `temperature_stale` attribute becomes `true` and it's dropped from the average entirely (it still keeps its weight and status, it just no longer contributes a temperature).

The overall `Temperature` sensor also has a small **fallback term**: a plain average of every room's current valid reading (regardless of whether that room is active), with a fixed weight equal to 1% of your smallest room's unoccupied weight. This term is normally negligible next to any room with real weight, but it keeps the sensor producing a sensible number — rather than becoming unavailable — while every room is fading toward zero (for example, right after startup, or if every room is deactivated at once). If every temperature sensor is down at once, the fallback uses the rooms' last readings instead, until they go stale. The `fallback` attribute turns `true` when this term's weight exceeds the sum of every room's own weight, which is your cue that the temperature is currently closer to a whole-home average than to an occupancy-weighted one.

Last readings are saved across restarts, so a normal reboot doesn't make the output jump while sensors reconnect. After a longer downtime, readings older than the stale limit aren't reused; each room joins in again as soon as its sensor reports.

The `Temperature` sensor becomes `unavailable` only when there's nothing at all to average — every room is either stale or has never reported a valid temperature.

So during a total sensor outage the thermostat gets the last readings for at most the stale limit (5 minutes by default), then `unavailable`. If your thermostat can turn itself off when its sensor is unavailable — `dual_smart_thermostat`'s `sensor_stale_duration`, for example — set that too, a little longer than OORT's stale limit.

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

Any other climate integration with an equivalent "current/target temperature sensor" option — including UI-configured ones — works the same way: point that option at your zone's `sensor.oort_<zone>_temperature`.

## Known limitations

- Each zone creates a single device holding all of its room-weight and temperature sensors, rather than one device per room. Room sensors won't appear on their own room's area page in Home Assistant unless you manually assign each `<Room> weight` entity to that area (**Settings → Devices & services → Entities**).
- No HACS listing yet; manual installation only.
- A room's area can't be changed after the room is created — remove and re-add it instead.

## Troubleshooting

**Repairs shows "Some OORT rooms can never count as occupied".** This fires when a room has no occupancy sensors, no occupancy template, and the zone has no people configured — so that room can only ever sit at its unoccupied weight. Add occupancy sensors or a template to the room, or add a person to the zone — both from the zone's **Configure** menu. The issue clears automatically once either is true, or if the room is removed.

**The `Temperature` sensor is `unavailable`.** Every configured room's temperature sensor is either stale (unavailable for longer than the stale limit) or has never reported a usable value. Check each room's `temperature_available` and `temperature_stale` attributes to find the affected sensor(s).

## Development

Tests run against Home Assistant 2026.9.3 (via `pytest-homeassistant-custom-component`), which needs Python 3.14:

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/python -m pytest
```

This runs 151 tests: the pure-Python weighting/smoothing engine, the config flow, and end-to-end runtime tests against an in-memory Home Assistant.

Lint with [ruff](https://docs.astral.sh/ruff/):

```sh
uvx ruff check --select F,E9,B custom_components tests
```

## License

MIT — see [LICENSE](LICENSE).
