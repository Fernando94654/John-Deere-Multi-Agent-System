"""2D frontend: the campaign replayed as a matplotlib animation.

The field on the left, tinted by work zone, with the harvesters (circles) and
the grain carts (squares) driving over it; on the right, live load bars and the
fleet counters.
"""

from __future__ import annotations

from johndeere.simulation import SimulationResult
from johndeere.world.grid import EMPTY, FOOD, OBSTACLE

from .replay import frames

# Terrain stays neutral plus a single hue, which leaves the categorical hues to
# mean "which harvester" rather than "what kind of ground".
TERRAIN_ORDER = [OBSTACLE, EMPTY, FOOD]
TERRAIN_COLORS = ["#6f6d66", "#f0efec", "#1baf7a"]
TERRAIN_INDEX = {value: i for i, value in enumerate(TERRAIN_ORDER)}

# Reference categorical order, minus the greens that would blend into the crop.
HARVESTER_COLORS = ["#2a78d6", "#eb6834", "#eda100", "#e87ba4", "#4a3aa7", "#e34948"]
CART_COLOR = "#26262a"  # carts are told apart by shape and label, not by hue
FARM_COLOR = "#8a5cf0"

INK = "#0b0b0b"
INK_MUTED = "#52514e"
SURFACE = "#fcfcfb"
TRACK = "#e6e5e1"


def harvester_color(index: int) -> str:
    """Color of harvester `index`; ids beyond the palette reuse a hue."""
    return HARVESTER_COLORS[index % len(HARVESTER_COLORS)]


