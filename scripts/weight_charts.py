"""Draw the README's weight charts by running the engine with the default settings.

python scripts/weight_charts.py          # write docs/images/*.svg
python scripts/weight_charts.py --check  # fail if they're out of date
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
import math
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from custom_components.overengineered_occupied_room_temperature import (
    const,
    engine,
)
from custom_components.overengineered_occupied_room_temperature.engine import (
    RoomConfig,
    RoomInputs,
    RoomState,
    Status,
)

OUT = Path(__file__).resolve().parents[1] / "docs" / "images"
DEFAULT = engine.room_config(const.DEFAULTS, {})
TAUS = DEFAULT.taus
WEIGHTS = DEFAULT.weights
STEP = 0.25  # minutes between plotted points

EMPTY = RoomInputs(open=False, person_present=False, occupied=False, temperature=20.0)
PERSON = replace(EMPTY, person_present=True)
OCCUPIED = replace(EMPTY, occupied=True)
OPEN = replace(PERSON, open=True)
DROPOUT = replace(PERSON, temperature=None)

# Light and dark colors: chart chrome, categorical slots, a blue ordinal ramp
# (light to dark on the light surface, reversed on the dark one).
LIGHT = {
    "surface": "#fcfcfb",
    "ink": "#0b0b0b",
    "ink2": "#52514e",
    "muted": "#898781",
    "grid": "#e1e0d9",
    "axis": "#c3c2b7",
    "band": "#f0efec",
    "c1": "#2a78d6",
    "c2": "#eb6834",
    "c3": "#1baf7a",
    "c4": "#eda100",
    "r1": "#86b6ef",
    "r2": "#5598e7",
    "r3": "#2a78d6",
    "r4": "#1c5cab",
    "r5": "#104281",
}
DARK = {
    "surface": "#1a1a19",
    "ink": "#ffffff",
    "ink2": "#c3c2b7",
    "muted": "#898781",
    "grid": "#2c2c2a",
    "axis": "#383835",
    "band": "#262624",
    "c1": "#3987e5",
    "c2": "#d95926",
    "c3": "#199e70",
    "c4": "#c98500",
    "r1": "#184f95",
    "r2": "#256abf",
    "r3": "#3987e5",
    "r4": "#86b6ef",
    "r5": "#cde2fb",
}
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'

WIDTH, PAD = 760, 24
LEFT, RIGHT, PLOT_HEIGHT = 64, WIDTH - PAD, 280
GUIDE_LABELS = 84  # right margin for guide labels
CHAR_WIDTH = 7.0  # rough width of a legend character, for wrapping


def settled(status: Status, weight: float) -> RoomState:
    """A room that has sat in ``status`` long enough to reach ``weight``."""
    occupied = status if status in (Status.PERSON, Status.OCCUPIED) else None
    return RoomState(weight=weight, target=weight, status=status, last_occupied_state=occupied)


def run(
    rooms: Mapping[str, tuple[RoomState, RoomConfig]],
    inputs_at: Callable[[str, float], RoomInputs],
    minutes: float,
) -> list[tuple[float, dict[str, float]]]:
    """Each room's weight every ``STEP`` minutes, stepped by the engine."""
    states = {key: state for key, (state, _) in rooms.items()}
    points = []
    for i in range(round(minutes / STEP) + 1):
        t = i * STEP
        states = {
            key: engine.step_room(states[key], inputs_at(key, t), config, t * 60)
            for key, (_, config) in rooms.items()
        }
        points.append((t, {key: state.weight for key, state in states.items()}))
    return points


def one_room(start: RoomState, inputs: RoomInputs, minutes: float) -> list[tuple[float, float]]:
    """One room's weight after its inputs change at minute 0."""
    points = run({"room": (start, DEFAULT)}, lambda _, t: inputs, minutes)
    return [(t, weights["room"]) for t, weights in points]


@dataclass
class Series:
    label: str
    """Legend text."""
    tag: str | None
    """Label on the line, if any."""
    color: str
    points: list[tuple[float, float]]
    tag_at: float
    """Minute at which the tag sits."""
    tag_dx: float = 8
    tag_dy: float = -8
    tag_anchor: str = "start"
    dots: list[float] = field(default_factory=list)
    """Minutes to mark on the line."""


