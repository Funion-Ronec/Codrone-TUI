# Occupancy grid and A* route search for the navigator.
# Field frame (docs/DEVELOPMENT.md): cm, origin at the takeoff point, +x forward, +y left, heading clockwise.

from __future__ import annotations

import heapq
import math
from typing import Iterable

from .sensing import CLEAR, HIT

Cell = tuple[int, int]
SQRT2 = math.sqrt(2.0)
HIT_ADD = 2  # confidence a reading adds to the cell it ended in
HIT_MAX = 4  # cap, so a few readings that see through a stale obstacle can still remove it
MAX_EXPANSIONS = 80_000
LOOKAHEAD_CELLS = 80  # how far the path smoother tries to shortcut in one go

_NEIGHBOURS = ((1, 0, 1.0), (-1, 0, 1.0), (0, 1, 1.0), (0, -1, 1.0),
               (1, 1, SQRT2), (1, -1, SQRT2), (-1, 1, SQRT2), (-1, -1, SQRT2))


def _disk(radius_cells: float) -> list[Cell]:
    r = max(0.0, radius_cells)
    n = int(math.floor(r))
    return [(i, j) for i in range(-n, n + 1) for j in range(-n, n + 1) if i * i + j * j <= r * r + 1e-9]


class OccupancyGrid:
    # Square grid centred on the takeoff point. `hits` come from the range sensor, `static` from field.toml.

    def __init__(self, half_cm: float, cell_cm: float = 10.0) -> None:
        self.cell = float(cell_cm)
        self.n = int(math.ceil(half_cm / self.cell))
        self.hits: dict[Cell, int] = {}
        self.static: set[Cell] = set()
        self._zones: dict[tuple[float, float, float, float], tuple[set[Cell], set[Cell]]] = {}

    def clear(self) -> None:
        self.hits.clear()
        self.static = set()
        self._zones.clear()

    def cell_of(self, x: float, y: float) -> Cell:
        return (int(round(x / self.cell)), int(round(y / self.cell)))

    def center(self, c: Cell) -> tuple[float, float]:
        return (c[0] * self.cell, c[1] * self.cell)

    def inside(self, c: Cell) -> bool:
        return abs(c[0]) <= self.n and abs(c[1]) <= self.n

    def box_cells(self, x0: float, y0: float, x1: float, y1: float) -> list[Cell]:
        # every cell the rectangle touches
        c = self.cell
        i0, i1 = math.floor(x0 / c + 0.5), math.floor(x1 / c + 0.5)
        j0, j1 = math.floor(y0 / c + 0.5), math.floor(y1 / c + 0.5)
        return [(i, j) for i in range(i0, i1 + 1) for j in range(j0, j1 + 1)]

    def set_static(self, cells: Iterable[Cell]) -> None:
        new = {c for c in cells if self.inside(c)}
        if new != self.static:
            self.static = new
            self._zones.clear()

    def mark(self, x: float, y: float, heading_deg: float, kind: str, dist_cm: float, max_cm: float,
             cone_deg: float) -> None:
        # One front reading: the beam saw open space up to the hit (or to max range), and something at the hit.
        if kind not in (HIT, CLEAR):
            return
        h = math.radians(heading_deg)
        fx, fy = math.cos(h), -math.sin(h)
        free_to = max(0.0, dist_cm - self.cell) if kind == HIT else max_cm
        here = self.cell_of(x, y)
        changed = False
        seen: set[Cell] = set()
        d, step = 0.0, self.cell / 2.0
        while d <= free_to:
            c = self.cell_of(x + fx * d, y + fy * d)
            if c not in seen:
                seen.add(c)
                k = self.hits.get(c)
                if k is not None:
                    if k <= 1:
                        del self.hits[c]
                    else:
                        self.hits[c] = k - 1
                    changed = True
            d += step
        if kind == HIT:
            # the obstacle can be anywhere across the beam at that distance
            half = math.radians(cone_deg)
            n = max(1, int(math.ceil(2 * half * dist_cm / self.cell)))
            arc = {self.cell_of(x + math.cos(h - half + 2 * half * i / n) * dist_cm,
                                y - math.sin(h - half + 2 * half * i / n) * dist_cm) for i in range(n + 1)}
            for c in arc:
                if c != here and self.inside(c):
                    self.hits[c] = min(HIT_MAX, self.hits.get(c, 0) + HIT_ADD)
                    changed = True
        if changed:
            self._zones.clear()

    def zones(self, hit_hard: float, hit_soft: float, static_hard: float, static_soft: float
              ) -> tuple[set[Cell], set[Cell]]:
        # hard: the drone would touch something; soft: closer than we'd like, allowed at a cost
        key = (hit_hard, hit_soft, static_hard, static_soft)
        cached = self._zones.get(key)
        if cached is not None:
            return cached
        hard: set[Cell] = set()
        soft: set[Cell] = set()
        for cells, r_hard, r_soft in ((list(self.hits), hit_hard, hit_soft), (list(self.static), static_hard, static_soft)):
            if not cells:
                continue
            dh, ds = _disk(r_hard / self.cell), _disk(r_soft / self.cell)
            for i, j in cells:
                hard.update((i + a, j + b) for a, b in dh)
                soft.update((i + a, j + b) for a, b in ds)
        soft -= hard
        self._zones[key] = (hard, soft)
        return hard, soft

    def route(self, start: tuple[float, float], goal: tuple[float, float], *, hit_hard: float, hit_soft: float,
              static_hard: float, static_soft: float, soft_cost: float = 4.0) -> list[tuple[float, float]] | None:
        # Waypoints (cm) from start to goal, the last one exactly the goal. None if there is no way through.
        hard, soft = self.zones(hit_hard, hit_soft, static_hard, static_soft)
        s, g = self.cell_of(*start), self.cell_of(*goal)
        if not self.inside(g) or g in hard:
            return None
        if s == g:
            return [(float(goal[0]), float(goal[1]))]

        def h(c: Cell) -> float:
            dx, dy = abs(c[0] - g[0]), abs(c[1] - g[1])
            return dx + dy + (SQRT2 - 2.0) * min(dx, dy)

        best: dict[Cell, float] = {s: 0.0}
        came: dict[Cell, Cell] = {}
        heap = [(h(s), 0.0, s)]
        expanded = 0
        while heap:
            _f, cost, c = heapq.heappop(heap)
            if c == g:
                break
            if cost > best.get(c, math.inf) + 1e-9:
                continue
            expanded += 1
            if expanded > MAX_EXPANSIONS:
                return None
            for di, dj, step in _NEIGHBOURS:
                nb = (c[0] + di, c[1] + dj)
                if not self.inside(nb) or nb in hard:
                    continue
                if di and dj and ((c[0] + di, c[1]) in hard or (c[0], c[1] + dj) in hard):
                    continue  # no squeezing diagonally past a corner
                nc = cost + step + (soft_cost if nb in soft else 0.0)
                if nc < best.get(nb, math.inf):
                    best[nb] = nc
                    came[nb] = c
                    heapq.heappush(heap, (nc + h(nb), nc, nb))
        else:
            return None

        path = [g]
        while path[-1] != s:
            path.append(came[path[-1]])
        path.reverse()
        points = [self.center(c) for c in self._simplify(path, hard, soft)[1:]]
        points[-1] = (float(goal[0]), float(goal[1]))
        return points

    def _simplify(self, path: list[Cell], hard: set[Cell], soft: set[Cell]) -> list[Cell]:
        # keep only the corners: jump ahead while a straight line stays as clear as the A* path was
        soft_prefix = [0]
        for c in path:
            soft_prefix.append(soft_prefix[-1] + (1 if c in soft else 0))
        out = [path[0]]
        i = 0
        while i < len(path) - 1:
            j = min(len(path) - 1, i + LOOKAHEAD_CELLS)
            while j > i + 1:
                if self._clear_line(path[i], path[j], hard, soft, soft_prefix[j + 1] - soft_prefix[i]):
                    break
                j -= 1
            out.append(path[j])
            i = j
        return out

    @staticmethod
    def _clear_line(a: Cell, b: Cell, hard: set[Cell], soft: set[Cell], soft_budget: int) -> bool:
        steps = max(abs(b[0] - a[0]), abs(b[1] - a[1])) * 3
        if steps == 0:
            return True
        seen: set[Cell] = set()
        soft_n = 0
        for k in range(steps + 1):
            t = k / steps
            c = (int(round(a[0] + (b[0] - a[0]) * t)), int(round(a[1] + (b[1] - a[1]) * t)))
            if c in seen:
                continue
            seen.add(c)
            if c in hard:
                return False
            if c in soft:
                soft_n += 1
                if soft_n > soft_budget:
                    return False
        return True
