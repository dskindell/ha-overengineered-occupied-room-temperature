# Overengineered Occupied-Room Temperature (OORT)

Home Assistant custom integration that keeps the rooms you're in at your chosen temperature with a whole-home HVAC system, by producing an occupancy-weighted temperature for your thermostat.

OORT needs a climate controller that can use an external temperature sensor as its current temperature.

## Development

Tests run against Home Assistant 2026.9.3 (via `pytest-homeassistant-custom-component`), which needs Python 3.14:

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
.venv/bin/python -m pytest
```
