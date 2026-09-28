"""Draw the README's demo animation by running the engine with the default settings.

python scripts/demo_animation.py  # write docs/images/oort-demo.gif

Needs cairosvg and Pillow: uv pip install --python .venv/bin/python cairosvg
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import sys

import cairosvg
from PIL import Image
from weight_charts import DEFAULT, FONT, LIGHT, OUT

from custom_components.overengineered_occupied_room_temperature import engine
from custom_components.overengineered_occupied_room_temperature.engine import (
    RoomInputs,
    RoomState,
    Status,
)

WIDTH, HEIGHT = 760, 440
STEP = 0.5  # simulated minutes per frame
FRAME_MS = 70
HOLD_FRAMES = 30  # extra frames on the last one before the loop restarts
WALK = 1.0  # simulated minutes the person takes between rooms
START = 18 * 60 + 30  # clock at minute 0: 6:30 PM
ACCENT = LIGHT["c1"]
WALL = LIGHT["muted"]


@dataclass(frozen=True)
class Room:
    key: str
    name: str
    temperature: float
    x: float
    y: float


PLAN_X, PLAN_Y, ROOM_W, ROOM_H = 24, 64, 184, 150
ROOMS = (
    Room("office", "Office", 69.0, PLAN_X, PLAN_Y),
    Room("kitchen", "Kitchen", 73.0, PLAN_X + ROOM_W, PLAN_Y),
    Room("living", "Living room", 72.0, PLAN_X, PLAN_Y + ROOM_H),
    Room("bedroom", "Bedroom", 66.0, PLAN_X + ROOM_W, PLAN_Y + ROOM_H),
)
BY_KEY = {room.key: room for room in ROOMS}
WALL_ROOM = BY_KEY["living"]

# (minute the person walks in, room, caption)
STORY = (
    (0, "office", "Working in the office: OORT reads the office, not the living-room wall."),
    (22, "kitchen", "A quick stop in the kitchen: the weights shift over a few minutes."),
    (30, "living", "Evening in the living room: OORT follows."),
    (
        56,
        "bedroom",
        "Off to bed: the thermostat now sees the 66° bedroom, not the 72° living room.",
    ),
)
MINUTES = 84.0


def where(t: float) -> tuple[str, int]:
    """The room the person is in at minute ``t``, and the story step."""
    index = max(i for i, (start, _, _) in enumerate(STORY) if start <= t)
    return STORY[index][1], index


def simulate() -> list[tuple[float, dict[str, RoomState], float]]:
    """Each frame's minute, room states and OORT temperature."""
    first = STORY[0][1]
    states = {
        room.key: RoomState(
            weight=DEFAULT.weights.person if room.key == first else DEFAULT.weights.base,
            target=DEFAULT.weights.person if room.key == first else DEFAULT.weights.base,
            status=Status.PERSON if room.key == first else Status.UNOCCUPIED,
        )
        for room in ROOMS
    }
    frames = []
    for i in range(round(MINUTES / STEP) + 1):
        t = i * STEP
        here, _ = where(t)
        rooms = {
            room.key: (
                states[room.key],
                RoomInputs(
                    open=False,
                    person_present=room.key == here,
                    occupied=False,
                    temperature=room.temperature,
                ),
                DEFAULT,
            )
            for room in ROOMS
        }
        step = engine.step_zone(rooms, t * 60)
        states = step.rooms
        assert step.result.temperature is not None
        frames.append((t, states, step.result.temperature))
    return frames


def mix(color: str, amount: float) -> str:
    """``color`` blended over the surface by ``amount`` (0-1)."""
    surface = LIGHT["surface"]
    channels = (
        round(int(surface[i : i + 2], 16) * (1 - amount) + int(color[i : i + 2], 16) * amount)
        for i in (1, 3, 5)
    )
    return "#" + "".join(f"{c:02x}" for c in channels)


def clock(t: float) -> str:
    minutes = START + int(t)
    hour, minute = divmod(minutes, 60)
    return f"{(hour - 1) % 12 + 1}:{minute:02d} {'PM' if hour < 24 else 'AM'}"


