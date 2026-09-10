#!/usr/bin/env python3
"""Recommend a harvester / grain-cart fleet from field size and a budget.

    FLEET_COST_CURRENCY=MXN FLEET_COST_VERSION=v1 \\
    FLEET_HARVESTER_COST=100 FLEET_CART_COST=40 \\
    python recommend_fleet.py 16 22 --budget 1000

Reads the unit prices from the environment (same names the web API uses) and
prints the recommendation as JSON.
"""

from __future__ import annotations

import argparse
import json
import sys

from johndeere.fleet_advisor import FleetCosts, recommend_fleet


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("rows", type=int, help="field rows")
    parser.add_argument("cols", type=int, help="field columns")
    parser.add_argument("--budget", type=int, required=True, help="operating budget")
    parser.add_argument("--border", type=int, default=1)
    parser.add_argument("--min-obstacles", type=int, default=3)
    parser.add_argument("--max-obstacles", type=int, default=5)
    parser.add_argument("--food-ratio", type=float, default=1.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = recommend_fleet(
            args.rows,
            args.cols,
            budget=args.budget,
            costs=FleetCosts.from_env(),
            border=args.border,
            min_obstacles=args.min_obstacles,
            max_obstacles=args.max_obstacles,
            food_ratio=args.food_ratio,
        )
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
