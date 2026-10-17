import json
import math
from pathlib import Path

import pytest

from bambu_mcp import catalog, optimize, presets
from bambu_mcp.runner import Metrics, Runner, Variant, parse_result
from bambu_mcp.threemf import Package, apply, rot_matrix

FIXTURE = Path(__file__).parent / "fixtures/pa_pattern.3mf"


def test_rot_matrix_x90_maps_y_to_z():
    p = apply(rot_matrix(90, 0, 0), (0, 1, 0))
    assert p == pytest.approx((0, 0, 1), abs=1e-9)


def test_rotate_object_drops_to_bed_and_keeps_footprint_center(tmp_path):
    pkg = Package(FIXTURE)
    (lo0, hi0) = pkg.world_bbox("2")
    pkg.rotate_object("2", 90, 0, 0)
    lo, hi = pkg.world_bbox("2")
    assert lo[2] == pytest.approx(0, abs=1e-6)
    # a thin slab (h=0.85) stood on edge: new height = old depth
    assert hi[2] == pytest.approx(hi0[1] - lo0[1], abs=1e-6)
    assert (lo[0] + hi[0]) / 2 == pytest.approx((lo0[0] + hi0[0]) / 2, abs=1e-6)
    out = tmp_path / "r.3mf"
    pkg.save(out)
    assert Package(out).world_bbox("2")[1][2] == pytest.approx(hi[2], abs=1e-6)


def test_object_settings_insert_and_replace():
    pkg = Package(FIXTURE)
    pkg.set_object_settings("2", {"wall_loops": "4", "extruder": "2"})
    s = pkg.object_settings()["2"]
    assert s["wall_loops"] == "4" and s["extruder"] == "2" and s["name"] == "Cube"
    pkg.set_object_settings("2", {"wall_loops": "5"})
    assert pkg.object_settings()["2"]["wall_loops"] == "5"
    assert pkg.entries["Metadata/model_settings.config"].decode().count('key="wall_loops"') == 1


def test_flatten_merges_chain_child_wins(tmp_path):
    (tmp_path / "filament").mkdir()
    (tmp_path / "filament/base.json").write_text(json.dumps({"name": "base", "filament_density": ["1.24"], "a": 1}))
    (tmp_path / "filament/kid.json").write_text(json.dumps({"name": "kid", "inherits": "base", "a": 2}))
    idx = presets.index([tmp_path])["filament"]
    flat = presets.flatten(idx["kid"], idx)
    assert flat["a"] == 2 and flat["filament_density"] == ["1.24"] and "inherits" not in flat


def test_flatten_detects_cycle_and_missing_parent(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"name": "a", "inherits": "b"}))
    (tmp_path / "b.json").write_text(json.dumps({"name": "b", "inherits": "a"}))
    idx = {"a": tmp_path / "a.json", "b": tmp_path / "b.json"}
    with pytest.raises(ValueError, match="cycle"):
        presets.flatten(idx["a"], idx)
    with pytest.raises(ValueError, match="not found"):
        presets.flatten(idx["a"], {"a": idx["a"]})


@pytest.mark.parametrize("key,val,out", [("wall_loops", 3, "3"), ("sparse_infill_density", "15", "15%"),
                                         ("enable_support", True, "1"), ("sparse_infill_pattern", "gyroid", "gyroid"),
                                         ("layer_height", 0.2, "0.2")])
def test_validate_ok(key, val, out):
    assert catalog.validate(key, val) == out


@pytest.mark.parametrize("key,val", [("wall_loops", 99), ("sparse_infill_pattern", "nope"), ("made_up", 1),
                                     ("machine_start_gcode", "M104 S300"), ("post_process", "rm -rf /"),
                                     ("wall loops", 1), ("x; --export-stl", 1)])
def test_validate_rejects(key, val):
    with pytest.raises(ValueError):
        catalog.validate(key, val)


def test_validate_force_allows_unknown_but_not_gcode():
    assert catalog.validate("some_new_key", 1, force=True) == "1"
    with pytest.raises(ValueError):
        catalog.validate("machine_end_gcode", "x", force=True)