def text(x: float, y: float, value: str, size: float, fill: str, **attrs: str) -> str:
    extra = "".join(f' {key.replace("_", "-")}="{val}"' for key, val in attrs.items())
    return f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}"{extra}>{value}</text>'


def person(t: float) -> tuple[float, float]:
    """The person's position: the centre of their room, walking between rooms."""
    here, index = where(t)
    target = BY_KEY[here]
    tx, ty = target.x + ROOM_W / 2, target.y + ROOM_H / 2 + 14
    if index == 0 or t - STORY[index][0] >= WALK:
        return tx, ty
    source = BY_KEY[STORY[index - 1][1]]
    sx, sy = source.x + ROOM_W / 2, source.y + ROOM_H / 2 + 14
    f = (t - STORY[index][0]) / WALK
    return sx + (tx - sx) * f, sy + (ty - sy) * f


def frame(t: float, states: dict[str, RoomState], history: list[tuple[float, float]]) -> str:
    ink, ink2, muted = LIGHT["ink"], LIGHT["ink2"], LIGHT["muted"]
    total = sum(state.weight for state in states.values())
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f"font-family='{FONT}'>",
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{LIGHT["surface"]}"/>',
        text(24, 38, "OORT follows you from room to room", 19, ink, font_weight="600"),
        text(WIDTH - 24, 38, clock(t), 17, ink2, text_anchor="end"),
    ]

    # Floor plan: each room tinted by its share of the zone temperature.
    for room in ROOMS:
        share = states[room.key].weight / total
        out.append(
            f'<rect x="{room.x}" y="{room.y}" width="{ROOM_W}" height="{ROOM_H}" '
            f'fill="{mix(ACCENT, 0.55 * share)}" stroke="{LIGHT["axis"]}" stroke-width="2"/>'
        )
        out.append(text(room.x + 12, room.y + 24, room.name, 14, ink, font_weight="600"))
        out.append(
            text(
                room.x + ROOM_W - 12,
                room.y + 24,
                f"{room.temperature:.0f}°",
                14,
                ink2,
                text_anchor="end",
            )
        )
        bar_x, bar_y, bar_w = room.x + 12, room.y + ROOM_H - 22, ROOM_W - 70
        out.append(
            f'<rect x="{bar_x}" y="{bar_y}" width="{bar_w}" height="8" rx="4" '
            f'fill="{LIGHT["grid"]}"/>'
        )
        if share * bar_w >= 1:
            out.append(
                f'<rect x="{bar_x}" y="{bar_y}" width="{share * bar_w:.1f}" height="8" '
                f'rx="4" fill="{ACCENT}"/>'
            )
        out.append(
            text(room.x + ROOM_W - 12, bar_y + 9, f"{share:.0%}", 13, ink2, text_anchor="end")
        )
    # The fixed sensor a plain thermostat would use.
    wx, wy = WALL_ROOM.x + 12, WALL_ROOM.y + 34
    out.append(
        f'<rect x="{wx}" y="{wy}" width="14" height="18" rx="3" fill="none" '
        f'stroke="{WALL}" stroke-width="2"/>'
    )
    out.append(text(wx + 20, wy + 13, "wall thermostat", 12, muted))
    px, py = person(t)
    out.append(f'<circle cx="{px:.1f}" cy="{py - 16:.1f}" r="8" fill="{ink}"/>')
    out.append(f'<path d="M{px - 12:.1f},{py + 14:.1f} a12,14 0 0 1 24,0 z" fill="{ink}"/>')

    # Readouts.
    panel_x = PLAN_X + 2 * ROOM_W + 32
    oort = history[-1][1]
    out.append(text(panel_x, PLAN_Y + 18, "OORT temperature", 14, ink2))
    out.append(text(panel_x, PLAN_Y + 60, f"{oort:.1f}°F", 34, ACCENT, font_weight="600"))
    out.append(text(panel_x + 168, PLAN_Y + 18, "Wall thermostat", 14, ink2))
    out.append(
        text(
            panel_x + 168,
            PLAN_Y + 60,
            f"{WALL_ROOM.temperature:.1f}°F",
            34,
            WALL,
            font_weight="600",
        )
    )

    # History chart.
    left, right = panel_x + 30, WIDTH - 24
    top, bottom = PLAN_Y + 118, PLAN_Y + 2 * ROOM_H
    legend_y = PLAN_Y + 92
    out.append(
        f'<line x1="{panel_x}" x2="{panel_x + 18}" y1="{legend_y - 4}" y2="{legend_y - 4}" '
        f'stroke="{ACCENT}" stroke-width="2.5"/>'
    )
    out.append(text(panel_x + 24, legend_y, "OORT", 12.5, ink2))
    out.append(
        f'<line x1="{panel_x + 80}" x2="{panel_x + 98}" y1="{legend_y - 4}" '
        f'y2="{legend_y - 4}" stroke="{WALL}" stroke-width="2" stroke-dasharray="5 4"/>'
    )
    out.append(text(panel_x + 104, legend_y, "wall thermostat", 12.5, ink2))
    low, high = 64.0, 74.0

    def cx(m: float) -> float:
        return left + (right - left) * m / MINUTES

    def cy(v: float) -> float:
        return bottom - (bottom - top) * (v - low) / (high - low)

    for value in (64, 66, 68, 70, 72, 74):
        out.append(
            f'<line x1="{left}" x2="{right}" y1="{cy(value):.1f}" y2="{cy(value):.1f}" '
            f'stroke="{LIGHT["axis"] if value == low else LIGHT["grid"]}"/>'
        )
        out.append(text(left - 6, cy(value) + 4, f"{value}°", 12, muted, text_anchor="end"))
    out.append(
        f'<line x1="{left}" x2="{cx(t):.1f}" y1="{cy(WALL_ROOM.temperature):.1f}" '
        f'y2="{cy(WALL_ROOM.temperature):.1f}" stroke="{WALL}" stroke-width="2" '
        'stroke-dasharray="5 4"/>'
    )
    path = " ".join(
        f"{'M' if i == 0 else 'L'}{cx(m):.1f},{cy(v):.1f}" for i, (m, v) in enumerate(history)
    )
    out.append(
        f'<path d="{path}" fill="none" stroke="{ACCENT}" stroke-width="2.5" '
        'stroke-linejoin="round"/>'
    )
    out.append(f'<circle cx="{cx(t):.1f}" cy="{cy(oort):.1f}" r="6" fill="{LIGHT["surface"]}"/>')
    out.append(f'<circle cx="{cx(t):.1f}" cy="{cy(oort):.1f}" r="4" fill="{ACCENT}"/>')

    # Caption.
    _, index = where(t)
    out.append(text(24, HEIGHT - 44, STORY[index][2], 15, ink))
    out.append(
        text(
            24,
            HEIGHT - 20,
            "Room shading and bars: each room's share of the "
            "temperature. Default settings, one tracked person.",
            12.5,
            muted,
        )
    )
    out.append("</svg>")
    return "\n".join(out)


def main() -> int:
    images = []
    history: list[tuple[float, float]] = []
    for t, states, temperature in simulate():
        history.append((t, temperature))
        png = cairosvg.svg2png(bytestring=frame(t, states, history).encode())
        images.append(Image.open(io.BytesIO(png)).convert("RGB"))
    # One palette for every frame, so colors don't shift between frames.
    samples = images[:: max(1, len(images) // 24)]
    sheet = Image.new("RGB", (WIDTH, HEIGHT * len(samples)))
    for i, image in enumerate(samples):
        sheet.paste(image, (0, HEIGHT * i))
    palette = sheet.quantize(colors=256, method=Image.Quantize.MEDIANCUT)
    frames = [image.quantize(palette=palette, dither=Image.Dither.NONE) for image in images]
    durations = [FRAME_MS] * (len(frames) - 1) + [FRAME_MS * HOLD_FRAMES]
    OUT.mkdir(parents=True, exist_ok=True)
    frames[0].save(
        OUT / "oort-demo.gif",
        save_all=True,
        append_images=frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
