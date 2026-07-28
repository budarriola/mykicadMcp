"""Tests for the Phase 7.8 GPU acceleration tier (`kicad_router_accel`).

HARDWARE HONESTY - read this before trusting a green run. The machine these
tests were written on HAS a CUDA GPU (`nvidia-smi` reports a GTX 1650) but no
CUDA array module installed: `cupy` and `torch` are OPTIONAL, commented-out
dependencies per the plan's dependency policy, so nothing here has executed on
real device memory. What IS proven:

  * the tier's `xp`-parameterized kernel is exercised end to end through the
    SAME code path a device would take (`fine_wavefront(..., xp=...)`), using a
    numpy-backed stand-in - so the host/device split, the transfers, and the
    backtrace hand-off are all real code under test, and parity against the cpu
    A* and the numpy tier is asserted on that path;
  * the memory planner, batch tiling, per-item demotion, and the OOM retry
    ladder are exercised against a SIMULATED device that raises a real
    out-of-memory error on a chosen allocation;
  * the probe degrades correctly on a box with no importable array module, and
    still reports a GPU it can see via `nvidia-smi` (which is genuinely
    executed here when present).

What is NOT proven: that cupy's own kernels reproduce these fields on silicon.
That needs `pip install cupy` on a CUDA box and a re-run of the parity tests
with `acceleration: "gpu"`; until then the tier ships behind an explicit opt-in
and demotes to numpy/cpu everywhere else.
"""

from __future__ import annotations

import numpy as np
import pytest

import kicad_router_accel as accel
import kicad_router_tool as rt


# --------------------------------------------------------------------------- #
# A stand-in array module. `_PassthroughXp` is numpy pretending to be a device
# (so the `xp is not np` transfer branches actually run); `_OomXp` additionally
# fails a chosen allocation with a realistically-named exception.
# --------------------------------------------------------------------------- #

class _CudaOutOfMemoryError(RuntimeError):
    """Named the way cupy names its OOM, since `_is_oom` matches structurally."""


class _PassthroughXp:
    """Every numpy attribute, plus the device transfer helpers (`asarray` in,
    `asnumpy` out) the tier uses to move fields on and off a device."""

    def __init__(self) -> None:
        self.allocations = 0

    def __getattr__(self, name):
        attr = getattr(np, name)
        if name in ("full", "full_like", "zeros", "ones", "asarray") and callable(attr):
            def counted(*a, **k):
                self.allocations += 1
                self._maybe_fail()
                return attr(*a, **k)
            return counted
        return attr

    def _maybe_fail(self) -> None:
        return None

    @staticmethod
    def asnumpy(a):
        return np.asarray(a)


class _OomXp(_PassthroughXp):
    def __init__(self, fail_at: int) -> None:
        super().__init__()
        self.fail_at = fail_at

    def _maybe_fail(self) -> None:
        if self.allocations >= self.fail_at:
            raise _CudaOutOfMemoryError("Out of memory allocating device array")


_LAYERS = ["F.Cu", "B.Cu"]
_LAYER_TYPES = {"F.Cu": "signal", "B.Cu": "signal"}


def _window_with_wall():
    """A small window with a one-layer wall, so the search has a real decision
    to make (route round it on F.Cu, or via down to B.Cu and straight across)."""
    win = rt._FineWindow(-1.0, -3.0, 11.0, 5.0, 0.25, _LAYERS, _LAYER_TYPES, "NET")
    obstacles = [rt._Obst("seg", "WALL", frozenset(["F.Cu"]), 0.3, 5.0, -3.0, 5.0, 2.0)]
    win.build(obstacles, 0.1, 0.3, 0.2, 0.2)
    return win


def _search(win, xp=None):
    s = win.nearest_free(0.0, 0.0, _LAYERS, max_ring=max(win.cols, win.rows))
    g = win.nearest_free(10.0, 0.0, _LAYERS, max_ring=max(win.cols, win.rows))
    args = (win, "signal", rt._Weights({}, 1.0), {}, {}, s, _LAYERS, g, set(_LAYERS),
            None, None)
    if xp is None:
        return rt._fine_astar(*args)
    return accel.fine_wavefront(*args, xp=xp)