@dataclass
class Chart:
    name: str
    title: str
    subtitle: list[str]
    x_max: float
    x_step: float
    y_ticks: list[tuple[float, str]]
    series: list[Series]
    bands: list[tuple[float, float, str]] = field(default_factory=list)
    guides: list[tuple[float, str]] = field(default_factory=list)


WEIGHT_TICKS = [(v / 4, f"{v / 4:g}") for v in range(5)]
PERCENT_TICKS = [(v / 4, f"{v * 25}%") for v in range(5)]


def rise_chart() -> Chart:
    empty = settled(Status.UNOCCUPIED, WEIGHTS.base)
    return Chart(
        name="default-rise",
        title="Rising, with the default settings",
        subtitle=[
            "Weight of an empty room after someone arrives at minute 0.",
            "Dots mark one tau: 63% of the way there.",
        ],
        x_max=40,
        x_step=5,
        y_ticks=WEIGHT_TICKS,
        series=[
            Series(
                f"A tracked person arrives (person rise tau, {TAUS.person_rise:g} min)",
                "person rise",
                "c1",
                one_room(empty, PERSON, 40),
                tag_at=6,
                tag_dy=18,
                dots=[TAUS.person_rise],
            ),
            Series(
                f"An occupancy sensor turns on (occupancy rise tau, {TAUS.occupancy_rise:g} min)",
                "occupancy rise",
                "c2",
                one_room(empty, OCCUPIED, 40),
                tag_at=16,
                tag_dy=18,
                dots=[TAUS.occupancy_rise],
            ),
        ],
    )


def fall_chart() -> Chart:
    person = settled(Status.PERSON, WEIGHTS.person)
    return Chart(
        name="default-fall",
        title="Falling, with the default settings",
        subtitle=[
            "Weight of a room after something changes at minute 0.",
            "Dots mark one tau: 63% of the way down.",
        ],
        x_max=30,
        x_step=5,
        y_ticks=WEIGHT_TICKS,
        series=[
            Series(
                f"A window opens (open tau, {TAUS.open:g} min)",
                "open",
                "c3",
                one_room(person, OPEN, 30),
                tag_at=1,
                tag_dx=4,
                tag_dy=40,
                dots=[TAUS.open],
            ),
            Series(
                f"The tracked person leaves (person fall tau, {TAUS.person_fall:g} min)",
                "person fall",
                "c1",
                one_room(person, EMPTY, 30),
                tag_at=5,
                tag_dx=4,
                tag_dy=24,
                dots=[TAUS.person_fall],
            ),
            Series(
                f"The temperature sensor drops out (dropout tau, {TAUS.dropout:g} min)",
                "dropout",
                "c4",
                one_room(person, DROPOUT, 30),
                tag_at=5,
                tag_dx=8,
                tag_dy=-10,
                dots=[TAUS.dropout],
            ),
            Series(
                f"Occupancy ends (occupancy fall tau, {TAUS.occupancy_fall:g} min)",
                "occupancy fall",
                "c2",
                one_room(settled(Status.OCCUPIED, WEIGHTS.occupied), EMPTY, 30),
                tag_at=13,
                tag_dx=4,
                tag_dy=-10,
                dots=[TAUS.occupancy_fall],
            ),
        ],
    )


def tau_chart() -> Chart:
    taus = (1, 3, 5, 10, 20)
    series = []
    for i, tau in enumerate(taus, start=1):
        config = replace(DEFAULT, taus=replace(TAUS, person_rise=tau))
        start = RoomState(weight=0.0, target=0.0, status=Status.UNOCCUPIED)
        points = run({"room": (start, config)}, lambda _, t: PERSON, 60)
        series.append(
            Series(
                f"tau {tau} min",
                None,
                f"r{i}",
                [(t, weights["room"] / WEIGHTS.person) for t, weights in points],
                tag_at=0,
                dots=[tau],
            )
        )
    return Chart(
        name="tau-comparison",
        title="How the tau sets the pace",
        subtitle=[
            "Share of the way from the old weight to the new one — the same for every tau,",
            "rising or falling. Each dot sits at its tau: 63% of the way there.",
        ],
        x_max=60,
        x_step=10,
        y_ticks=PERCENT_TICKS,
        series=series,
        guides=[(1 - math.exp(-1), "1 tau: 63%"), (1 - math.exp(-3), "3 taus: 95%")],
    )


