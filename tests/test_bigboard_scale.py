"""Big-board MEMORY-budget acceptance for the M5 acceleration work.

kiln (~1.6k segments, 4 layers, ~180 x 60 mm) is the SMALL end of what this
router is designed for, so neither the whole-board lazy window nor the GPU
memory planner can be accepted on kiln alone - the plan requires synthetic
stress boards at 10x and 100x kiln scale, with MEMORY budgets, since no real
board in this repo exercises big-board behaviour.

Deliberately NOT benchmarked: per the standing user directive, runtime
speed-ups are not measured here. These tests assert the things that are
correctness-visible at scale instead:

  * the GPU memory planner refuses to over-commit VRAM at 10x/100x scale, tiles
    to the budget, and bottoms out into per-item demotion rather than an OOM
    crash (the plan's stated OOM acceptance gate, applied at scale);
  * the whole-board LAZY window's build cost is a function of OBSTACLE COUNT,
    not window area - the property the whole M5 window lift rests on - shown by
    building the same obstacle set into windows spanning 1x, 10x and 100x the
    area and observing the work done stay flat, while the eager window's own
    node budget is exceeded many times over.
"""

from __future__ import annotations

import kicad_router_accel as accel
import kicad_router_tool as rt


# kiln, roughly: 180 x 60 mm, 4 copper layers, 0.2 mm detailed grid.
_KILN_W_MM, _KILN_H_MM = 180.0, 60.0
_KILN_LAYERS = 4
_GRID_MM = 0.2


def _window_shape(width_mm: float, height_mm: float, grid: float, layers: int):
    """(rows, cols, layers) a whole-board window of this size would have."""
    cols = max(2, int(width_mm / grid) + 1)
    rows = max(2, int(height_mm / grid) + 1)
    return rows, cols, layers


def _scaled(area_scale: float, layers: int = _KILN_LAYERS):
    """A board `area_scale` times kiln's AREA (so linear dimensions grow by its
    square root), at kiln's grid."""
    k = area_scale ** 0.5
    return _window_shape(_KILN_W_MM * k, _KILN_H_MM * k, _GRID_MM, layers)


# ------------------------- GPU memory budget at scale ---------------------- #

def test_kiln_scale_window_fits_a_modest_vram_budget():
    """Sanity anchor for the two scale tests below: at 1x, a whole-board
    wavefront is small enough that a 2 GB budget (this box's actual free VRAM
    class) holds it comfortably - so a demotion at 10x/100x is really about
    scale, not a broken estimate."""
    rows, cols, layers = _scaled(1.0)
    need = accel.estimate_window_device_bytes(rows, cols, layers)
    budget = 2048 * 1024 * 1024
    assert need < budget
    batches, oversized = accel.plan_batches([need], budget)
    assert oversized == [] and batches == [[0]]


def test_ten_x_kiln_whole_board_window_is_planned_not_over_committed(monkeypatch):
    """10x kiln area on 8 layers: the planner must either fit it or declare it
    oversized - never silently dispatch something it cannot hold."""
    monkeypatch.setattr(accel, "gpu_array_module", lambda probe=None: object())
    rows, cols, layers = _scaled(10.0, layers=8)
    need = accel.estimate_window_device_bytes(rows, cols, layers)
    budget = 2048 * 1024 * 1024
    batches, oversized = accel.plan_batches([need], budget)
    assert (oversized == [0]) != (batches == [[0]]), "must be exactly one or the other"
    if oversized:
        # Oversized is the honest answer, and the executor must DEMOTE it.
        results, report = accel.run_windows(
            [{"rows": rows, "cols": cols, "layers": layers}],
            {"autorouter": {"gpu": {"memory_budget_mb": 2048}}},
            gpu_call=lambda it, xp: "gpu", fallback_call=lambda it: "cpu",
            probe={"available": True, "module": "cupy", "name": "Sim",
                   "free_mb": 2048, "total_mb": 4096, "source": "cupy", "reason": None})
        assert results == ["cpu"] and report["demoted_oversized"] == 1


def test_hundred_x_kiln_window_demotes_cleanly_instead_of_crashing(monkeypatch):
    """100x kiln area, 8 layers: far beyond any consumer VRAM. The acceptance
    gate is that the run COMPLETES via demotion rather than crashing."""
    rows, cols, layers = _scaled(100.0, layers=8)
    need = accel.estimate_window_device_bytes(rows, cols, layers)
    assert need > 8 * 1024 ** 3, "100x kiln should exceed any consumer VRAM"
    monkeypatch.setattr(accel, "gpu_array_module", lambda probe=None: object())
    results, report = accel.run_windows(
        [{"rows": rows, "cols": cols, "layers": layers}],
        {"autorouter": {"gpu": {"memory_budget_mb": 2048}}},
        gpu_call=lambda it, xp: "gpu", fallback_call=lambda it: "cpu",
        probe={"available": True, "module": "cupy", "name": "Sim", "free_mb": 2048,
               "total_mb": 4096, "source": "cupy", "reason": None})
    assert results == ["cpu"]
    assert report["demoted_oversized"] == 1
    assert report["demoted_oom"] == 0