# ---------------------------- parity (the gate) ---------------------------- #

def test_gpu_kernel_path_matches_cpu_and_numpy_byte_for_byte():
    """The parity gate the tier ships behind: the device code path (transfers
    included) must return the SAME path as the pure-Python A* reference and the
    numpy tier - identical geometry, not merely identical cost."""
    win = _window_with_wall()
    cpu_path = _search(win)
    numpy_path = _search(win, xp=np)
    device_path = _search(win, xp=_PassthroughXp())
    assert cpu_path is not None, "fixture must route for parity to mean anything"
    assert cpu_path == numpy_path
    assert cpu_path == device_path


def test_device_transfer_branches_actually_run():
    """Guard against the parity test above passing vacuously because `xp is np`
    short-circuited every transfer: the stand-in must really have allocated."""
    xp = _PassthroughXp()
    _search(_window_with_wall(), xp=xp)
    assert xp.allocations > 0


def test_gpu_parity_holds_on_a_lazy_whole_board_window():
    """The two M5 pieces compose: the device kernel over a LAZY window (whose
    blocked sets are computed on demand) still matches the cpu A*."""
    win = rt._FineWindow(-1.0, -3.0, 11.0, 5.0, 0.25, _LAYERS, _LAYER_TYPES,
                         "NET", lazy=True)
    win.build([rt._Obst("seg", "WALL", frozenset(["F.Cu"]), 0.3, 5.0, -3.0, 5.0, 2.0)],
              0.1, 0.3, 0.2, 0.2)
    assert _search(win) == _search(win, xp=_PassthroughXp())


# ------------------------- probe / graceful absence ------------------------ #

def test_probe_gpu_never_raises_and_reports_a_reason_when_unusable():
    info = accel.probe_gpu()
    assert set(("available", "module", "free_mb", "source")) <= set(info)
    if not info["available"]:
        assert info["reason"], "an unavailable GPU must say WHY"
    # If nvidia-smi saw a device, the probe must have reported its name/VRAM
    # even though no array module is importable - that is the useful diagnostic.
    if info["source"] == "nvidia-smi":
        assert info["name"] and info["total_mb"] > 0


def test_gpu_array_module_raises_when_unavailable():
    if accel.probe_gpu().get("available"):
        pytest.skip("a real CUDA array module is installed on this box")
    with pytest.raises(accel.GpuUnavailable):
        accel.gpu_array_module()


def test_missing_gpu_demotes_every_item_instead_of_erroring():
    """A missing GPU is EXPECTED and must be a graceful demotion, not an error."""
    if accel.probe_gpu().get("available"):
        pytest.skip("a real CUDA array module is installed on this box")
    items = [{"rows": 4, "cols": 4, "layers": 2} for _ in range(3)]
    results, report = accel.run_windows(
        items, {}, gpu_call=lambda it, xp: "gpu", fallback_call=lambda it: "cpu")
    assert results == ["cpu", "cpu", "cpu"]
    assert report["demoted_no_device"] == 3
    assert report["on_gpu"] == 0
    assert report["reason"]


def test_router_backend_gpu_still_routes_without_a_device():
    """End to end through the router's own dispatch: asking for the gpu backend
    on a box without one must still produce the cpu/numpy answer."""
    win = _window_with_wall()
    s = win.nearest_free(0.0, 0.0, _LAYERS, max_ring=max(win.cols, win.rows))
    g = win.nearest_free(10.0, 0.0, _LAYERS, max_ring=max(win.cols, win.rows))
    args = (win, "signal", rt._Weights({}, 1.0), {}, {}, s, _LAYERS, g, set(_LAYERS),
            None, None)
    rt.gpu_tier_report(reset=True)
    assert rt._fine_search("gpu", *args) == rt._fine_search("cpu", *args)


def test_resolve_backend_accepts_gpu_only_explicitly():
    assert rt._resolve_backend({"autorouter": {"acceleration": "gpu"}}) == "gpu"
    assert rt._resolve_backend({"autorouter": {"acceleration": "auto"}}) == "cpu"
    assert rt._resolve_backend({"autorouter": {"acceleration": "hybrid"}}) == "cpu"


