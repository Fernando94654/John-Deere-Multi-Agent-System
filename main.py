#!/usr/bin/env python3
"""Entry point for the John Deere multi-agent harvesting simulation.

    python3 main.py --harvesters 3 --carts 2 --seed 42
    python3 main.py --frontend visual --save run.gif
"""

from __future__ import annotations

import argparse
import sys

from johndeere.config import SimulationConfig
from johndeere.simulation import Simulation


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simulate a fleet of harvesters and grain carts working a field."
    )
    parser.add_argument("--rows", type=int, default=16, help="Field rows")
    parser.add_argument("--cols", type=int, default=22, help="Field columns")
    parser.add_argument("--harvesters", type=int, default=3, help="Number of harvesters")
    parser.add_argument("--carts", type=int, default=2, help="Number of grain carts")
    parser.add_argument(
        "--food-ratio",
        type=float,
        default=1.0,
        help="Share of the sowable ground covered in crop, obstacles and the "
        "farmyard excluded; 1 sows everything else (default 1.0)",
    )
    parser.add_argument(
        "--min-obstacles", type=int, default=3, help="Minimum number of obstacles"
    )
    parser.add_argument(
        "--max-obstacles", type=int, default=5, help="Maximum number of obstacles"
    )
    parser.add_argument(
        "--border",
        type=int,
        default=1,
        help="Width of the bare headland around the field, a ring road the "
        "machines can always drive on (default 1, use 0 for none)",
    )
    parser.add_argument("--seed", type=int, default=None, help="Random seed")
    parser.add_argument(
        "--max-ticks", type=int, default=5000, help="Safety limit on the run length"
    )
    parser.add_argument(
        "--frontend",
        choices=("console", "visual"),
        default="console",
        help="How to watch the run (default console)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.20,
        help="Seconds between console frames (default 0.20)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=250,
        help="Milliseconds between frames in the 2D view (default 250)",
    )
    parser.add_argument(
        "--save", metavar="PATH", help="Write the 2D view to a GIF instead of a window"
    )
    parser.add_argument("--fps", type=int, default=4, help="Frame rate when saving")
    parser.add_argument(
        "--no-animation",
        action="store_true",
        help="Console only: skip the replay and print the final frame",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.frontend == "visual" and args.save:
        import matplotlib

        matplotlib.use("Agg")

    config = SimulationConfig(
        rows=args.rows,
        cols=args.cols,
        harvesters=args.harvesters,
        carts=args.carts,
        food_ratio=args.food_ratio,
        min_obstacles=args.min_obstacles,
        max_obstacles=args.max_obstacles,
        border=args.border,
        seed=args.seed,
        max_ticks=args.max_ticks,
    )

    try:
        simulation = Simulation(config)
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1

    result = simulation.run()

    if args.frontend == "console":
        from frontends.console import render

        render(result, delay=args.delay, animate=not args.no_animation)
    else:
        from frontends.visual import render

        render(result, interval=args.interval, save=args.save, fps=args.fps)

    return 0 if result.completed else 2


if __name__ == "__main__":
    raise SystemExit(main())