def test_many_mid_size_windows_stream_through_a_small_budget():
    """Batches TILE: a hundred 10x-kiln-ish windows against a budget that holds
    only a few at a time must be split into many batches, never one - windows
    stream through the budget in chunks rather than all-at-once."""
    rows, cols, layers = _scaled(1.0)
    need = accel.estimate_window_device_bytes(rows, cols, layers)
    budget = need * 3
    batches, oversized = accel.plan_batches([need] * 100, budget)
    assert oversized == []
    assert len(batches) >= 33
    assert all(len(b) <= 3 for b in batches)
    assert [i for b in batches for i in b] == list(range(100))


# ------------------- lazy window build cost vs window AREA ----------------- #

def _wall_obstacles(n: int, span_mm: float):
    """`n` short track segments spread across a `span_mm` square."""
    step = span_mm / max(1, n)
    return [rt._Obst("seg", "OTHER", frozenset(["F.Cu", "B.Cu"]), 0.25,
                     i * step, 0.0, i * step, 2.0) for i in range(n)]


def _lazy_build_work(width_mm: float, height_mm: float, obstacles):
    """Build a lazy window of this size and report how much indexing work it
    did (total obstacle-bucket insertions) - the build's real cost driver."""
    win = rt._FineWindow(0.0, 0.0, width_mm, height_mm, _GRID_MM,
                         ["F.Cu", "B.Cu"], {"F.Cu": "signal", "B.Cu": "signal"},
                         "TARGET", lazy=True)
    win.build(obstacles, 0.1, 0.3, 0.2, 0.2)
    assert win._index is not None
    return win, sum(len(v) for v in win._index.buckets.values())


def test_lazy_build_cost_tracks_obstacles_not_window_area():
    """THE load-bearing scale property of the M5 window lift: growing the window
    to 10x and 100x the AREA, with the same obstacles, does not grow the build.
    (The eager path's cost is O(window area / grid^2) instead, which is exactly
    why `_MAX_WINDOW_NODES` had to exist.)"""
    obstacles = _wall_obstacles(200, _KILN_W_MM)
    _w1, work_1x = _lazy_build_work(_KILN_W_MM, _KILN_H_MM, obstacles)
    k10, k100 = 10.0 ** 0.5, 100.0 ** 0.5
    _w10, work_10x = _lazy_build_work(_KILN_W_MM * k10, _KILN_H_MM * k10, obstacles)
    _w100, work_100x = _lazy_build_work(_KILN_W_MM * k100, _KILN_H_MM * k100, obstacles)
    assert work_1x == work_10x == work_100x, (work_1x, work_10x, work_100x)
    assert work_1x > 0


def test_hundred_x_kiln_lazy_window_far_exceeds_the_eager_node_budget():
    """The window these tests build would be refused outright by the eager
    budget - by two orders of magnitude - yet the lazy one builds fine and
    answers cell queries. That gap IS the lift."""
    k = 100.0 ** 0.5
    win, _work = _lazy_build_work(_KILN_W_MM * k, _KILN_H_MM * k,
                                  _wall_obstacles(200, _KILN_W_MM))
    nodes = win.cols * win.rows * len(win.layers)
    assert nodes > 100 * rt._MAX_WINDOW_NODES
    # and it still answers, without ever materializing those nodes
    assert isinstance(win.cell_of(5.0, 1.0) in win.blocked_track["F.Cu"], bool)
    assert len(win.blocked_track["F.Cu"]._cache) == 1


def test_choose_grid_coarsens_a_100x_board_into_the_lazy_budget():
    """A board too big even for the lazy budget must COARSEN (deterministically)
    rather than be refused - the same adaptive-grid rule, one budget up."""
    k = 100.0 ** 0.5
    grid = rt._choose_grid(_KILN_W_MM * k, _KILN_H_MM * k, 8, _GRID_MM, 1.0,
                           rt._MAX_LAZY_WINDOW_NODES)
    assert grid > _GRID_MM, "a 100x board must coarsen past the base grid"
    assert grid <= 1.0
    assert grid == rt._choose_grid(_KILN_W_MM * k, _KILN_H_MM * k, 8, _GRID_MM, 1.0,
                                   rt._MAX_LAZY_WINDOW_NODES), "must be deterministic"