# ------------------------ memory planner / batching ------------------------ #

def test_footprint_estimate_scales_with_window_size():
    small = accel.estimate_window_device_bytes(100, 100, 2)
    big = accel.estimate_window_device_bytes(200, 200, 2)
    assert big == 4 * small
    assert accel.estimate_window_device_bytes(100, 100, 4) == 2 * small


def test_plan_batches_tiles_to_the_budget_and_preserves_order():
    sizes = [10, 10, 10, 10, 10]
    batches, oversized = accel.plan_batches(sizes, budget_bytes=25)
    assert oversized == []
    assert batches == [[0, 1], [2, 3], [4]]
    # flattening must reproduce the input order: batching never reorders work.
    assert [i for b in batches for i in b] == list(range(5))


def test_plan_batches_marks_single_oversized_items():
    batches, oversized = accel.plan_batches([10, 500, 10], budget_bytes=25)
    assert oversized == [1]
    assert [i for b in batches for i in b] == [0, 2]


def test_plan_batches_with_no_budget_declares_everything_oversized():
    batches, oversized = accel.plan_batches([1, 2, 3], budget_bytes=0)
    assert batches == []
    assert oversized == [0, 1, 2]


def test_explicit_batch_cap_is_honoured():
    settings = {"autorouter": {"gpu": {"batch": 2}}}
    assert accel.resolve_batch_limit(settings) == 2
    assert accel.resolve_batch_limit({"autorouter": {"gpu": {"batch": "auto"}}}) is None
    batches, _ = accel.plan_batches([1] * 5, budget_bytes=1000, max_batch=2)
    assert [len(b) for b in batches] == [2, 2, 1]


def test_explicit_memory_budget_overrides_the_probe():
    settings = {"autorouter": {"gpu": {"memory_budget_mb": 64}}}
    assert accel.gpu_memory_budget_bytes(settings) == 64 * 1024 * 1024


def test_auto_budget_reserves_headroom_from_probed_free_vram():
    probe = {"free_mb": 2000}
    budget = accel.gpu_memory_budget_bytes({}, probe=probe)
    assert 0 < budget < 2000 * 1024 * 1024, "auto budget must not claim all free VRAM"


# ---------------------------- OOM fallback gates --------------------------- #

def _fake_available_probe():
    return {"available": True, "module": "cupy", "name": "SimulatedDevice",
            "free_mb": 4096, "total_mb": 4096, "source": "cupy", "reason": None}


def test_forced_tiny_vram_demotes_cleanly_rather_than_crashing(monkeypatch):
    """The plan's OOM-fallback ACCEPTANCE GATE: a forced-tiny-VRAM run completes
    via demotion, not a crash. 1 MB cannot hold any of these windows, so every
    item bottoms out of tiling and drops to the numpy/cpu tier."""
    monkeypatch.setattr(accel, "gpu_array_module", lambda probe=None: _PassthroughXp())
    settings = {"autorouter": {"gpu": {"memory_budget_mb": 1, "oom_fallback": True}}}
    items = [{"rows": 300, "cols": 300, "layers": 4} for _ in range(4)]
    results, report = accel.run_windows(
        items, settings, gpu_call=lambda it, xp: "gpu", fallback_call=lambda it: "cpu",
        probe=_fake_available_probe())
    assert results == ["cpu"] * 4
    assert report["demoted_oversized"] == 4
    assert report["on_gpu"] == 0


def test_generous_vram_keeps_everything_on_the_device(monkeypatch):
    """Control for the test above: with a real budget nothing is demoted, so the
    demotion counts above are measuring the budget and not a broken tier."""
    monkeypatch.setattr(accel, "gpu_array_module", lambda probe=None: _PassthroughXp())
    settings = {"autorouter": {"gpu": {"memory_budget_mb": 4096}}}
    items = [{"rows": 50, "cols": 50, "layers": 2} for _ in range(4)]
    results, report = accel.run_windows(
        items, settings, gpu_call=lambda it, xp: "gpu", fallback_call=lambda it: "cpu",
        probe=_fake_available_probe())
    assert results == ["gpu"] * 4
    assert report["demoted_oversized"] == 0 and report["demoted_oom"] == 0


