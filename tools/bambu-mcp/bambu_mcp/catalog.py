"""Curated knobs that move time / material. Keys verified against src/libslic3r/PrintConfig.cpp.

kind: int|float|percent|bool|enum. `floor` is the functional-part minimum used by the optimizer.
"""
import re

CATALOG: dict[str, dict] = {
    "layer_height": dict(kind="float", range=(0.08, 0.32), note="higher = faster, visible layer lines"),
    "initial_layer_print_height": dict(kind="float", range=(0.1, 0.3), note="adhesion; do not raise much"),
    "wall_loops": dict(kind="int", range=(1, 10), floor=2, note="strength vs time/material"),
    "top_shell_layers": dict(kind="int", range=(0, 12), floor=3),
    "bottom_shell_layers": dict(kind="int", range=(0, 12), floor=2),
    "sparse_infill_density": dict(kind="percent", range=(0, 100), floor=10, note="biggest material/time lever"),
    "sparse_infill_pattern": dict(kind="enum", values=["grid", "gyroid", "cubic", "adaptivecubic", "lightning",
                                  "crosshatch", "zig-zag", "line", "triangles", "honeycomb", "tri-hexagon"],
                                  note="lightning = least material, not structural"),
    "infill_combination": dict(kind="bool", note="infill every 2nd layer; needs layer_height <= half nozzle"),
    "enable_support": dict(kind="bool"),
    "support_type": dict(kind="enum", values=["normal(auto)", "tree(auto)", "normal(manual)", "tree(manual)"]),
    "support_style": dict(kind="enum", values=["default", "grid", "snug", "tree_slim", "tree_strong",
                                               "tree_hybrid", "tree_organic"]),
    "support_threshold_angle": dict(kind="int", range=(0, 90), note="higher = fewer supports, more overhang risk"),
    "brim_type": dict(kind="enum", values=["auto_brim", "brim_ears", "outer_only", "inner_only",
                                           "outer_and_inner", "no_brim"]),
    "skirt_loops": dict(kind="int", range=(0, 10)),
    "enable_prime_tower": dict(kind="bool", note="needed for multi-colour on most machines"),
    "prime_tower_width": dict(kind="float", range=(10, 100)),
    "flush_into_infill": dict(kind="bool", note="less purge waste"),
    "flush_into_objects": dict(kind="bool", note="less purge waste, colour bleed into object"),
    "ironing_type": dict(kind="enum", values=["no ironing", "top", "topmost", "solid"], note="adds time"),
    "print_sequence": dict(kind="enum", values=["by layer", "by object"]),
    "seam_position": dict(kind="enum", values=["nearest", "aligned", "back", "random"]),
    "outer_wall_speed": dict(kind="float", note="capped by filament volumetric speed"),
    "inner_wall_speed": dict(kind="float"),
    "sparse_infill_speed": dict(kind="float"),
    "default_acceleration": dict(kind="float"),
}

# Setting these would let a tool call put arbitrary G-code / scripts into printer files.
DENY = ("post_process", "filename_format")
DENY_SUFFIX = ("_gcode",)


def validate(key: str, value, force: bool = False) -> str:
    """Return the CLI string for value, or raise ValueError."""
    if not re.fullmatch(r"[a-z0-9_]+", key):
        raise ValueError(f"bad key {key!r}")
    if key in DENY or key.endswith(DENY_SUFFIX):
        raise ValueError(f"{key} is blocked (code injection risk)")
    spec = CATALOG.get(key)
    if spec is None:
        if force:
            return str(value)
        raise ValueError(f"{key} not in catalog; pass force=true to set it unvalidated")
    k = spec["kind"]
    if k == "bool":
        if value in (True, "1", 1, "true", "True"):
            return "1"
        if value in (False, "0", 0, "false", "False"):
            return "0"
        raise ValueError(f"{key}: expected bool")
    if k == "enum":
        if str(value) not in spec["values"]:
            raise ValueError(f"{key}: {value!r} not in {spec['values']}")
        return str(value)
    if k == "percent":
        v = float(str(value).rstrip("%"))
        out = f"{v:g}%"
    elif k == "int":
        fv = float(value)
        if isinstance(value, bool) or not fv.is_integer():
            raise ValueError(f"{key}: expected an integer, got {value!r}")
        v = int(fv)
        out = str(v)
    else:
        v = float(value)
        out = f"{v:g}"
    lo, hi = spec.get("range", (None, None))
    if lo is not None and not lo <= v <= hi:
        raise ValueError(f"{key}: {v} outside {lo}..{hi}")
    return out
