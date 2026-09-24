# Overengineered Occupied-Room Temperature (OORT)

Home Assistant custom integration that keeps the rooms you're in at your chosen temperature with a whole-home HVAC system, by producing an occupancy-weighted temperature for your thermostat.

OORT needs a climate controller that can use an external temperature sensor as its current temperature.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
```