def test_argv_uses_equals_form_and_dashes():
    r = Runner.__new__(Runner)
    r.binary = "/bin/bs"
    v = Variant(overrides={"sparse_infill_density": "10%", "wall_loops": "2"},
                flags={"orient": 1, "arrange": 1, "allow_rotations": True}, plate=1)
    a = r.argv(v, "in.3mf", Path("out"), ["m.json", "p.json"], ["f.json"], 60)
    assert "--sparse-infill-density=10%" in a and "--wall-loops=2" in a
    assert "--orient=1" in a and "--allow-rotations" in a
    assert a[a.index("--load-settings") + 1] == "m.json;p.json"
    assert a[-1] == "in.3mf" and a[a.index("--slice") + 1] == "1"


def test_parse_result():
    j = {"return_code": 0, "sliced_plates": [
        {"id": 1, "total_predication": 100, "filaments": [{"total_used_g": 2.5}, {"total_used_g": 1}],
         "feature_type_times": {"Sparse infill": 40}, "warning_message": ""},
        {"id": 2, "total_predication": 50, "filaments": [{"total_used_g": 1}], "feature_type_times": {}}]}
    m = parse_result(j)
    assert (m.ok, m.time_s, m.filament_g, m.role_time_s) == (True, 150, 4.5, {"Sparse infill": 40})
    assert parse_result(j, plate=2).time_s == 50
    bad = parse_result({"return_code": 7, "error_string": "boom"})
    assert not bad.ok and "boom" in bad.error


def test_score_and_feasibility():
    o = optimize.Objective(time=0.5, material=0.5, max_time_increase_pct=10)
    base = Metrics(ok=True, time_s=100, filament_g=10)
    better = Metrics(ok=True, time_s=50, filament_g=8)
    slower = Metrics(ok=True, time_s=120, filament_g=5)
    assert optimize.score(base, base, o) == pytest.approx(1.0)
    assert optimize.score(better, base, o) > 1.0
    assert optimize.feasible(better, base, o) and not optimize.feasible(slower, base, o)
    assert math.isinf(optimize.score(Metrics(ok=False), base, o))


def test_candidates_respect_functional_floor_and_locks():
    o = optimize.Objective(functional=True, locked_keys=("sparse_infill_pattern",))
    cands = optimize.candidates("infill", Variant(), FIXTURE, o)
    dens = [c.overrides["sparse_infill_density"] for c in cands if "sparse_infill_density" in c.overrides]
    assert "5%" not in dens and "10%" in dens
    assert not any("sparse_infill_pattern" in c.overrides for c in cands)
    o2 = optimize.Objective(functional=False)
    pats = [c.overrides.get("sparse_infill_pattern") for c in optimize.candidates("infill", Variant(), FIXTURE, o2)]
    assert "lightning" in pats


def test_candidates_skip_disabled_support_and_unchanged_settings():
    effective = {"enable_support": "0", "wall_loops": "2", "top_shell_layers": "3",
                 "bottom_shell_layers": "3"}
    o = optimize.Objective(functional=True)
    assert optimize.candidates("support", Variant(), FIXTURE, o, effective) == []
    shells = optimize.candidates("shells", Variant(), FIXTURE, o, effective)
    assert any(c.overrides.get("bottom_shell_layers") == "2" for c in shells)
    assert not any(c.overrides.get("wall_loops") == "2" or c.overrides.get("top_shell_layers") == "3"
                   for c in shells)
    assert optimize.candidates("support", Variant(), FIXTURE, o, effective,
                               {"2": {"enable_support": "1"}})
    locked = optimize.Objective(functional=True, locked_keys=("bottom_shell_layers",))
    assert not any("bottom_shell_layers" in c.overrides for c in
                   optimize.candidates("shells", Variant(), FIXTURE, locked, effective))
    specified = optimize.Objective(functional=True, specification={"min_bottom_layers": 3,
                                                             "min_top_layers": 3, "min_wall_loops": 2,
                                                             "min_infill_pct": 15})
    assert not any("bottom_shell_layers" in c.overrides for c in
                   optimize.candidates("shells", Variant(), FIXTURE, specified, effective))
    assert not any(c.overrides.get("sparse_infill_density") == "10%" for c in
                   optimize.candidates("infill", Variant(), FIXTURE, specified, effective))


