"""Needs a Bambu Studio binary (BAMBU_STUDIO_BIN or the default install). Run: pytest -m slow"""
import struct
from pathlib import Path

import pytest

from bambu_mcp.runner import Runner, Variant, find_binary

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def runner(tmp_path_factory):
    try:
        find_binary()
    except FileNotFoundError:
        pytest.skip("no Bambu Studio binary")
    return Runner(home=tmp_path_factory.mktemp("home"))


@pytest.fixture(scope="module")
def cube(tmp_path_factory):
    s = 30.0
    v = [(x * s, y * s, z * s) for x in (0, 1) for y in (0, 1) for z in (0, 1)]
    f = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6),
         (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    p = tmp_path_factory.mktemp("m") / "cube.stl"
    with open(p, "wb") as o:
        o.write(b"\0" * 80 + struct.pack("<I", len(f)))
        for a, b, c in f:
            o.write(struct.pack("<12fH", 0, 0, 0, *v[a], *v[b], *v[c], 0))
    return p


KW = dict(machine="Bambu Lab A1 mini 0.4 nozzle", process="0.20mm Standard @BBL A1M",
          filaments=["Bambu PLA Basic @BBL A1M"])


def test_overrides_move_time_and_grams(runner, cube):
    base = runner.evaluate(cube, Variant(flags={"arrange": 1, "orient": 0}), **KW)
    assert base.ok, base.error
    assert base.filament_g > 0, "grams 0 => preset inherits not flattened"
    lean = runner.evaluate(cube, Variant(overrides={"sparse_infill_density": "5%"},
                                         flags={"arrange": 1, "orient": 0}), **KW)
    assert lean.ok and lean.time_s < base.time_s and lean.filament_g < base.filament_g
    again = runner.evaluate(cube, Variant(overrides={"sparse_infill_density": "5%"},
                                          flags={"arrange": 1, "orient": 0}), **KW)
    assert again.run_id == lean.run_id and again.time_s == lean.time_s  # cache hit


def test_bad_preset_is_reported_not_raised(runner, cube):
    m = runner.evaluate(cube, Variant(), machine="nope", process="nope")
    assert not m.ok and "not found" in m.error