def trip_chart() -> Chart:
    # To the kitchen at 10, back at 15, to the kitchen for good at 35.
    def inputs_at(room: str, t: float) -> RoomInputs:
        in_kitchen = 10 <= t < 15 or t >= 35
        return PERSON if (room == "kitchen") == in_kitchen else EMPTY

    epsilon = engine.fallback_epsilon([WEIGHTS.base, WEIGHTS.base])
    series = []
    variants = (
        (TAUS.person_fall, 19, "end", -8, 4),
        (10, 25, "start", -8, -10),
        (20, 48, "start", -8, -12),
    )
    for i, (tau, tag_at, anchor, dx, dy) in enumerate(variants):
        config = replace(DEFAULT, taus=replace(TAUS, person_fall=tau))
        rooms = {
            "living": (settled(Status.PERSON, WEIGHTS.person), config),
            "kitchen": (settled(Status.UNOCCUPIED, WEIGHTS.base), config),
        }
        points = run(rooms, inputs_at, 60)
        default = " (default)" if tau == TAUS.person_fall else ""
        series.append(
            Series(
                f"person fall tau {tau:g} min{default}",
                f"{tau:g} min",
                f"r{(1, 3, 5)[i]}",
                [(t, w["living"] / (w["living"] + w["kitchen"] + epsilon)) for t, w in points],
                tag_at=tag_at,
                tag_dx=dx,
                tag_dy=dy,
                tag_anchor=anchor,
            )
        )
    return Chart(
        name="short-trip",
        title="A short trip out, then a real move",
        subtitle=[
            "The living room's share of the zone temperature, with a second room (the kitchen).",
            "A tracked person steps into the kitchen for 5 minutes, comes back, then moves there.",
        ],
        x_max=60,
        x_step=10,
        y_ticks=PERCENT_TICKS,
        series=series,
        bands=[(10, 15, "in the kitchen"), (35, 60, "in the kitchen")],
    )


def fmt(value: float) -> str:
    return f"{value:.1f}".removesuffix(".0")


def style() -> str:
    def rules(colors: Mapping[str, str]) -> str:
        return "".join(
            [
                f".bg{{fill:{colors['surface']}}}",
                f".t1{{fill:{colors['ink']}}}",
                f".t2{{fill:{colors['ink2']}}}",
                f".t3{{fill:{colors['muted']}}}",
                f".grid{{stroke:{colors['grid']}}}",
                f".axis{{stroke:{colors['axis']}}}",
                f".band{{fill:{colors['band']}}}",
                *(
                    f".{key}{{stroke:{colors[key]};fill:{colors[key]}}}"
                    for key in colors
                    if key[0] in "cr" and key[1:].isdigit()
                ),
                f".halo{{fill:{colors['surface']};stroke:{colors['surface']}}}",
            ]
        )

    return (
        f"text{{font-family:{FONT};font-size:13px}}"
        ".title{font-size:17px;font-weight:600}.tag{font-weight:600}"
        "text.halo{stroke-width:4px;stroke-linejoin:round}"
        "path.line{fill:none;stroke-width:2px;stroke-linejoin:round;stroke-linecap:round}"
        ".grid,.axis{stroke-width:1px}.guide{stroke-dasharray:4 4}"
        f"{rules(LIGHT)}@media (prefers-color-scheme: dark){{{rules(DARK)}}}"
    )


def text(px: float, py: float, value: str, cls: str, anchor: str = "start") -> str:
    return f'<text x="{fmt(px)}" y="{fmt(py)}" class="{cls}" text-anchor="{anchor}">{value}</text>'


def haloed(px: float, py: float, value: str, cls: str, anchor: str = "start") -> list[str]:
    """Text on a surface-colored outline, to stay readable over lines."""
    return [text(px, py, value, "halo", anchor), text(px, py, value, cls, anchor)]