def test_optimizer_searches_bottom_layers_without_disabled_support_slices():
    seen = []

    class FakeRunner:
        def evaluate(self, _source, variant, **_kwargs):
            seen.append(variant)
            faster = variant.overrides.get("bottom_shell_layers") == "2"
            return Metrics(ok=True, time_s=90 if faster else 100, filament_g=10)

    result = optimize.optimize(FakeRunner(), FIXTURE, Variant(), optimize.Objective(),
                               stages=["support", "shells"], max_slices=10)
    assert result.best.overrides.get("bottom_shell_layers") == "2"
    assert any(h["overrides"].get("bottom_shell_layers") == "2" for h in result.history)
    assert all(h["stage"] != "support" for h in result.history)


def test_evaluate_batch_rejects_redundant_change_before_slicing(monkeypatch):
    from bambu_mcp import server
    from bambu_mcp.schemas import VariantSpec

    session = server.Session(FIXTURE, variant=Variant(overrides={"wall_loops": "2"}))
    monkeypatch.setitem(server.SESSIONS, "redundant", session)
    monkeypatch.setattr(server, "runner", lambda: pytest.fail("redundant variant reached the slicer"))
    with pytest.raises(ValueError, match="variant 0 does not change"):
        server.evaluate_batch("redundant", [VariantSpec(overrides={"wall_loops": 2})])


# ---- regressions from ocr review ------------------------------------------------------------------
def test_object_settings_escape_xml():
    pkg = Package(FIXTURE)
    pkg.set_object_settings("2", {"note": 'a"b<c>&d'})
    xml = pkg.entries["Metadata/model_settings.config"].decode()
    import xml.etree.ElementTree as ET
    ET.fromstring(xml)  # still well-formed
    assert pkg.object_settings()["2"]["note"] == 'a"b<c>&d'


def test_index_skips_non_object_json(tmp_path):
    (tmp_path / "filament").mkdir()
    (tmp_path / "filament/list.json").write_text("[1, 2]")
    (tmp_path / "filament/ok.json").write_text(json.dumps({"name": "ok"}))
    assert set(presets.index([tmp_path])["filament"]) == {"ok"}


def test_corrupt_cache_is_a_miss_and_failures_are_not_cached(tmp_path):
    r = Runner.__new__(Runner)
    run = tmp_path / "k"
    run.mkdir()
    (run / "result.json").write_text("{not json")
    assert r._cached(run, "k", 0) is None and not (run / "result.json").exists()
    (run / "result.json").write_text(json.dumps({"return_code": 3, "error_string": "x"}))
    assert r._cached(run, "k", 0) is None                       # failure: re-run, don't cache-hit
    assert r._cached(run, "k", 0, failures=True).ok is False    # but readable right after a run


def test_identical_variants_slice_once(tmp_path, monkeypatch):
    import threading
    import time
    r = Runner.__new__(Runner)
    r.binary, r.home = "/bin/x", tmp_path
    r._index, r._version = {"machine": {}, "process": {}, "filament": {}}, "v1"
    r._locks, r._locks_guard = {}, threading.Lock()
    (tmp_path / "runs").mkdir()
    src = tmp_path / "s.stl"
    src.write_bytes(b"x")
    calls = []

    def fake_run(argv, cwd, timeout, **kw):
        calls.append(1)
        time.sleep(0.05)
        (Path(cwd) / "result.json").write_text(json.dumps({"return_code": 0, "sliced_plates": [
            {"id": 1, "total_predication": 10, "filaments": [{"total_used_g": 1}]}]}))
        import zipfile
        with zipfile.ZipFile(Path(cwd) / 'out.3mf', 'w') as z:
            z.writestr('Metadata/slice_info.config', '<config><plate><metadata key="prediction" value="10"/><metadata key="weight" value="1"/><metadata key="outside" value="false"/></plate></config>')
            z.writestr('Metadata/plate_1.gcode', 'G1 X1 E1')
        return (0, None, "", 0.05)

    monkeypatch.setattr("bambu_mcp.runner.run_bounded", fake_run)
    out = optimize.evaluate_batch(r, src, [Variant() for _ in range(4)], workers=4)
    assert all(m.ok and m.time_s == 10 for m in out) and len(calls) == 1


