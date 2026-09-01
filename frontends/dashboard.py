"""The architecture diagram, alive: it lights up as each part does its work.

The boxes and arrows are the ones from the project brief. Every tick the engine
reports a `TickActivity`, and the panel shows what acted:

- each box has **its own colour**, so which part woke up reads at a glance;
- a **dot travels down an arrow** whenever information flows along it;
- a caption spells out, in words, what just happened this tick.

A lit box fades over a few ticks instead of snapping off — at four frames a
second a one-tick flash is invisible.
"""

from __future__ import annotations

from matplotlib.patches import FancyBboxPatch

from johndeere.activity import TickActivity

# Each box gets its own hue so the viewer can tell them apart instantly. The
# name is written inside every box too: colour is never the only cue.
COLORS = {
    "data": "#2a78d6",
    "engine": "#4a3aa7",
    "assignment": "#eb6834",
    "agents": "#1baf7a",
    "logistics": "#eda100",
    "telemetry": "#e87ba4",
}

# Where each box sits, in axes coordinates: (x, y, width, height). The margins
# keep borders and labels clear of the panel edges.
LAYOUT = {
    "data": (0.035, 0.66, 0.21, 0.30),
    "engine": (0.395, 0.66, 0.21, 0.30),
    "assignment": (0.755, 0.66, 0.21, 0.30),
    "agents": (0.10, 0.29, 0.24, 0.27),
    "logistics": (0.66, 0.29, 0.24, 0.27),
    "telemetry": (0.36, 0.00, 0.28, 0.20),
}

TITLES = {
    "data": "Field &\nfleet data",
    "engine": "Coordination\nengine",
    "assignment": "Task\nassignment",
    "agents": "Harvester\nagents",
    "logistics": "Grain carts\n& logistics",
    "telemetry": "Telemetry\n(feedback)",
}

# Counters shown under each box, as (attribute, short label).
READOUTS = {
    "data": [("requests_posted", "req")],
    "engine": [("bids_evaluated", "bids")],
    "assignment": [("assignments", "pairs")],
    "agents": [("cuts", "cut")],
    "logistics": [("transfers", "loads"), ("deliveries", "drops")],
    "telemetry": [],
}

LINKS = [
    ("data", "engine", "votes", False),
    ("engine", "assignment", "coalitions", False),
    ("engine", "agents", "", False),
    ("engine", "logistics", "", False),
    ("agents", "telemetry", "", False),
    ("logistics", "telemetry", "", False),
    ("telemetry", "engine", "feedback", True),
]

FADE = 3  # ticks a box stays lit after it last did something
DOT_SPEED = 0.34  # how far along its arrow a travelling dot moves per tick


