#!/usr/bin/env python3
"""Recommend a harvester/grain-cart fleet from field dimensions only."""

from __future__ import annotations

import argparse
import json
import os
import sys

from johndeere.recommendation import FleetCostConfig, recommend_fleet_profiles


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recommend a fleet by evaluating repeated heuristic simulations."
    )
    parser.add_argument("rows", type=int, help="Field rows")
    parser.add_argument("cols", type=int, help="Field columns")
    parser.add_argument("--budget", type=int, required=True, help="operating budget")
    advanced = parser.add_argument_group("advanced evaluation options")
    advanced.add_argument(
        "--seeds",
        type=int,
        default=7,
        help="number of shared terrain seeds, from 5 to 30 (default 7)",
    )
    advanced.add_argument(
        "--base-seed",
        type=int,
        default=42,
        help="seed used to generate the reproducible seed schedule (default 42)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = recommend_fleet_profiles(
            args.rows,
            args.cols,
            budget=args.budget,
            costs=FleetCostConfig.from_mapping(os.environ),
            seed_count=args.seeds,
            base_seed=args.base_seed,
        )
    except ValueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
