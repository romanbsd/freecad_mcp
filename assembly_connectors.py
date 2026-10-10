"""Editable connector recipes built from existing native graph features.

All dimensions are millimeters. Recipes are read-only; the caller previews or
applies them through the ordinary transactional component patch API.
"""
import copy
import math
import re


def _length(value, name, positive=True):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(name + " must be a finite millimeter number")
    if value > 1e6:
        raise ValueError(name + " exceeds the one-million-millimeter recipe limit")
    if value < 0 or (positive and value == 0):
        raise ValueError(name + " must be " + ("positive" if positive else "nonnegative"))
    return float(value)


def connector_pair(prefix, male_base, female_base, position, axis="z",
                   kind="pin_socket", diameter=3, length=4, clearance=.2,
                   embed=1, bottom_clearance=.2, lead_in=.3, width=6,
                   thickness=1.2, male_material=None, female_material=None):
    """Generate a round locating pin/socket or rectangular glue tongue/recess.

    position is the entry plane, positive axis points into the receiving part.
    clearance is PER SIDE, not diametral. No snap/press-fit guarantee is implied.
    Base parts must already exist in the same component coordinate frame.
    Rectangular glue ledges use width/thickness and do not apply lead_in.
    """
    for name, value in (("prefix", prefix), ("male_base", male_base), ("female_base", female_base)):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_]\w*", value):
            raise ValueError(name + " must be a feature identifier")
    if male_base == female_base:
        raise ValueError("connector bases must be different parts")
    if axis not in ("x", "y", "z") or kind not in ("pin_socket", "glue_ledge"):
        raise ValueError("unsupported connector axis or kind")
    if not isinstance(position, (list, tuple)) or len(position) != 3:
        raise ValueError("position must contain three finite millimeter numbers")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in position):
        raise ValueError("position must contain three finite millimeter numbers")
    if any(abs(v) > 1e6 for v in position):
        raise ValueError("position exceeds the recipe coordinate limit")
    values = {n: _length(v, n, n not in ("clearance", "bottom_clearance", "lead_in"))
              for n, v in (("diameter", diameter), ("length", length), ("clearance", clearance),
                           ("embed", embed), ("bottom_clearance", bottom_clearance),
                           ("lead_in", lead_in), ("width", width), ("thickness", thickness))}
    diameter, length, clearance, embed = (values[n] for n in ("diameter", "length", "clearance", "embed"))
    bottom_clearance, lead_in, width, thickness = (values[n] for n in ("bottom_clearance", "lead_in", "width", "thickness"))
    if kind == "pin_socket" and (lead_in >= diameter / 2 or lead_in >= length):
        raise ValueError("lead_in must be less than pin radius and insertion length")
    epsilon = .01  # Extend socket just outside its mouth for a clean boolean.
    index = "xyz".index(axis)

    def point(offset):
        p = list(position)
        p[index] += offset
        return dict(zip("xyz", p))

    def cylinder(suffix, radius, height, offset):
        return {"id": prefix + suffix, "type": "cylinder", "radius": radius,
                "height": height, "axis": axis, "position": point(offset), "role": "tool"}

    features = []
    if kind == "pin_socket":
        pin = cylinder("_Pin", diameter / 2, length + embed - lead_in, -embed)
        features.append(pin)
        pin_id = pin["id"]
        if lead_in:
            tip = {"id": prefix + "_Tip", "type": "cone", "radius1": diameter / 2,
                   "radius2": diameter / 2 - lead_in, "height": lead_in,
                   "axis": axis, "position": point(length - lead_in), "role": "tool"}
            features.append(tip)
            features.append({"id": prefix + "_PinTool", "type": "union",
                             "shapes": [pin_id, tip["id"]], "role": "tool"})
            pin_id = prefix + "_PinTool"
        socket = cylinder("_Socket", diameter / 2 + clearance,
                          length + bottom_clearance + epsilon, -epsilon)
        features.append(socket)
        socket_id = socket["id"]
        if lead_in:
            mouth = {"id": prefix + "_Mouth", "type": "cone",
                     "radius1": diameter / 2 + clearance + lead_in,
                     "radius2": diameter / 2 + clearance, "height": lead_in + epsilon,
                     "axis": axis, "position": point(-epsilon), "role": "tool"}
            features.append(mouth)
            features.append({"id": prefix + "_SocketTool", "type": "union",
                             "shapes": [socket_id, mouth["id"]], "role": "tool"})
            socket_id = prefix + "_SocketTool"
    else:
        # Boxes use world axes. The two transverse dimensions are centered.
        transverse = [i for i in range(3) if i != index]
        for suffix, gap, height, offset in (("_Pin", 0, length + embed, -embed),
                                           ("_Socket", clearance, length + bottom_clearance + epsilon, -epsilon)):
            dims = [0., 0., 0.]
            dims[index] = height
            dims[transverse[0]], dims[transverse[1]] = width + 2*gap, thickness + 2*gap
            p = list(position)
            p[index] += offset
            for i in transverse:
                p[i] -= dims[i] / 2
            features.append({"id": prefix + suffix, "type": "box", "role": "tool",
                             "size": dict(zip("xyz", dims)), "position": dict(zip("xyz", p))})
        pin_id, socket_id = prefix + "_Pin", prefix + "_Socket"
    male_id, female_id = prefix + "_Male", prefix + "_Female"
    male = {"id": male_id, "type": "union", "shapes": [male_base, pin_id], "role": "output"}
    female = {"id": female_id, "type": "cut", "base": female_base, "tool": socket_id, "role": "output"}
    for feature, material in ((male, male_material), (female, female_material)):
        if material is not None:
            feature["material"] = copy.deepcopy(material)
    features.extend([male, female])
    if {male_base, female_base} & {f["id"] for f in features}:
        raise ValueError("connector identifiers collide with base features")
    return {"features": features, "output_ids": [male_id, female_id], "units": "mm",
            "clearance_per_side_mm": clearance, "bottom_clearance_mm": bottom_clearance,
            "lead_in_applied": kind == "pin_socket" and lead_in > 0,
            "notes": ["Rectangular glue ledges do not apply lead_in."] if kind == "glue_ledge" else [],
            "verification_required": ["pin embedded in male base", "socket within receiving wall",
                                      "local seating and remaining wall thickness", "physical clearance coupon"],
            "assembly": "insert along positive " + axis + "; attach with adhesive if required"}