def draw(chart: Chart) -> str:
    header: list[str] = [text(PAD, 32, chart.title, "t1 title")]
    line_y = 32.0
    for line in chart.subtitle:
        line_y += 20
        header.append(text(PAD, line_y, line, "t2"))

    # Legend rows, wrapped to the width.
    legend_x, legend_y = PAD, line_y + 28
    for series in chart.series:
        width = 20 + CHAR_WIDTH * len(series.label) + 20
        if legend_x > PAD and legend_x + width > WIDTH - PAD:
            legend_x, legend_y = PAD, legend_y + 20
        header.append(
            f'<rect class="{series.color}" x="{fmt(legend_x)}" y="{fmt(legend_y - 6)}" '
            'width="14" height="3" rx="1.5"/>'
        )
        header.append(text(legend_x + 20, legend_y, series.label, "t2"))
        legend_x += width

    top = legend_y + 26
    bottom = top + PLOT_HEIGHT
    height = bottom + 52
    right = RIGHT - GUIDE_LABELS if chart.guides else RIGHT
    y_min, y_max = chart.y_ticks[0][0], chart.y_ticks[-1][0]

    def x(t: float) -> float:
        return LEFT + (right - LEFT) * t / chart.x_max

    def y(v: float) -> float:
        return bottom - (bottom - top) * (v - y_min) / (y_max - y_min)

    def at(points: list[tuple[float, float]], t: float) -> float:
        return min(points, key=lambda p: abs(p[0] - t))[1]

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {fmt(height)}" '
        f'width="{WIDTH}" height="{fmt(height)}" role="img" aria-labelledby="title desc">',
        f'<title id="title">{chart.title}</title>',
        f'<desc id="desc">{" ".join(chart.subtitle)}</desc>',
        f"<style>{style()}</style>",
        f'<rect class="bg" width="{WIDTH}" height="{fmt(height)}" rx="8"/>',
        *header,
    ]
    for start, end, label in chart.bands:
        out.append(
            f'<rect class="band" x="{fmt(x(start))}" y="{fmt(top)}" '
            f'width="{fmt(x(end) - x(start))}" height="{PLOT_HEIGHT}"/>'
        )
        out.append(text((x(start) + x(end)) / 2, top + 18, label, "t3", "middle"))
    for value, label in chart.y_ticks:
        cls = "axis" if value == y_min else "grid"
        out.append(
            f'<line class="{cls}" x1="{LEFT}" x2="{right}" '
            f'y1="{fmt(y(value))}" y2="{fmt(y(value))}"/>'
        )
        out.append(text(LEFT - 8, y(value) + 4, label, "t3", "end"))
    for i in range(round(chart.x_max / chart.x_step) + 1):
        t = i * chart.x_step
        out.append(text(x(t), bottom + 20, fmt(t), "t3", "middle"))
    out.append(text((LEFT + right) / 2, bottom + 42, "Minutes", "t3", "middle"))
    for value, label in chart.guides:
        out.append(
            f'<line class="axis guide" x1="{LEFT}" x2="{right}" '
            f'y1="{fmt(y(value))}" y2="{fmt(y(value))}"/>'
        )
        out.append(text(right + 8, y(value) + 4, label, "t3"))
    for series in chart.series:
        path = " ".join(
            f"{'M' if i == 0 else 'L'}{fmt(x(t))},{fmt(y(v))}"
            for i, (t, v) in enumerate(series.points)
        )
        out.append(f'<path class="line {series.color}" d="{path}"/>')
    for series in chart.series:
        for t in series.dots:
            cx, cy = fmt(x(t)), fmt(y(at(series.points, t)))
            out.append(f'<circle class="halo" cx="{cx}" cy="{cy}" r="6"/>')
            out.append(f'<circle class="{series.color}" cx="{cx}" cy="{cy}" r="4"/>')
    for series in chart.series:
        if series.tag is None:
            continue
        tx = x(series.tag_at) + series.tag_dx
        ty = y(at(series.points, series.tag_at)) + series.tag_dy
        out += haloed(tx, ty, series.tag, "t1 tag", series.tag_anchor)
    out.append("</svg>")
    return "\n".join(out) + "\n"


def charts() -> Iterable[Chart]:
    return (rise_chart(), fall_chart(), tau_chart(), trip_chart())


def main(args: list[str]) -> int:
    check = args == ["--check"]
    stale = []
    for chart in charts():
        path = OUT / f"{chart.name}.svg"
        svg = draw(chart)
        if check:
            if not path.exists() or path.read_text() != svg:
                stale.append(path.name)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(svg)
    if stale:
        print(f"Out of date (run scripts/weight_charts.py): {', '.join(stale)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
