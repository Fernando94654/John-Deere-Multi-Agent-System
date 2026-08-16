#!/usr/bin/env python3
"""2D visual frontend for the harvesting tractor simulation.

The field is built by `grid_generator.py` and swept by `tractor_simulation.py`;
this script records the run and replays it as a matplotlib animation: the map on
the left, a live per-tractor score panel on the right.

Unlike the other frontends this one needs matplotlib (see requirements.txt).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from grid_generator import EMPTY, FOOD, OBSTACLE, Grid, generate_grid
from tractor_simulation import Cell, SimulationResult, Tractor, plan_paths, simulate

# Terrain keeps to neutrals plus a single hue, which leaves the categorical hues
# free to mean "tractor identity" rather than "kind of ground".
TERRAIN_ORDER = [OBSTACLE, EMPTY, FOOD]
TERRAIN_COLORS = ["#52514e", "#f0efec", "#1baf7a"]
TERRAIN_INDEX = {value: i for i, value in enumerate(TERRAIN_ORDER)}

# Reference categorical order, minus the two green slots that would blend into
# the crop color. Identity never rests on color alone: every marker carries its
# tractor id, which is also what keeps more tractors than colors readable.
TRACTOR_COLORS = ["#2a78d6", "#eb6834", "#eda100", "#e87ba4", "#4a3aa7", "#e34948"]

INK = "#0b0b0b"
INK_MUTED = "#52514e"
SURFACE = "#fcfcfb"
BAND_LINE = "#8a8880"


@dataclass
class Frame:
    """One recorded turn of the run."""

    turn: int
    positions: list[Cell]
    harvested: list[Cell]
    food: list[int]


def tractor_color(tractor_id: int) -> str:
    """Color for a tractor; ids beyond the palette reuse a hue but keep their label."""
    return TRACTOR_COLORS[tractor_id % len(TRACTOR_COLORS)]


def label_ink(hex_color: str) -> str:
    """Pick black or white for text drawn on top of `hex_color`."""
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return INK if luminance > 0.6 else "#ffffff"


def record_run(grid: Grid, n_tractors: int) -> tuple[list[Frame], SimulationResult]:
    """Run the simulation, capturing one `Frame` per turn.

    The algorithm still works on the single grid it is given. The scratch copy
    here is presentation-only: it is how this script tells which cells were
    harvested on which turn, since `simulate` reports totals rather than events.
    """
    scratch = [row[:] for row in grid]
    frames: list[Frame] = []

    def capture(turn: int, tractors: list[Tractor]) -> None:
        harvested: list[Cell] = []
        for tractor in tractors:
            if tractor.position is None:
                continue
            row, col = tractor.position
            if scratch[row][col] == FOOD:
                scratch[row][col] = EMPTY
                harvested.append((row, col))
        frames.append(
            Frame(
                turn=turn,
                positions=[t.position for t in tractors if t.position is not None],
                harvested=harvested,
                food=[t.food for t in tractors],
            )
        )

    result = simulate(grid, n_tractors, on_turn_end=capture)
    return frames, result


def build_animation(
    initial: Grid,
    frames: list[Frame],
    result: SimulationResult,
    total_food: int,
    interval: int,
):
    """Assemble the figure and the FuncAnimation that replays the recorded run."""
    import numpy as np
    from matplotlib import pyplot as plt
    from matplotlib.animation import FuncAnimation
    from matplotlib.colors import ListedColormap
    from matplotlib.patches import Patch

    rows, cols = len(initial), len(initial[0])
    tractors = result.tractors

    figure_width = min(16.0, 4.0 + cols * 0.42)
    figure_height = min(10.0, max(4.5, rows * 0.42))
    fig = plt.figure(figsize=(figure_width, figure_height), facecolor=SURFACE)
    grid_spec = fig.add_gridspec(
        2, 2, width_ratios=[3, 1], height_ratios=[3, 1], wspace=0.12, hspace=0.25
    )
    ax_field = fig.add_subplot(grid_spec[:, 0])
    ax_bars = fig.add_subplot(grid_spec[0, 1])
    ax_info = fig.add_subplot(grid_spec[1, 1])

    # --- the field ---------------------------------------------------------
    display = np.array([[TERRAIN_INDEX[v] for v in row] for row in initial])
    image = ax_field.imshow(
        display,
        cmap=ListedColormap(TERRAIN_COLORS),
        vmin=0,
        vmax=len(TERRAIN_COLORS) - 1,
        interpolation="nearest",
    )
    ax_field.set_xticks(np.arange(-0.5, cols, 1), minor=True)
    ax_field.set_yticks(np.arange(-0.5, rows, 1), minor=True)
    ax_field.grid(which="minor", color=SURFACE, linewidth=1.5)
    ax_field.tick_params(which="both", length=0, labelbottom=False, labelleft=False)
    for spine in ax_field.spines.values():
        spine.set_visible(False)

    # Band boundaries, so each tractor's assigned area is visible.
    for tractor in tractors[1:]:
        first_row, _ = tractor.row_span
        first_col, _ = tractor.column_span
        if first_row > 0:
            ax_field.axhline(first_row - 0.5, color=BAND_LINE, linewidth=1.6)
        if first_col > 0:
            ax_field.axvline(first_col - 0.5, color=BAND_LINE, linewidth=1.6)

    scatter = ax_field.scatter(
        [], [], s=260, zorder=3, edgecolors=SURFACE, linewidths=2
    )
    markers = [
        ax_field.text(
            0,
            0,
            "",
            ha="center",
            va="center",
            fontsize=9,
            fontweight="bold",
            zorder=4,
            color=label_ink(tractor_color(t.id)),
        )
        for t in tractors
    ]
    field_title = ax_field.set_title("", color=INK, fontsize=12, pad=10, loc="left")

    # A key for the terrain, so the three ground states never rest on color alone.
    ax_field.legend(
        handles=[
            Patch(facecolor=color, edgecolor="#d9d8d3", linewidth=0.8, label=label)
            for color, label in (
                (TERRAIN_COLORS[TERRAIN_INDEX[FOOD]], "crop"),
                (TERRAIN_COLORS[TERRAIN_INDEX[EMPTY]], "harvested"),
                (TERRAIN_COLORS[TERRAIN_INDEX[OBSTACLE]], "obstacle"),
            )
        ],
        loc="upper left",
        bbox_to_anchor=(0, -0.01),
        ncol=3,
        frameon=False,
        handlelength=1.2,
        handleheight=1.2,
        labelcolor=INK_MUTED,
        fontsize=9,
    )

    # --- the score panel ---------------------------------------------------
    ids = list(range(len(tractors)))
    colors = [tractor_color(i) for i in ids]
    bars = ax_bars.barh(ids, [0] * len(ids), color=colors, height=0.34)
    bar_labels = [
        ax_bars.text(0, i, "", va="center", ha="left", fontsize=9, color=INK_MUTED)
        for i in ids
    ]
    headroom = max((t.food for t in tractors), default=1) * 1.25 or 1
    ax_bars.set_xlim(0, headroom)
    ax_bars.set_ylim(len(ids) - 0.5, -0.5)
    ax_bars.set_yticks(ids)
    ax_bars.set_yticklabels([f"T{i}" for i in ids], color=INK_MUTED, fontsize=9)
    ax_bars.set_xticks([])
    ax_bars.tick_params(length=0)
    ax_bars.set_title("Food collected", color=INK, fontsize=11, loc="left", pad=8)
    ax_bars.set_facecolor(SURFACE)
    for spine in ax_bars.spines.values():
        spine.set_visible(False)

    # --- the status block --------------------------------------------------
    ax_info.set_axis_off()
    status = ax_info.text(
        0, 0.9, "", va="top", ha="left", fontsize=10, color=INK, linespacing=1.6
    )
    progress_track = ax_info.barh([0.12], [1.0], height=0.14, color="#e6e5e1")
    progress_bar = ax_info.barh([0.12], [0.0], height=0.14, color=INK_MUTED)
    ax_info.set_xlim(0, 1)
    ax_info.set_ylim(0, 1)

    total_turns = len(frames)

    def update(index: int):
        frame = frames[index]
        for row, col in frame.harvested:
            display[row][col] = TERRAIN_INDEX[EMPTY]
        image.set_data(display)

        scatter.set_offsets([(col, row) for row, col in frame.positions])
        scatter.set_facecolor(colors[: len(frame.positions)])
        for tractor, marker, (row, col) in zip(tractors, markers, frame.positions):
            marker.set_position((col, row))
            marker.set_text(str(tractor.id))

        harvested = sum(frame.food)
        for bar, label, value in zip(bars, bar_labels, frame.food):
            bar.set_width(value)
            label.set_position((value + headroom * 0.03, bar.get_y() + bar.get_height() / 2))
            label.set_text(str(value))

        field_title.set_text(f"Field {rows}x{cols}   turn {frame.turn}/{total_turns}")
        status.set_text(
            f"Turn {frame.turn} of {total_turns}\n"
            f"Harvested {harvested} of {total_food}"
        )
        progress_bar[0].set_width(frame.turn / total_turns)
        return [image, scatter, *markers, *bars, *bar_labels, status]

    animation = FuncAnimation(
        fig, update, frames=total_turns, interval=interval, blit=False, repeat=False
    )
    return fig, animation


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Animate n harvesting tractors over a 2D field."
    )
    parser.add_argument("--rows", type=int, default=10, help="Grid rows")
    parser.add_argument("--cols", type=int, default=14, help="Grid columns")
    parser.add_argument("--tractors", type=int, default=3, help="Number of tractors")
    parser.add_argument(
        "--food-ratio",
        type=float,
        default=0.8,
        help="Share of the whole grid covered in food (default 0.8)",
    )
    parser.add_argument(
        "--min-obstacles", type=int, default=3, help="Minimum number of obstacles"
    )
    parser.add_argument(
        "--max-obstacles", type=int, default=5, help="Maximum number of obstacles"
    )
    parser.add_argument("--seed", type=int, default=None, help="Random seed")
    parser.add_argument(
        "--interval", type=int, default=200, help="Milliseconds between frames"
    )
    parser.add_argument(
        "--save",
        metavar="PATH",
        help="Write the run to a GIF instead of opening a window",
    )
    parser.add_argument("--fps", type=int, default=6, help="Frame rate when saving")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.save:
        import matplotlib

        matplotlib.use("Agg")

    try:
        plan_paths(args.rows, args.cols, args.tractors)
        grid = generate_grid(
            args.rows,
            args.cols,
            food_ratio=args.food_ratio,
            min_obstacles=args.min_obstacles,
            max_obstacles=args.max_obstacles,
            seed=args.seed,
        )
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    initial = [row[:] for row in grid]
    total_food = sum(row.count(FOOD) for row in grid)
    frames, result = record_run(grid, args.tractors)

    from matplotlib import pyplot as plt
    from matplotlib.animation import PillowWriter

    fig, animation = build_animation(
        initial, frames, result, total_food, args.interval
    )

    if args.save:
        animation.save(args.save, writer=PillowWriter(fps=args.fps))
        plt.close(fig)
        print(f"Saved {len(frames)} frames to {args.save}")
    else:
        plt.show()

    print(f"Total food harvested: {result.total_food}/{total_food}")
    print(f"Turns taken: {result.turns}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
