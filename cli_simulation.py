#!/usr/bin/env python3
"""CLI and console presentation for the harvesting tractor simulation.

The field is built by `grid_generator.py` and swept by `tractor_simulation.py`;
this script only wires them together, renders the grid and animates the run
turn by turn.
"""

from __future__ import annotations

import argparse
import string
import sys
import time

from grid_generator import EMPTY, FOOD, OBSTACLE, Grid, generate_grid
from tractor_simulation import Tractor, plan_paths, simulate

SYMBOLS = {OBSTACLE: "#", EMPTY: ".", FOOD: "*"}
TRACTOR_ALPHABET = string.digits + string.ascii_uppercase

RESET = "\033[0m"
CELL_COLORS = {OBSTACLE: "\033[31m", EMPTY: "\033[90m", FOOD: "\033[32m"}
TRACTOR_COLORS = [
    "\033[1;93m",
    "\033[1;96m",
    "\033[1;95m",
    "\033[1;94m",
    "\033[1;91m",
    "\033[1;97m",
]

# ANSI: move the cursor up `n` lines, then clear from there to the end of screen.
CURSOR_UP = "\033[{n}A\033[J"


def tractor_symbol(tractor_id: int) -> str:
    """One-character label for a tractor: 0-9 then A-Z, so columns stay aligned."""
    return TRACTOR_ALPHABET[tractor_id % len(TRACTOR_ALPHABET)]


def paint(text: str, color: str, colored: bool) -> str:
    """Wrap `text` in an ANSI color, or return it untouched when colors are off."""
    return f"{color}{text}{RESET}" if colored else text


def format_grid(
    grid: Grid, tractors: list[Tractor] | None = None, colored: bool = False
) -> str:
    """Render the grid, optionally overlaying the tractors at their positions.

    `#` is an obstacle, `.` empty ground and `*` food. The overlay only reads
    `tractor.position`; it never modifies the grid.
    """
    overlay: dict[tuple[int, int], int] = {}
    if tractors:
        overlay = {t.position: t.id for t in tractors if t.position is not None}

    header = "    " + " ".join(str(c % 10) for c in range(len(grid[0])))
    lines = [header]
    for r, row in enumerate(grid):
        cells = []
        for c, value in enumerate(row):
            if (r, c) in overlay:
                tractor_id = overlay[(r, c)]
                color = TRACTOR_COLORS[tractor_id % len(TRACTOR_COLORS)]
                cells.append(paint(tractor_symbol(tractor_id), color, colored))
            else:
                cells.append(paint(SYMBOLS[value], CELL_COLORS[value], colored))
        lines.append(f"{r:>3} " + " ".join(cells))
    return "\n".join(lines)


def format_assignment(tractors: list[Tractor]) -> str:
    """Table listing the band, the border entry point and the area per tractor."""
    header = (
        f"{'Tractor':>8} {'Start':>9} {'Rows':>8} {'Columns':>9} "
        f"{'Area':>6} {'Food':>7}"
    )
    lines = [header]
    for tractor in tractors:
        row, col = tractor.start_cell
        first_row, last_row = tractor.row_span
        first_col, last_col = tractor.column_span
        lines.append(
            f"{tractor_symbol(tractor.id):>8} {f'({row},{col})':>9} "
            f"{f'{first_row}-{last_row}':>8} {f'{first_col}-{last_col}':>9} "
            f"{tractor.area:>6} {tractor.food:>7}"
        )
    return "\n".join(lines)


def render_frame(
    grid: Grid,
    tractors: list[Tractor],
    turn: int,
    total_turns: int,
    total_food: int,
    colored: bool,
) -> str:
    """Build one animation frame: status line, grid with tractors, live scores."""
    harvested = sum(t.food for t in tractors)
    status = (
        f"Turn {turn:>4}/{total_turns}    harvested {harvested:>4}/{total_food} food"
    )
    scores = "    ".join(
        f"{tractor_symbol(t.id)}: {t.food:>3}" for t in tractors
    )
    return "\n".join([status, format_grid(grid, tractors, colored), scores])


def legend(colored: bool) -> str:
    """One-line key of the symbols used by the renderer."""
    return (
        f"{paint('#', CELL_COLORS[OBSTACLE], colored)} obstacle   "
        f"{paint('.', CELL_COLORS[EMPTY], colored)} empty   "
        f"{paint('*', CELL_COLORS[FOOD], colored)} food   "
        f"{paint('0-9/A-Z', TRACTOR_COLORS[0], colored)} tractor"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simulate n harvesting tractors over a grid of variable size."
    )
    parser.add_argument("--rows", type=int, default=10, help="Grid rows")
    parser.add_argument("--cols", type=int, default=10, help="Grid columns")
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
        "--delay",
        type=float,
        default=0.15,
        help="Seconds between animation frames (default 0.15)",
    )
    parser.add_argument(
        "--no-animation",
        action="store_true",
        help="Skip the turn-by-turn animation and print only the summary",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    interactive = sys.stdout.isatty()
    colored = interactive

    try:
        paths = plan_paths(args.rows, args.cols, args.tractors)
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

    total_food = sum(row.count(FOOD) for row in grid)
    total_turns = max(len(path) for path in paths)

    print(f"Initial map ({args.rows}x{args.cols})   {legend(colored)}")
    print(format_grid(grid, colored=colored))
    print()

    animated = interactive and not args.no_animation
    previous_lines = 0

    def draw(turn: int, tractors: list[Tractor]) -> None:
        """Repaint the current frame in place, on top of the previous one."""
        nonlocal previous_lines
        frame = render_frame(
            grid, tractors, turn, total_turns, total_food, colored
        )
        if previous_lines:
            sys.stdout.write(CURSOR_UP.format(n=previous_lines))
        sys.stdout.write(frame + "\n")
        sys.stdout.flush()
        previous_lines = frame.count("\n") + 1
        time.sleep(args.delay)

    result = simulate(grid, args.tractors, on_turn_end=draw if animated else None)

    if animated:
        print()
    print("Final map (all food harvested)")
    print(format_grid(grid, colored=colored))

    print("\nArea assignment")
    print(format_assignment(result.tractors))

    print(f"\nTotal food harvested: {result.total_food}/{total_food}")
    print(f"Turns taken: {result.turns}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