def test_floors_come_from_catalog():
    assert optimize.floor("sparse_infill_density", True) == 10
    assert optimize.floor("sparse_infill_density", False) == 5
    assert optimize.floor("wall_loops", True) == 2


# ---- regressions from second ocr review -------------------------------------------------------------
def test_flatten_rejects_non_object_preset(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("[1]")
    with pytest.raises(ValueError, match="not a JSON object"):
        presets.flatten(f, {})


def test_index_skips_non_string_names(tmp_path):
    (tmp_path / "process").mkdir()
    (tmp_path / "process/a.json").write_text(json.dumps({"name": ["x"]}))
    (tmp_path / "process/b.json").write_text(json.dumps({"name": 7}))
    (tmp_path / "process/c.json").write_text(json.dumps({"name": "ok"}))
    assert set(presets.index([tmp_path])["process"]) == {"ok"}


def test_rotate_handles_each_instance_independently(tmp_path):
    pkg = Package(FIXTURE)
    xml = pkg.model_xml
    item = next(m.group(0) for m in __import__("re").finditer(r"<item\b[^>]*>", xml))
    moved = item.replace("82.7945612 86.0694634 0.425", "10 20 0.425")
    pkg.entries["3D/3dmodel.model"] = xml.replace(item, item + "\n  " + moved).encode()
    a0, b0 = pkg.world_bbox("2", 0), pkg.world_bbox("2", 1)
    pkg.rotate_object("2", 90, 0, 0)
    a, b = pkg.world_bbox("2", 0), pkg.world_bbox("2", 1)
    for (lo0, hi0), (lo, hi) in ((a0, a), (b0, b)):
        assert (lo[0] + hi[0]) / 2 == pytest.approx((lo0[0] + hi0[0]) / 2, abs=1e-6)  # kept its own xy centre
        assert lo[2] == pytest.approx(0, abs=1e-6)
    assert a[0][0] != pytest.approx(b[0][0])  # instances did not collapse onto each other


# ---- regressions from full-pass ocr review ----------------------------------------------------------
def test_every_catalog_key_is_a_recorded_live_slicer_option():
    """Compatibility snapshot excludes commented-out definitions; no sibling checkout required."""
    snapshot = json.loads((FIXTURE.parent / "slicer_options.json").read_text(encoding="utf-8"))
    assert snapshot["commit"] and len(snapshot["sha256"]) == 64
    assert set(catalog.CATALOG) <= set(snapshot["keys"])


def test_empty_stage_list_runs_no_stages(monkeypatch):
    calls = []
    r = type("R", (), {"evaluate": lambda self, src, v, **kw: calls.append(v) or Metrics(
        ok=True, time_s=10, filament_g=1)})()
    res = optimize.optimize(r, FIXTURE, Variant(), optimize.Objective(), stages=[])
    assert len(calls) == 1 and res.history == []


@pytest.mark.parametrize("mode,val", [("required", "1"), ("forbidden", "0")])
def test_keep_supports_pins_enable_support(mode, val):
    seen = []
    r = type("R", (), {"evaluate": lambda self, src, v, **kw: seen.append(dict(v.overrides)) or Metrics(
        ok=True, time_s=10, filament_g=1)})()
    optimize.optimize(r, FIXTURE, Variant(), optimize.Objective(keep_supports=mode), stages=["support"])
    assert all(o.get("enable_support") == val for o in seen)
    if mode == "forbidden":
        assert len(seen) == 1  # support stage has nothing to try


def test_settings_reset_drops_empty_object_entry():
    from bambu_mcp import server
    s = server.Session(FIXTURE)
    s.variant.object_overrides = {"2": {"wall_loops": "3"}}
    server.SESSIONS["t"] = s
    try:
        out = server.settings_reset("t", keys=None, object_id="2")
    finally:
        server.SESSIONS.pop("t")
    assert out.object_overrides == {} and s.variant.object_overrides == {}


# ---- regressions from full-file ocr scan ------------------------------------------------------------
def test_flatten_rejects_non_string_inherits(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"name": "p", "inherits": ["a"]}))
    with pytest.raises(ValueError, match="inherits must be a string"):
        presets.flatten(f, {})


