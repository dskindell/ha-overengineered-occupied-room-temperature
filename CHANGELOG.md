# Changelog

All notable changes to OORT. Versions follow [Semantic Versioning](https://semver.org/); betas are published as GitHub pre-releases, which HACS offers only to those who turn on the repository's pre-release switch.

## [1.0.0-beta.1] - 2026-10-09

First release.

### Added

- A `Temperature` sensor per zone: the occupancy-weighted temperature of its rooms, for any thermostat that can use an external temperature sensor.
- A `<Room> weight` sensor per room, with attributes showing why it has its weight.
- Rooms with a temperature sensor, occupancy sensors or template, and opening sensors or template; people placed by an entity whose state or attribute is an area name or area ID.
- Weights that move toward a target set by who is in the room, at configurable rates (taus), with enter and exit delays; open rooms and dropped-out sensors fade out.
- A Settings screen with defaults for every room, per-room overrides, a note on each value that differs from the integration's default and a way to restore the defaults.
- A plain-average fallback when no room carries weight, a stale-reading limit, room state saved across restarts and outages, and a startup grace period.
- Renamed source entities are followed automatically; Repairs issues for rooms that can never count as occupied and for entities that don't exist.
- Default settings tuned on two weeks of recorded data from a real house.