def label_ink(hex_color: str) -> str:
    """Pick black or white for text drawn on top of `hex_color`."""
    r, g, b = (int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return INK if 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.6 else "#ffffff"


def build_animation(result: SimulationResult, interval: int):
    """Assemble the figure and the FuncAnimation that replays the run."""
    import numpy as np
    from matplotlib import pyplot as plt
    from matplotlib.animation import FuncAnimation
    from matplotlib.colors import ListedColormap, to_rgb
    from matplotlib.patches import Patch

    replay = [([row[:] for row in grid], snap) for grid, snap in frames(result)]
    rows = len(result.initial_grid)
    cols = len(result.initial_grid[0])
    farm = (0, 0)
    total_food = sum(row.count(FOOD) for row in result.initial_grid)
    machines = [*result.harvesters, *result.carts]

    fig = plt.figure(
        figsize=(min(17.0, 5.0 + cols * 0.42), min(10.5, max(5.0, rows * 0.46))),
        facecolor=SURFACE,
    )
    spec = fig.add_gridspec(
        2, 2, width_ratios=[3, 1.15], height_ratios=[3, 1.4], wspace=0.14, hspace=0.3
    )
    ax_field = fig.add_subplot(spec[:, 0])
    ax_bars = fig.add_subplot(spec[0, 1])
    ax_info = fig.add_subplot(spec[1, 1])

    # --- the field ---------------------------------------------------------
    display = np.array([[TERRAIN_INDEX[v] for v in row] for row in result.initial_grid])
    image = ax_field.imshow(
        display,
        cmap=ListedColormap(TERRAIN_COLORS),
        vmin=0,
        vmax=len(TERRAIN_COLORS) - 1,
        interpolation="nearest",
    )

    # Work zones as a faint wash, so who owns what is readable at a glance.
    tint = np.zeros((rows, cols, 4))
    for index, zone in enumerate(result.zones):
        red, green, blue = to_rgb(harvester_color(index))
        for row, col in zone:
            tint[row, col] = (red, green, blue, 0.10)
    ax_field.imshow(tint, interpolation="nearest")

    ax_field.set_xticks(np.arange(-0.5, cols, 1), minor=True)
    ax_field.set_yticks(np.arange(-0.5, rows, 1), minor=True)
    ax_field.grid(which="minor", color=SURFACE, linewidth=1.2)
    ax_field.tick_params(which="both", length=0, labelbottom=False, labelleft=False)
    for spine in ax_field.spines.values():
        spine.set_visible(False)

    ax_field.scatter(
        [farm[1]], [farm[0]], s=520, marker="s", color=FARM_COLOR, zorder=2
    )
    ax_field.text(
        farm[1], farm[0], "F", ha="center", va="center", color="#ffffff",
        fontweight="bold", fontsize=10, zorder=5,
    )

    colors = [harvester_color(h.id) for h in result.harvesters]
    colors += [CART_COLOR] * len(result.carts)
    markers = []
    for index, machine in enumerate(machines):
        is_cart = index >= len(result.harvesters)
        dot = ax_field.scatter(
            [], [], s=300 if is_cart else 260,
            marker="s" if is_cart else "o",
            color=colors[index], edgecolors=SURFACE, linewidths=2, zorder=3,
        )
        text = ax_field.text(
            0, 0, machine.label, ha="center", va="center", fontsize=8,
            fontweight="bold", color=label_ink(colors[index]), zorder=4,
        )
        markers.append((dot, text))

    # A stub pointing the way each harvester faces, so the turns on the spot are
    # visible: the spout is always on the left of this arrow.
    noses = [
        ax_field.plot([], [], color=INK, linewidth=2, solid_capstyle="round", zorder=4.5)[0]
        for _ in result.harvesters
    ]

    # The unloading spout: drawn only while a cart is under it, which is what
    # makes "these two are working as a pair, on the move" readable at a glance.
    spouts = [
        ax_field.plot([], [], color=CART_COLOR, linewidth=3.5, zorder=2.5)[0]
        for _ in result.carts
    ]

    title = ax_field.set_title("", color=INK, fontsize=12, pad=10, loc="left")
    ax_field.legend(
        handles=[
            Patch(facecolor=color, edgecolor="#d9d8d3", linewidth=0.8, label=name)
            for color, name in (
                (TERRAIN_COLORS[TERRAIN_INDEX[FOOD]], "crop"),
                (TERRAIN_COLORS[TERRAIN_INDEX[EMPTY]], "harvested"),
                (TERRAIN_COLORS[TERRAIN_INDEX[OBSTACLE]], "obstacle"),
                (FARM_COLOR, "farm"),
                (CART_COLOR, "grain cart"),
            )
        ],
        loc="upper left", bbox_to_anchor=(0, -0.01), ncol=5, frameon=False,
        handlelength=1.2, handleheight=1.2, labelcolor=INK_MUTED, fontsize=9,
    )

    # --- load bars ---------------------------------------------------------
    slots = list(range(len(machines)))
    capacities = [
        (result.snapshots[0].harvesters + result.snapshots[0].carts)[i].capacity
        for i in slots
    ]
    ax_bars.barh(slots, [1.0] * len(slots), color=TRACK, height=0.42)
    bars = ax_bars.barh(slots, [0.0] * len(slots), color=colors, height=0.42)
    bar_labels = [
        ax_bars.text(1.04, i, "", va="center", fontsize=9, color=INK_MUTED)
        for i in slots
    ]
    ax_bars.set_xlim(0, 1.34)
    ax_bars.set_ylim(len(slots) - 0.5, -0.5)
    ax_bars.set_yticks(slots)
    ax_bars.set_yticklabels([m.label for m in machines], color=INK_MUTED, fontsize=9)
    ax_bars.set_xticks([])
    ax_bars.tick_params(length=0)
    ax_bars.set_title("Load", color=INK, fontsize=11, loc="left", pad=8)
    ax_bars.set_facecolor(SURFACE)
    for spine in ax_bars.spines.values():
        spine.set_visible(False)

    # --- counters ----------------------------------------------------------
    ax_info.set_axis_off()
    status = ax_info.text(
        0, 1.0, "", va="top", fontsize=9, color=INK, linespacing=1.55
    )
    ax_info.barh([0.04], [1.0], height=0.09, color=TRACK)
    progress = ax_info.barh([0.04], [0.0], height=0.09, color=INK_MUTED)
    ax_info.set_xlim(0, 1)
    ax_info.set_ylim(0, 1)

    def update(index: int):
        grid, snapshot = replay[index]
        for row in range(rows):
            for col in range(cols):
                display[row][col] = TERRAIN_INDEX[grid[row][col]]
        image.set_data(display)

        views = [*snapshot.harvesters, *snapshot.carts]
        for (dot, text), view in zip(markers, views):
            dot.set_offsets([(view.position[1], view.position[0])])
            text.set_position((view.position[1], view.position[0]))

        for nose, view in zip(noses, snapshot.harvesters):
            row, col = view.position
            dr, dc = view.heading
            nose.set_data([col, col + dc * 0.42], [row, row + dr * 0.42])

        harvester_at = {v.id: v.position for v in snapshot.harvesters}
        for spout, view in zip(spouts, snapshot.carts):
            if view.link is None:
                spout.set_data([], [])
            else:
                row, col = harvester_at[view.link]
                spout.set_data([view.position[1], col], [view.position[0], row])

        for bar, label, view, capacity in zip(bars, bar_labels, views, capacities):
            bar.set_width(view.load / capacity if capacity else 0)
            label.set_text(f"{view.load}/{capacity}")

        m = snapshot.metrics
        title.set_text(
            f"Field {rows}x{cols}   tick {snapshot.tick}/{len(replay)}   "
            f"harvested {m.harvested}/{total_food}"
        )
        status.set_text(
            f"Delivered to farm   {m.delivered}\n"
            f"In transit          {m.in_transit}\n"
            f"Fuel                {m.fuel:.0f} L\n"
            f"CO2                 {m.co2:.0f} kg\n"
            f"Idle                {m.idle_ticks} ticks"
        )
        progress[0].set_width(m.harvested / total_food if total_food else 0)
        return [image, *(d for d, _ in markers), *noses, *spouts, *bars, status]

    animation = FuncAnimation(
        fig, update, frames=len(replay), interval=interval, blit=False, repeat=False
    )
    return fig, animation


def render(
    result: SimulationResult, interval: int = 120, save: str | None = None, fps: int = 8
) -> None:
    """Show the run in a window, or write it to a GIF when `save` is given."""
    from matplotlib import pyplot as plt
    from matplotlib.animation import PillowWriter

    fig, animation = build_animation(result, interval)
    if save:
        animation.save(save, writer=PillowWriter(fps=fps))
        plt.close(fig)
        print(f"Saved {len(result.snapshots)} frames to {save}")
    else:
        plt.show()

    m = result.metrics
    print(
        f"Campaign {'finished' if result.completed else 'INCOMPLETE'} "
        f"in {result.ticks} ticks - delivered {m.delivered} units, "
        f"{m.fuel:.0f} L, {m.co2:.0f} kg CO2"
    )
