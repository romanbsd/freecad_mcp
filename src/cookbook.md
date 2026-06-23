# FreeCAD scripting cookbook (for the `execute` tool)

Working idioms for driving FreeCAD through the `execute` tool. Copy and adapt.

## Ground rules
- Namespace already has `App` (FreeCAD), `Gui` (FreeCADGui), `doc` (active document, may be `None`).
- `import Part`, `import Sketcher`, `import Draft` yourself when you need them.
- The document is recomputed for you after each `execute`; the whole call is one undo step.
- Return data by assigning to `result` or using `print()` — both come back.
- Object **names** (`obj.Name`) are unique IDs; **labels** (`obj.Label`) are the user-facing text.

## Which paradigm?
- **`Part`** — direct CSG solids (Box, Cylinder, Cut, Fillet…). Simplest; use this unless you need a parametric feature tree.
- **`PartDesign`** — sketch-based parametric features inside a `Body` (Sketch → Pad/Pocket/Revolution). Use for editable, history-based parts.
- **`Draft`** — 2D drafting, arrays, annotations.
- **`Sketcher`** — constrained 2D sketches (usually consumed by PartDesign).

---

## New / pick document
```python
doc = App.ActiveDocument or App.newDocument("Part")
```

## Part primitives
```python
import Part
box = doc.addObject("Part::Box", "Box")
box.Length, box.Width, box.Height = 10, 20, 5

cyl = doc.addObject("Part::Cylinder", "Cyl")
cyl.Radius, cyl.Height = 3, 20
# others: Part::Sphere (Radius), Part::Cone (Radius1, Radius2, Height)
```

## Boolean operations (parametric)
```python
cut = doc.addObject("Part::Cut", "Cut")      # also Part::Fuse, Part::Common
cut.Base, cut.Tool = box, cyl
# one-shot on shapes instead: result = box.Shape.cut(cyl.Shape)
```

## Fillet / chamfer all edges
```python
fil = doc.addObject("Part::Fillet", "Fillet")
fil.Base = box
fil.Edges = [(i + 1, 2.0, 2.0) for i in range(len(box.Shape.Edges))]  # (edge#, r1, r2)
box.Visibility = False   # hide the source so only the result shows
```

## Fillet specific edges (target by index)
Use the `get_subelements` tool to list a shape's edges. Each edge has a curve
type, length, center, endpoints (`start`/`end`) and a unit `direction` (null
for closed/curved edges) — filter by `direction` to pick, e.g., only the
vertical edges (`direction == [0,0,1]` or `[0,0,-1]`). Then fillet by 1-based
index:
```python
fil = doc.addObject("Part::Fillet", "Fillet")
fil.Base = box
fil.Edges = [(3, 1.5, 1.5), (5, 1.5, 1.5)]   # only edges 3 and 5
box.Visibility = False
```
The `get_selection` tool also returns sub-element names (Edge1/Face2) the user
has clicked, so you can fillet exactly what they picked.

## PartDesign: sketch → pad (FreeCAD 1.x)
```python
import Part, Sketcher
body = doc.addObject("PartDesign::Body", "Body")
sk = body.newObject("Sketcher::SketchObject", "Sketch")
sk.AttachmentSupport = [(doc.XY_Plane, "")]   # FreeCAD <1.0 used sk.Support
sk.MapMode = "FlatFace"
# a 10x6 rectangle
pts = [App.Vector(0, 0, 0), App.Vector(10, 0, 0), App.Vector(10, 6, 0), App.Vector(0, 6, 0)]
for a, b in zip(pts, pts[1:] + pts[:1]):
    sk.addGeometry(Part.LineSegment(a, b), False)
pad = body.newObject("PartDesign::Pad", "Pad")
pad.Profile = sk
pad.Length = 5
```

## Move / rotate
```python
box.Placement.Base = App.Vector(10, 0, 0)
box.Placement.Rotation = App.Rotation(App.Vector(0, 0, 1), 45)  # axis, angle in degrees
```

## Inspect (read without mutating)
```python
result = {
    "objects": [(o.Name, o.TypeId) for o in doc.Objects],
    "props": box.PropertiesList,
    "bbox": tuple(box.Shape.BoundBox),          # (XMin,YMin,ZMin,XMax,YMax,ZMax)
    "volume": box.Shape.Volume,
    "faces": len(box.Shape.Faces),
}
```

## Check for build errors
```python
result = [(o.Name, o.State) for o in doc.Objects if not o.isValid()]
```

## Export
```python
import Part
Part.export([box], "/tmp/out.step")   # by extension: .step .iges .brep
box.Shape.exportStl("/tmp/out.stl")
```

## Appearance (GUI only)
```python
box.ViewObject.ShapeColor = (1.0, 0.0, 0.0)   # RGB 0..1
box.ViewObject.Transparency = 50               # 0..100
box.ViewObject.Visibility = True
```

After building, call the `get_screenshot` tool (try `view="iso"`) to see the result.

## Parametric components (prefer this for reusable, editable models)
For anything with named dimensions you'll tweak, use the component tools instead
of raw `execute` — they build a live parametric tree FreeCAD rebuilds itself:
1. `create_component(document, name, parameters=[...])` — each parameter is
   `kind:"input"` (has `default`, optional `min`/`max`/`enum`) or `kind:"derived"`
   (has `expression`, read-only). Refer to params as `$name` in expressions.
2. `define_component(component_id, features=[...])` — feature types box, cylinder,
   cone, prism, transform, cut, union, intersection, array, group. Sizes/positions
   are expressions with units, e.g. `"$width / 2"`, `"$wall_thickness + 2 mm"`.
   Booleans/arrays reference other features by `id` (`{"type":"cut","base":"wall","tool":"hole"}`).
3. `set_component_parameters(component_id, {"width":"200 mm"})` — rebuilds only
   affected features. `validate_component` checks geometry/ranges/collisions.
   `render_component` (with optional `section`) and `export_component` finish.