def test_presets_read_as_utf8(tmp_path):
    (tmp_path / "filament").mkdir()
    (tmp_path / "filament/u.json").write_text(json.dumps({"name": "Bambu PLA – žluté"}, ensure_ascii=False),
                                             encoding="utf-8")
    assert "Bambu PLA – žluté" in presets.index([tmp_path])["filament"]


@pytest.mark.parametrize("val", [2.9, True, "2.9"])
def test_int_settings_reject_non_integral(val):
    with pytest.raises(ValueError):
        catalog.validate("wall_loops", val)
    assert catalog.validate("wall_loops", 3.0) == "3"


def test_object_setting_goes_to_object_level_not_part_level():
    pkg = Package(FIXTURE)
    xml = pkg.entries["Metadata/model_settings.config"].decode()
    # "matrix" exists only inside <part>; setting it on the object must add an object-level entry
    pkg.set_object_settings("2", {"matrix": "X"})
    out = pkg.entries["Metadata/model_settings.config"].decode()
    assert '<metadata key="matrix" value="1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1"/>' in out   # part untouched
    assert pkg.object_settings()["2"]["matrix"] == "X"


def test_object_setting_with_quote_is_replaced_not_duplicated():
    pkg = Package(FIXTURE)
    pkg.set_object_settings("2", {"note": 'say "hi"'})
    pkg.set_object_settings("2", {"note": "plain"})
    out = pkg.entries["Metadata/model_settings.config"].decode()
    assert out.count('key="note"') + out.count("key='note'") == 1
    assert pkg.object_settings()["2"]["note"] == "plain"


def test_preset_content_change_changes_cache_key(tmp_path, monkeypatch):
    import threading
    r = Runner.__new__(Runner)
    r.binary, r.home = "/bin/x", tmp_path
    r._version, r._locks, r._locks_guard = "v1", {}, threading.Lock()
    preset = tmp_path / "proc.json"
    preset.write_text(json.dumps({"name": "p", "layer_height": "0.2"}))
    r._index = {"machine": {}, "process": {}, "filament": {}}
    (tmp_path / "runs").mkdir()
    src = tmp_path / "s.stl"
    src.write_bytes(b"x")
    keys = []
    monkeypatch.setattr(Runner, "_evaluate_locked", lambda self, source, v, key, *a: keys.append(key) or Metrics(ok=True))
    r.evaluate(src, Variant(), process=str(preset))
    preset.write_text(json.dumps({"name": "p", "layer_height": "0.3"}))
    r.evaluate(src, Variant(), process=str(preset))
    assert keys[0] != keys[1]


def test_bad_zip_is_a_failed_metric_not_a_crash(tmp_path):
    import threading
    r = Runner.__new__(Runner)
    r.binary, r.home = "/bin/x", tmp_path
    r._version, r._locks, r._locks_guard = "v1", {}, threading.Lock()
    r._index = {"machine": {}, "process": {}, "filament": {}}
    (tmp_path / "runs").mkdir()
    bad = tmp_path / "bad.3mf"
    bad.write_bytes(b"not a zip")
    m = r.evaluate(bad, Variant(object_rotation={"2": (90, 0, 0)}))
    assert not m.ok and "BadZipFile" in m.error


def test_run_id_path_traversal_refused():
    from bambu_mcp import server
    for bad in ("../../etc", "..", "abc", "/etc/passwd", "g" * 24):
        with pytest.raises(ValueError, match="bad run_id"):
            server._run_dir(bad)


