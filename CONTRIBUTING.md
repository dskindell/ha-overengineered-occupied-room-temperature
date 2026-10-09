# Contributing to OORT

## Set up

The tests run against Home Assistant 2026.9.3 (through `pytest-homeassistant-custom-component`), which needs Python 3.14. With [uv](https://docs.astral.sh/uv/):

```sh
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements_test.txt
```

Then install the [pre-commit](https://pre-commit.com/) hooks once, so each commit gets the same checks as CI:

```sh
uvx pre-commit install
```

## Layout

`custom_components/overengineered_occupied_room_temperature/`:

- `engine.py`: the calculation itself, pure Python with no Home Assistant imports. `step_zone()` advances every room of a zone and returns the weighted temperature, so it can replay recorded history offline.
- `const.py`: keys, and `SETTINGS`, the one table of numeric settings (default, limits, unit, per room or zone-wide). A new setting needs a row there, a field in the engine and translations.
- `config_flow.py`: the zone menu, shared by setup and **Configure**.
- `zone.py`: the runtime. It watches entities and templates, runs the minute timer and decides when sensors are written.
- `storage.py`: each room's state, saved across restarts.
- `sensor.py`: the `<Room> weight` and `Temperature` sensors.
- `translations/en.json`: edited by hand. Its `config` and `options` blocks must stay identical, and numbers from `SETTINGS` appear as placeholders (`{<key>_default}`, `{<key>_minimum}`, `{<key>_maximum}`), never typed in.

Tests are in `tests/`, one module per source module; `test_zone.py` runs whole zones end to end in an in-memory Home Assistant.

## Checks

CI runs all of these; the pre-commit hooks cover the fast ones.

```sh
.venv/bin/python -m pytest --cov=custom_components/overengineered_occupied_room_temperature --cov-branch   # coverage must stay at 97% or more
uvx ruff@0.16.9 check custom_components tests scripts
uvx ruff@0.16.9 format --check custom_components tests scripts   # without --check it fixes the files
uvx codespell@2.4.3 custom_components tests scripts README.md CONTRIBUTING.md .github pyproject.toml
uvx --from actionlint-py==1.7.12.25 actionlint .github/workflows/*.yml
.venv/bin/python scripts/check_rules.py references       # no references to design notes in the code
uv pip install --python .venv/bin/python mypy==2.3.1
.venv/bin/python -m mypy custom_components/overengineered_occupied_room_temperature   # strict
docker run --rm -v "$PWD":/github/workspace ghcr.io/home-assistant/hassfest   # Home Assistant's integration validator
```

A second workflow runs the tests against the newest Home Assistant every week.

## README images

The charts and the demo animation are drawn by running the engine with the default settings. Regenerate them when a default or the engine's curves change:

```sh
.venv/bin/python scripts/weight_charts.py           # docs/images/*.svg; --check fails if they're out of date
uv pip install --python .venv/bin/python cairosvg
.venv/bin/python scripts/demo_animation.py          # docs/images/oort-demo.gif
```

## Releases

Versions follow [Semantic Versioning](https://semver.org/): fixes raise the patch number, new features or settings the minor number, and changes that break existing setups (a renamed entity or attribute, a removed setting) the major number. A change to what a zone stores comes with a config entry migration.

Every change is released first as a beta, a GitHub pre-release such as `v1.1.0-beta.1`. A beta becomes a stable release (the same commit, tagged `v1.1.0`) once it has run for at least 48 hours on a real installation, including a restart. Changes to the documentation alone aren't released.

To release:

1. In a pull request, set `version` in `manifest.json` (without the `v`) and add a `## [<version>] - <date>` section to `CHANGELOG.md`.
2. After it merges, check that CI passed on that commit of `main`.
3. For a stable release, check the beta on the real installation: no OORT errors or warnings in the log, no OORT Repairs issues, and every room's weight carried across the restart.
4. Tag that commit and push the tag: `git tag v<version> && git push origin v<version>`. The release workflow checks that the tag matches the manifest and is on `main`, then publishes the release with that version's changelog section, as a pre-release when the version has a suffix such as `-beta.1`.

## Commits

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/): `<type>[optional scope]: <description>`, for example `fix(zone): end delays on time`. The allowed types are `feat`, `fix`, `perf`, `refactor`, `style`, `docs`, `test`, `ci`, `build`, `chore` and `revert`. Leave out `Co-Authored-By` trailers. The `commit-msg` hook checks each message, and CI checks every commit it's given.

Code comments explain why, not what; keep them short.