def inspect_attachment(shape, target, anchor, radius=5, tolerance=.05):
    """Check contact only near an intended seat, not anywhere on the parts."""
    radius = _length(radius, "radius")
    tolerance = _length(tolerance, "tolerance", False)
    if not isinstance(anchor, (list, tuple)) or len(anchor) != 3 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in anchor):
        raise ValueError("anchor must contain three finite millimeter numbers")
    for part in (shape, target):
        if part.isNull() or not part.isValid():
            raise ValueError("attachment inspection requires valid nonempty shapes")
    import FreeCAD as App
    import Part
    region = Part.makeSphere(radius, App.Vector(*anchor))
    local = [part.common(region) for part in (shape, target)]
    if any(part.isNull() or not part.Vertexes for part in local):
        return {"status": "NO_LOCAL_GEOMETRY", "anchor": list(anchor), "radius_mm": radius,
                "gap_mm": None, "overlap_volume_mm3": None, "seated": False,
                "tolerance_mm": tolerance,
                "limitation": "Local contact does not prove retention, glue area, or assembly accessibility."}
    gap = float(local[0].distToShape(local[1])[0])
    overlap = float(local[0].common(local[1]).Volume)
    return {"status": "SEATED" if gap <= tolerance else "GAP", "anchor": list(anchor),
            "radius_mm": radius, "gap_mm": gap, "overlap_volume_mm3": overlap,
            "tolerance_mm": tolerance, "seated": gap <= tolerance,
            "limitation": "Local contact does not prove retention, glue area, or assembly accessibility."}
