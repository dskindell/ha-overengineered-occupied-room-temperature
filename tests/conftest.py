"""Make the Home Assistant-free engine importable on its own.

The integration package's ``__init__.py`` will import Home Assistant, so the
engine tests import ``engine`` directly from the component directory instead.
"""

from pathlib import Path
import sys

sys.path.insert(
    0,
    str(
        Path(__file__).parent.parent
        / "custom_components"
        / "overengineered_occupied_room_temperature"
    ),
)
