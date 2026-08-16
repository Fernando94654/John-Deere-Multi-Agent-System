"""Console frontend: the run animated in place in the terminal.

Draws the field as text, repainting each tick over the previous frame with ANSI
cursor codes, and prints a HUD with every machine's state and load plus the
fleet totals. Colors and animation are only used when stdout is a terminal, so
piped output stays plain text.
"""

from __future__ import annotations

import sys
import time

from johndeere.simulation import AgentView, SimulationResult, Snapshot
from johndeere.world.grid import EMPTY, FOOD, Grid, OBSTACLE

from .replay import frames

SYMBOLS = {OBSTACLE: "#", EMPTY: ".", FOOD: "*"}
RESET = "\033[0m"
CELL_COLORS = {OBSTACLE: "\033[31m", EMPTY: "\033[90m", FOOD: "\033[32m"}
HARVESTER_COLOR = "\033[1;93m"
CART_COLOR = "\033[1;96m"
FARM_COLOR = "\033[1;95m"

# Move the cursor up `n` lines, then clear from there to the end of the screen.
CURSOR_UP = "\033[{n}A\033[J"


def paint(text: str, color: str, colored: bool) -> str:
    """Wrap `text` in an ANSI color, or return it untouched when colors are off."""
    return f"{color}{text}{RESET}" if colored else text


def format_field(
    grid: Grid, snapshot: Snapshot, farm: tuple[int, int], colored: bool
) -> str:
    """Render the field with the machines and the farm drawn on top."""
    # Harvesters are digits and carts are letters: digits have no lowercase, so
    # numbering both of them would put two different machines under one symbol.
    overlay: dict[tuple[int, int], tuple[str, str]] = {farm: ("F", FARM_COLOR)}
    for view in snapshot.harvesters:
        overlay[view.position] = (str(view.id % 10), HARVESTER_COLOR)
    for view in snapshot.carts:
        overlay[view.position] = (chr(ord("a") + view.id % 26), CART_COLOR)

    header = "    " + " ".join(str(c % 10) for c in range(len(grid[0])))
    lines = [header]
    for r, row in enumerate(grid):
        cells = []
        for c, value in enumerate(row):
            if (r, c) in overlay:
                text, color = overlay[(r, c)]
                cells.append(paint(text, color, colored))
            else:
                cells.append(paint(SYMBOLS[value], CELL_COLORS[value], colored))
        lines.append(f"{r:>3} " + " ".join(cells))
    return "\n".join(lines)


def format_agent(view: AgentView, width: int = 10) -> str:
    """One HUD line: label, state, position and a load bar."""
    filled = round(width * view.load / view.capacity) if view.capacity else 0
    bar = "#" * filled + "-" * (width - filled)
    position = f"({view.position[0]},{view.position[1]})"
    return (
        f"  {view.label:<3} {view.state:<13} {position:>8}  "
        f"[{bar}] {view.load:>2}/{view.capacity}"
    )


def format_hud(snapshot: Snapshot, total_food: int) -> str:
    """The status block under the field."""
    m = snapshot.metrics
    lines = [
        f"tick {snapshot.tick:>4}   harvested {m.harvested:>4}/{total_food}   "
        f"delivered {m.delivered:>4}   in transit {m.in_transit:>3}",
        *[format_agent(v) for v in snapshot.harvesters],
        *[format_agent(v) for v in snapshot.carts],
        f"  fuel {m.fuel:>7.1f} L   CO2 {m.co2:>7.1f} kg   "
        f"driven {m.distance:>4} cells   idle {m.idle_ticks:>4} ticks",
    ]
    return "\n".join(lines)


def legend(colored: bool) -> str:
    """One-line key of the symbols used by the renderer."""
    return (
        f"{paint('#', CELL_COLORS[OBSTACLE], colored)} obstacle   "
        f"{paint('.', CELL_COLORS[EMPTY], colored)} bare   "
        f"{paint('*', CELL_COLORS[FOOD], colored)} crop   "
        f"{paint('F', FARM_COLOR, colored)} farm   "
        f"{paint('0-9', HARVESTER_COLOR, colored)} harvester   "
        f"{paint('a-z', CART_COLOR, colored)} cart"
    )


def render(result: SimulationResult, delay: float, animate: bool = True) -> None:
    """Replay `result` in the terminal and print the closing report."""
    colored = sys.stdout.isatty()
    animate = animate and colored
    farm = (0, 0)
    total_food = sum(row.count(FOOD) for row in result.initial_grid)
    rows, cols = len(result.initial_grid), len(result.initial_grid[0])

    print(f"Field {rows}x{cols}   {legend(colored)}\n")

    previous_lines = 0
    last_frame = ""
    for grid, snapshot in frames(result):
        last_frame = (
            format_field(grid, snapshot, farm, colored)
            + "\n"
            + format_hud(snapshot, total_food)
        )
        if not animate:
            continue
        if previous_lines:
            sys.stdout.write(CURSOR_UP.format(n=previous_lines))
        sys.stdout.write(last_frame + "\n")
        sys.stdout.flush()
        previous_lines = last_frame.count("\n") + 1
        time.sleep(delay)

    if not animate and last_frame:
        print(last_frame)

    m = result.metrics
    print(
        f"\nCampaign {'finished' if result.completed else 'INCOMPLETE'} "
        f"in {result.ticks} ticks"
    )
    print(f"  delivered to the farm : {m.delivered} units")
    print(f"  crop left unreachable : {result.unreachable_food} cells")
    print(f"  fuel / CO2            : {m.fuel:.1f} L / {m.co2:.1f} kg")
    print(f"  litres per unit       : {m.fuel_per_unit:.2f}")
    print(f"  idle time             : {m.idle_ticks} agent-ticks")
    print(f"  right-of-way stops    : {m.traffic_refusals}")