def test_settings_set_empty_object_overrides_creates_no_entry():
    from bambu_mcp import server
    s = server.Session(FIXTURE)
    server.SESSIONS["t2"] = s
    try:
        server.settings_set("t2", overrides={}, object_id="2")
        assert s.variant.object_overrides == {}
        s.variant.overrides = {"wall_loops": "3"}
        server.settings_reset("t2", keys=[])           # [] = drop nothing
        assert s.variant.overrides == {"wall_loops": "3"}
        server.settings_reset("t2")                    # omitted = drop all
        assert s.variant.overrides == {}
    finally:
        server.SESSIONS.pop("t2")


def test_orientation_locks_and_functional_validation():
    o = optimize.Objective(locked_objects=(1,))
    c = optimize.candidates("orientation", Variant(flags={"orient": 1}), FIXTURE, o)
    assert not any(v.flags.get("orient") == 1 and not v.object_rotation for v in c)
    assert all(v.flags.get("orient") == 0 for v in c if v.object_rotation)
    o2 = optimize.Objective(functional=True)
    base = Variant()
    bad = Variant(overrides={"wall_loops": "1", "sparse_infill_pattern": "lightning", "layer_height": "0.4"})
    assert len(optimize.violations(bad, base, FIXTURE, o2)) == 3
    assert not optimize.violations(bad, bad, FIXTURE, o2)  # user's own baseline choice is exempt
    adh = optimize.candidates("adhesion", Variant(), FIXTURE, o2)
    assert not any("brim_type" in c.overrides for c in adh)
    assert "adhesion" not in optimize.DEFAULT_STAGES


def test_optimize_rejects_orient_with_locks_and_combines_after_earlier_win():
    class R:  # fake runner: support_threshold_angle and each infill knob shave time independently
        def evaluate(self, source, v, **kw):
            t = 100.0
            t -= 5 * (v.overrides.get("support_threshold_angle") == "50")
            t -= 5 * (v.overrides.get("sparse_infill_density") == "10%")
            t -= 5 * (v.overrides.get("sparse_infill_pattern") == "adaptivecubic")
            return Metrics(ok=True, time_s=t, filament_g=10, run_id=f"{t}")
    with pytest.raises(ValueError, match="locked_objects"):
        optimize.optimize(R(), FIXTURE, Variant(flags={"orient": 1}), optimize.Objective(locked_objects=(2,)))
    res = optimize.optimize(R(), FIXTURE, Variant(), optimize.Objective(time=1, material=0),
                            stages=["support", "infill"], workers=1)
    assert res.best.overrides.get("sparse_infill_density") == "10%"
    assert res.best.overrides.get("sparse_infill_pattern") == "adaptivecubic"
    assert any(h["stage"] == "infill+combined" for h in res.history)


@pytest.mark.parametrize("kind", ["machine", "process", "filament"])
def test_inspection_reports_missing_presets_and_slice_fails(kind, tmp_path, monkeypatch):
    from bambu_mcp import server
    r = Runner(binary="/bin/true", home=tmp_path)
    r._index = {k: {} for k in presets.KINDS}
    r._version = "test"
    monkeypatch.setattr(server, "runner", lambda: r)
    s = server.Session(FIXTURE)
    if kind == "filament":
        s.filaments = ["missing-preset"]
    else:
        setattr(s, kind, "missing-preset")
    monkeypatch.setitem(server.SESSIONS, "missing-preset-test", s)
    summary = server.project_inspect("missing-preset-test")
    assert len(summary.settings_errors) == 1
    assert summary.settings_errors[0].startswith(f"{kind} preset 'missing-preset':")
    assert summary.effective_settings  # embedded settings are still available, but incomplete
    result = server.slice_plate("missing-preset-test")
    assert not result.ok and "not found" in result.error
    # Clearing the selected preset clears the diagnostic on the next inspection.
    if kind == "filament":
        s.filaments = []
    else:
        setattr(s, kind, None)
    assert server.project_inspect("missing-preset-test").settings_errors == []