class ArchitecturePanel:
    """Draws the diagram once, then only changes colours as the run goes."""

    def __init__(self, axis, palette):
        self.axis = axis
        self.dim, self.wash = palette["muted"], palette["wash"]
        self.surface = palette["surface"]
        self.ink, self.faint = palette["ink"], palette["faint"]

        self.heat = {box: 0 for box in LAYOUT}
        self.totals = {box: {name: 0 for name, _ in READOUTS[box]} for box in LAYOUT}
        self.phase = {(s, t): 0.0 for s, t, _, _ in LINKS}

        axis.set_xlim(0, 1)
        axis.set_ylim(-0.14, 1)
        axis.set_axis_off()

        self.ends = {}
        self.arrows, self.dots = {}, {}
        for source, target, label, dashed in LINKS:
            self.arrows[(source, target)] = self._draw_arrow(
                source, target, label, dashed
            )
            self.dots[(source, target)] = axis.plot(
                [], [], marker="o", markersize=7, linestyle="none",
                color=COLORS[source], zorder=5,
            )[0]

        self.boxes, self.labels, self.readouts = {}, {}, {}
        for box, (x, y, width, height) in LAYOUT.items():
            self.boxes[box] = axis.add_patch(
                FancyBboxPatch(
                    (x, y), width, height,
                    boxstyle="round,pad=0.004,rounding_size=0.03",
                    facecolor=self.wash, edgecolor=self.dim, linewidth=1.2, zorder=2,
                )
            )
            self.labels[box] = axis.text(
                x + width / 2, y + height * (0.66 if READOUTS[box] else 0.5),
                TITLES[box], ha="center", va="center", fontsize=8,
                color=self.faint, zorder=3, clip_on=False,
            )
            self.readouts[box] = axis.text(
                x + width / 2, y + height * 0.22, "",
                ha="center", va="center", fontsize=7, color=self.faint,
                family="monospace", zorder=3, clip_on=False,
            )

        self.caption = axis.text(
            0.5, -0.1, "", ha="center", va="center", fontsize=9,
            color=self.faint, zorder=3, clip_on=False,
        )

    def _draw_arrow(self, source, target, label, dashed):
        """One connector between two boxes, plus its label."""
        start, end = self._anchor(source, target)
        self.ends[(source, target)] = (start, end)
        arrow = self.axis.annotate(
            "", xy=end, xytext=start,
            arrowprops=dict(
                arrowstyle="-|>", color=self.dim, linewidth=1.2,
                linestyle=(0, (3, 3)) if dashed else "solid",
                shrinkA=2, shrinkB=2,
            ),
            zorder=1,
        )
        text = None
        if label:
            horizontal = abs(start[1] - end[1]) < 0.05
            text = self.axis.text(
                (start[0] + end[0]) / 2,
                (start[1] + end[1]) / 2 + (0.06 if horizontal else 0.0),
                label, ha="center" if horizontal else "left",
                va="bottom" if horizontal else "center",
                fontsize=7.5, color=self.faint, zorder=4, clip_on=False,
                bbox=dict(facecolor=self.surface, edgecolor="none", pad=1.0),
            )
        return arrow, text

    @staticmethod
    def _anchor(source, target):
        """Pick the two box edges an arrow should run between."""
        sx, sy, sw, sh = LAYOUT[source]
        tx, ty, tw, th = LAYOUT[target]
        source_centre = (sx + sw / 2, sy + sh / 2)
        target_centre = (tx + tw / 2, ty + th / 2)
        if abs(source_centre[1] - target_centre[1]) < 0.05:  # side by side
            forward = target_centre[0] > source_centre[0]
            return (
                (sx + sw if forward else sx, source_centre[1]),
                (tx if forward else tx + tw, target_centre[1]),
            )
        down = target_centre[1] < source_centre[1]
        return (
            (source_centre[0], sy if down else sy + sh),
            (target_centre[0], ty + th if down else ty),
        )

    @staticmethod
    def _story(activity: TickActivity) -> str:
        """One line of plain words describing what happened this tick."""
        beats = []
        if activity.requests_posted:
            beats.append(f"{activity.requests_posted} calling for a cart")
        if activity.bids_evaluated:
            beats.append(f"{activity.bids_evaluated} bids")
        if activity.assignments:
            beats.append(f"{activity.assignments} cart assigned")
        if activity.cuts:
            beats.append(f"{activity.cuts} cut")
        if activity.rotations:
            beats.append(f"{activity.rotations} turning to unload")
        if activity.transfers:
            beats.append(f"{activity.transfers} taking grain aboard")
        if activity.deliveries:
            beats.append(f"{activity.deliveries} delivered to the farm")
        if activity.give_ways:
            beats.append(f"{activity.give_ways} giving way")
        return "   ·   ".join(beats) if beats else "driving"

    def update(self, activity: TickActivity) -> list:
        """Light up whatever acted this tick and dim whatever did not."""
        for box in LAYOUT:
            self.heat[box] = FADE if activity.hot(box) else max(0, self.heat[box] - 1)
            for name, _ in READOUTS[box]:
                self.totals[box][name] += getattr(activity, name)

            lit = self.heat[box] > 0
            fresh = self.heat[box] == FADE
            colour = COLORS[box]
            patch = self.boxes[box]
            # A box acting right now is filled with its own colour; one that is
            # fading keeps the coloured border but loses the fill, so the eye
            # follows the change instead of a wall of colour.
            patch.set_facecolor(
                colour if fresh else (self.surface if lit else self.wash)
            )
            patch.set_edgecolor(colour if lit else self.dim)
            patch.set_linewidth(2.4 if fresh else (1.6 if lit else 1.2))
            self.labels[box].set_color(
                "#ffffff" if fresh else (self.ink if lit else self.faint)
            )
            self.labels[box].set_fontweight("bold" if fresh else "normal")
            self.readouts[box].set_text(
                "  ".join(
                    f"{self.totals[box][name]} {short}" for name, short in READOUTS[box]
                )
            )
            self.readouts[box].set_color(
                "#ffffff" if fresh else (colour if lit else self.faint)
            )

        for key, (arrow, text) in self.arrows.items():
            flowing = activity.flowing(*key)
            colour = COLORS[key[0]]
            arrow.arrow_patch.set_color(colour if flowing else self.dim)
            arrow.arrow_patch.set_linewidth(2.4 if flowing else 1.2)
            if text is not None:
                text.set_color(colour if flowing else self.faint)

            dot = self.dots[key]
            if flowing:
                # The dot slides from one box towards the next, so the direction
                # of the flow is shown, not just implied by the arrowhead.
                self.phase[key] = (self.phase[key] + DOT_SPEED) % 1.0
                (x0, y0), (x1, y1) = self.ends[key]
                step = 0.15 + 0.7 * self.phase[key]
                dot.set_data([x0 + (x1 - x0) * step], [y0 + (y1 - y0) * step])
                dot.set_color(colour)
            else:
                self.phase[key] = 0.0
                dot.set_data([], [])

        self.caption.set_text(self._story(activity))
        return [
            *self.boxes.values(), *self.labels.values(), *self.readouts.values(),
            *self.dots.values(), self.caption,
        ]
