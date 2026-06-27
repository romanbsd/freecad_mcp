"""Versioned declarative feature patterns.

Patterns expand to ordinary component graph nodes before FreeCAD objects are
created, so callers can inspect and patch the resulting graph.
"""


def _required(params, *names):
    missing = [name for name in names if params.get(name) is None]
    if missing:
        raise ValueError("pattern parameters missing: %s" % ", ".join(missing))


def _stud_grid(feature):
    p = dict(feature.get("parameters") or {})
    _required(p, "count_x", "count_y", "pitch", "diameter", "height")
    prefix = feature["id"]
    origin = p.get("origin") or {"x": "0 mm", "y": "0 mm", "z": "0 mm"}
    return [
        {
            "id": prefix + "__seed", "type": "cylinder",
            "radius": "(%s) / 2" % p["diameter"], "height": p["height"],
            "position": origin, "role": "construction",
        },
        {
            "id": prefix, "type": "grid_array", "base": prefix + "__seed",
            "count_x": p["count_x"], "count_y": p["count_y"],
            "spacing_x": p["pitch"], "spacing_y": p["pitch"],
            "role": feature.get("role", "output"),
        },
    ]


def _brick_underside(feature):
    p = dict(feature.get("parameters") or {})
    _required(
        p, "base", "width", "length", "wall", "depth", "tube_od",
        "tube_id", "pitch", "count_x", "count_y",
    )
    prefix = feature["id"]
    output = p.get("output", prefix)
    tube_x = p.get("tube_origin_x", "(%s) - (%s) / 2" % (p["pitch"], p["pitch"]))
    tube_y = p.get("tube_origin_y", tube_x)
    rib_t = p.get("rib_thickness", "0.8 mm")
    rib_z = p.get("rib_z", "1.9 mm")
    rib_h = p.get("rib_height", "(%s) - (%s)" % (p["depth"], rib_z))
    hollow = prefix + "__hollow"
    tubes = prefix + "__tubes"
    nodes = [
        {
            "id": prefix + "__void", "type": "box", "role": "tool",
            "size": {
                "x": "(%s) - 2 * (%s)" % (p["width"], p["wall"]),
                "y": "(%s) - 2 * (%s)" % (p["length"], p["wall"]),
                "z": p["depth"],
            },
            "position": {"x": p["wall"], "y": p["wall"], "z": "0 mm"},
        },
        {
            "id": hollow, "type": "cut", "base": p["base"],
            "tool": prefix + "__void", "role": "construction",
        },
        {
            "id": prefix + "__tube_seed", "type": "tube",
            "outer_radius": "(%s) / 2" % p["tube_od"],
            "inner_radius": "(%s) / 2" % p["tube_id"],
            "height": p["depth"],
            "position": {"x": tube_x, "y": tube_y, "z": "0 mm"},
            "role": "construction",
        },
        {
            "id": tubes, "type": "grid_array",
            "base": prefix + "__tube_seed",
            "count_x": int(p["count_x"]) - 1,
            "count_y": int(p["count_y"]) - 1,
            "spacing_x": p["pitch"], "spacing_y": p["pitch"],
            "role": "construction",
        },
        {
            "id": prefix + "__with_tubes", "type": "union",
            "base": hollow, "tool": tubes, "role": "construction",
        },
        {
            "id": prefix + "__h_rib_seed", "type": "box",
            "size": {
                "x": "(%s) - 2 * (%s)" % (p["width"], p["wall"]),
                "y": rib_t, "z": rib_h,
            },
            "position": {
                "x": p["wall"], "y": "(%s) - (%s) / 2" % (tube_y, rib_t),
                "z": rib_z,
            },
            "role": "construction",
        },
        {
            "id": prefix + "__h_ribs", "type": "grid_array",
            "base": prefix + "__h_rib_seed", "count_x": 1,
            "count_y": int(p["count_y"]) - 1,
            "spacing_x": p["pitch"], "spacing_y": p["pitch"],
            "role": "construction",
        },
        {
            "id": prefix + "__with_h_ribs", "type": "union",
            "base": prefix + "__with_tubes", "tool": prefix + "__h_ribs",
            "role": "construction",
        },
        {
            "id": prefix + "__v_rib_seed", "type": "box",
            "size": {
                "x": rib_t,
                "y": "(%s) - 2 * (%s)" % (p["length"], p["wall"]),
                "z": rib_h,
            },
            "position": {
                "x": "(%s) - (%s) / 2" % (tube_x, rib_t),
                "y": p["wall"], "z": rib_z,
            },
            "role": "construction",
        },
        {
            "id": prefix + "__v_ribs", "type": "grid_array",
            "base": prefix + "__v_rib_seed",
            "count_x": int(p["count_x"]) - 1, "count_y": 1,
            "spacing_x": p["pitch"], "spacing_y": p["pitch"],
            "role": "construction",
        },
        {
            "id": output, "type": "union",
            "base": prefix + "__with_h_ribs", "tool": prefix + "__v_ribs",
            "role": feature.get("role", "output"),
            "tags": list(feature.get("tags") or []) + ["lego_underside"],
        },
    ]
    return nodes


PATTERNS = {
    "lego.stud_grid": _stud_grid,
    "lego.brick_underside": _brick_underside,
}


def expand_patterns(features):
    expanded = []
    for feature in features:
        if feature.get("type") != "pattern":
            expanded.append(dict(feature))
            continue
        name = feature.get("pattern")
        if feature.get("version", "1") != "1":
            raise ValueError("unsupported pattern version for %s" % name)
        handler = PATTERNS.get(name)
        if handler is None:
            raise ValueError("unknown pattern %r" % name)
        expanded.extend(handler(feature))
    return expanded


def list_patterns():
    return [{"name": name, "version": "1"} for name in sorted(PATTERNS)]