def test_runtime_oom_retries_at_half_batch_then_demotes_the_item(monkeypatch):
    """A runtime allocator OOM (fragmentation, another app taking VRAM mid-run)
    is caught the same way: retry the batch halved, and only the item that still
    cannot run is demoted - the rest stay on the device."""
    monkeypatch.setattr(accel, "gpu_array_module", lambda probe=None: _PassthroughXp())
    settings = {"autorouter": {"gpu": {"memory_budget_mb": 4096}}}
    items = [{"rows": 10, "cols": 10, "layers": 2, "id": i} for i in range(4)]

    def gpu_call(it, xp):
        if it["id"] == 2:
            raise _CudaOutOfMemoryError("Out of memory allocating 1 GiB")
        return f"gpu{it['id']}"

    results, report = accel.run_windows(
        items, settings, gpu_call=gpu_call, fallback_call=lambda it: f"cpu{it['id']}",
        probe=_fake_available_probe())
    assert results == ["gpu0", "gpu1", "cpu2", "gpu3"]
    assert report["demoted_oom"] == 1
    assert report["on_gpu"] == 3


def test_oom_fallback_disabled_raises_instead_of_demoting(monkeypatch):
    """`oom_fallback: false` is a debugging/benchmarking choice and must fail
    loudly rather than silently changing where the work ran."""
    monkeypatch.setattr(accel, "gpu_array_module", lambda probe=None: _PassthroughXp())
    settings = {"autorouter": {"gpu": {"memory_budget_mb": 1, "oom_fallback": False}}}
    items = [{"rows": 300, "cols": 300, "layers": 4}]
    with pytest.raises(accel.GpuOutOfMemory):
        accel.run_windows(items, settings, gpu_call=lambda it, xp: "gpu",
                          fallback_call=lambda it: "cpu", probe=_fake_available_probe())


def test_real_kernel_survives_a_simulated_mid_search_oom(monkeypatch):
    """The OOM path with the REAL wavefront rather than a stub: the device run
    dies partway through allocating its fields, and the demoted numpy result is
    still the correct, parity-identical path."""
    monkeypatch.setattr(accel, "gpu_array_module",
                        lambda probe=None: _OomXp(fail_at=3))
    win = _window_with_wall()
    s = win.nearest_free(0.0, 0.0, _LAYERS, max_ring=max(win.cols, win.rows))
    g = win.nearest_free(10.0, 0.0, _LAYERS, max_ring=max(win.cols, win.rows))
    args = (win, "signal", rt._Weights({}, 1.0), {}, {}, s, _LAYERS, g, set(_LAYERS),
            None, None)
    items = [{"rows": win.rows, "cols": win.cols, "layers": len(win.layers)}]
    results, report = accel.run_windows(
        items, {"autorouter": {"gpu": {"memory_budget_mb": 4096}}},
        gpu_call=lambda it, xp: accel.fine_wavefront(*args, xp=xp),
        fallback_call=lambda it: accel.fine_wavefront(*args),
        probe=_fake_available_probe())
    assert report["demoted_oom"] == 1
    assert results[0] == rt._fine_astar(*args)


def test_is_oom_does_not_swallow_real_bugs():
    assert accel._is_oom(_CudaOutOfMemoryError("Out of memory"))
    assert accel._is_oom(MemoryError())
    assert accel._is_oom(accel.GpuOutOfMemory("x"))
    assert not accel._is_oom(ValueError("bad shape"))
    assert not accel._is_oom(KeyError("missing"))


# ------------------------------ system probe ------------------------------- #

def test_probe_system_resources_reads_this_machine():
    info = accel.probe_system_resources()
    assert info["cpu_count"] >= 1
    assert "gpu" in info
    if info["ram_source"] != "unavailable":
        assert info["ram_total_mb"] > 0
        assert 0 < info["ram_free_mb"] <= info["ram_total_mb"]
