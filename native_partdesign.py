"""Build a small, editable Part Design feature chain from declarative sketches.

This is an opt-in native alternative to the generic component graph. It accepts
the existing ``sketch_2d`` geometry and constraint vocabulary, then uses real
Part Design features and a Spreadsheet instead of proxy solids.
"""

import re
from pathlib import Path

import FreeCAD as App


_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_FEATURE_TYPES = {
    "loft": "PartDesign::AdditiveLoft",
    "pad": "PartDesign::Pad",
    "pocket": "PartDesign::Pocket",
    "revolve": "PartDesign::Revolution",
    "groove": "PartDesign::Groove",
}


def _name(value, label):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError("%s must be an alphanumeric FreeCAD identifier" % label)
    return value


def _expression(value, sheet_name="Parameters"):
    from parametric import _translate
    return _translate(str(value), sheet_name)


def _validate_spec(document, body, parameters, features):
    _name(document, "document name")
    _name(body, "body name")
    names = {"Parameters", body}
    for parameter in parameters:
        name = _name(parameter.get("name"), "parameter name")
        if name in names:
            raise ValueError("duplicate identifier %r" % name)
        names.add(name)
        if parameter.get("default") is None and parameter.get("expression") is None:
            raise ValueError("parameter %r needs a default or expression" % name)
    feature_ids = set()
    for feature in features:
        fid = _name(feature.get("id"), "feature id")
        if fid in names or fid in feature_ids:
            raise ValueError("duplicate identifier %r" % fid)
        feature_ids.add(fid)
        if feature.get("type") not in _FEATURE_TYPES and feature.get("type") != "sketch_2d":
            raise ValueError("native Part Design does not support feature type %r" % feature.get("type"))
        if feature.get("type") in ("pad", "pocket") and feature.get("length") is None:
            raise ValueError("feature %r needs a length" % fid)
    if not any(feature.get("type") in ("loft", "pad", "revolve")
               for feature in features):
        raise ValueError("native Part Design body needs an additive feature")


def _parameters(doc, parameters):
    sheet = doc.addObject("Spreadsheet::Sheet", "Parameters")
    sheet.Label = "Parameters"
    sheet.set("A1", "Alias")
    sheet.set("B1", "Value")
    sheet.set("C1", "Description")
    for row, parameter in enumerate(parameters, 2):
        name = parameter["name"]
        sheet.set("A%d" % row, name)
        sheet.set("B%d" % row, str(parameter.get("default", "0 mm")))
        sheet.setAlias("B%d" % row, name)
        sheet.set("C%d" % row, parameter.get("description", ""))
    for row, parameter in enumerate(parameters, 2):
        if parameter.get("expression") is not None:
            # Spreadsheet formulas use local aliases, not object-qualified names.
            formula = re.sub(r"\$([A-Za-z][A-Za-z0-9_]*)", r"\1",
                             str(parameter["expression"]))
            sheet.set("B%d" % row, "=" + formula)
    doc.recompute()
    return sheet


def _profile(feature, objects):
    pid = feature.get("profile")
    if pid not in objects or objects[pid].TypeId != "Sketcher::SketchObject":
        raise ValueError("feature %r needs a preceding sketch_2d profile" % feature["id"])
    return objects[pid]


def _solid_ok(feature):
    shape = feature.Shape
    if shape.isNull() or not shape.isValid() or len(shape.Solids) != 1:
        raise ValueError("%s did not create one valid solid" % feature.Name)
    return {"volume_mm3": shape.Volume, "solid_count": len(shape.Solids)}


def build_native_partdesign(document, body, parameters, features, save_path=None):
    """Build one native Body with fully constrained sketches and spreadsheet inputs.

    Feature order is the Part Design history. Supported operations are additive
    loft, pad, pocket, revolution, and groove. Existing documents are never
    replaced; a failed build closes only the new document.
    """
    from parametric import _build_sketch

    _validate_spec(document, body, parameters, features)
    if document in App.listDocuments():
        raise ValueError("document %r already exists" % document)
    destination = Path(save_path).expanduser().resolve() if save_path else None
    if destination and destination.suffix.lower() != ".fcstd":
        raise ValueError("save_path must end in .FCStd")
    if destination and destination.exists():
        raise ValueError("save_path %s already exists" % destination)
    doc = App.newDocument(document)
    save_attempted = False
    try:
        sheet = _parameters(doc, parameters)
        part_body = doc.addObject("PartDesign::Body", body)
        objects = {}
        stages = []
        for feature in features:
            fid, kind = feature["id"], feature["type"]
            if kind == "sketch_2d":
                spec = dict(feature, require_fully_constrained=True)
                sketch = _build_sketch(doc, sheet, spec, objects)
                part_body.addObject(sketch)
                doc.recompute()
                objects[fid] = sketch
                stages.append({"id": fid, "type": kind, "dof": sketch.DoF})
                continue
            obj = part_body.newObject(_FEATURE_TYPES[kind], fid)
            if kind == "loft":
                profiles = feature.get("loft_profiles") or []
                if len(profiles) < 2:
                    raise ValueError("loft %r needs at least two sketches" % fid)
                sketches = [_profile({"id": fid, "profile": p}, objects) for p in profiles]
                obj.Profile = sketches[0]
                obj.Sections = sketches[1:]
                obj.Ruled = bool(feature.get("ruled", False))
                profiles_to_hide = sketches
            else:
                sketch = _profile(feature, objects)
                obj.Profile = sketch
                profiles_to_hide = [sketch]
                if kind in ("pad", "pocket"):
                    obj.setExpression("Length", _expression(feature["length"]))
                else:
                    axis = feature.get("axis", "horizontal")
                    if axis not in ("horizontal", "vertical"):
                        raise ValueError("revolve/groove axis must be horizontal or vertical")
                    obj.ReferenceAxis = (sketch, ["H_Axis" if axis == "horizontal" else "V_Axis"])
                    obj.Reversed = bool(feature.get("reversed", False))
                    obj.setExpression("Angle", _expression(feature.get("angle", "360 deg")))
            doc.recompute()
            stages.append({"id": fid, "type": kind, **_solid_ok(obj)})
            objects[fid] = obj
            for profile in profiles_to_hide:
                profile.Visibility = False
        output = next(objects[f["id"]] for f in reversed(features)
                      if f["type"] in _FEATURE_TYPES)
        part_body.Tip = output
        doc.recompute()
        result = {"document": doc.Name, "body": part_body.Name,
                  "spreadsheet": sheet.Name, "output": output.Name,
                  "features": stages, "valid": output.Shape.isValid(),
                  "solid_count": len(output.Shape.Solids)}
        if destination:
            save_attempted = True
            doc.saveAs(str(destination))
            result["path"] = str(destination)
        return result
    except Exception:
        App.closeDocument(doc.Name)
        if save_attempted and destination.exists():
            destination.unlink()
        raise
