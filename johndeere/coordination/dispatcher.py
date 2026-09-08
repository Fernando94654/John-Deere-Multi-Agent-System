"""Who picks up whom: the grain cart auction.

Every harvester that crosses its threshold posts an unload request. Each tick
the free carts bid on the open requests and the cheapest bid wins — a sealed-bid
auction, which is the coordination mechanism the fleet uses instead of a fixed
harvester-to-cart pairing.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import TYPE_CHECKING, Optional

from ..config import Policy
from ..planning.pathfinding import a_star
from ..world.field import Field

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..agents.grain_cart import GrainCart
    from ..agents.harvester import Harvester


@dataclass
class UnloadRequest:
    """A harvester asking to be emptied."""

    harvester_id: int
    posted_tick: int
    served_by: Optional[int] = None

    def waiting(self, tick: int) -> int:
        """Ticks this request has been open."""
        return tick - self.posted_tick


@dataclass
class Dispatcher:
    """Keeps the open requests and runs the auction that assigns carts."""

    requests: dict[int, UnloadRequest] = dataclass_field(default_factory=dict)
    auctions_run: int = 0
    policy: Policy = dataclass_field(default_factory=Policy)

    def post(self, harvester: "Harvester", tick: int) -> None:
        """Register a request, if that harvester has not posted one already."""
        if harvester.id in self.requests:
            return
        self.requests[harvester.id] = UnloadRequest(harvester.id, tick)
        harvester.requested = True

    def close(self, harvester: "Harvester") -> None:
        """Drop the request of a harvester that has been emptied."""
        self.requests.pop(harvester.id, None)
        harvester.requested = False
        harvester.cart_id = None

    def reopen(self, harvester_id: int) -> None:
        """Put a request back on the block: its cart filled up before finishing."""
        request = self.requests.get(harvester_id)
        if request is not None:
            request.served_by = None

    @property
    def pending(self) -> list[UnloadRequest]:
        """Requests still waiting for a cart."""
        return [r for r in self.requests.values() if r.served_by is None]

    def sync(self, carts: list["GrainCart"]) -> int:
        """Reopen requests whose cart has stopped serving them.

        A cart releases itself when it fills up or loses the dock, and it does
        so without going through the dispatcher. The request would otherwise
        stay assigned to a cart that is never coming: it never reappears in
        `pending`, nobody bids on it again, and the harvester waits out the rest
        of the campaign with a full tank. Returns how many were put back.
        """
        reopened = 0
        for request in self.requests.values():
            if request.served_by is None:
                continue
            if carts[request.served_by].target_id != request.harvester_id:
                request.served_by = None
                reopened += 1
        return reopened

    def bid(
        self, field: Field, cart: "GrainCart", harvester: "Harvester", tick: int
    ) -> Optional[float]:
        """What it costs this cart to serve this harvester; `None` if it cannot.

        Distance is the bulk of it, discounted by how long the harvester has
        been waiting so nobody starves, and penalised when the cart does not
        have room for the whole tank and would have to come back. A cart with no
        route over cut ground yet cannot bid at all — it waits for the harvester
        to open one.

        The price is the drive to the **berth**, not to the harvester's own
        cell: that is where the cart is actually going, and a harvester whose
        tank filled up on a cell it could not then cut is standing on standing
        crop — routing to it would fail and leave the machine waiting for a cart
        that no rule allows anyone to send.
        """
        dock = cart.station(field, harvester)
        if dock is None:
            return None
        route = a_star(field, cart.position, dock, avoid_crop=True)
        if route is None:
            return None
        shortfall = max(0, harvester.load - cart.free_capacity)
        waited = self.requests[harvester.id].waiting(tick)
        return len(route) + shortfall * 2 - waited * self.policy.wait_weight

    def run_auctions(
        self,
        field: Field,
        harvesters: dict[int, "Harvester"],
        carts: list["GrainCart"],
        tick: int,
    ) -> None:
        """Assign free carts to open requests, cheapest bid first."""
        open_requests = self.pending
        if not open_requests:
            return
        self.auctions_run += 1

        available = [cart for cart in carts if cart.available]
        for request in sorted(open_requests, key=lambda r: r.posted_tick):
            if not available:
                return
            harvester = harvesters[request.harvester_id]
            bids = [
                (bid, cart.id, cart)
                for cart in available
                if (bid := self.bid(field, cart, harvester, tick)) is not None
            ]
            if not bids:
                continue
            _, _, winner = min(bids, key=lambda item: (item[0], item[1]))
            winner.assign(harvester.id)
            harvester.cart_id = winner.id
            request.served_by = winner.id
            available.remove(winner)
