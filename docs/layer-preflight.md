# Layer support preflight

Run this optional geometric screen before export and slicing. It supplements the
existing sampled wall/normal check; it does not certify support-free printing.
Existing calls retain their previous behavior unless `layer_sampling` is supplied.

```python
inspect_printability(
    document_name="MyDocument", object_name="FinalBody",
    build_direction=[0, 0, 1],
    layer_sampling={"layer_height": 0.2, "xy_resolution": 0.8,
                    "max_layers": 2000, "max_work": 2000000,
                    "time_budget": 15, "max_findings": 200})
```

Use either `document_name` plus the exact native `object_name`, or the existing
`component_id` / optional `feature_id`. Native inspection copies the shape and
uses its global placement; the document is not edited. Screen each printable
part in its intended build direction. For a part printed toward negative Y,
use `build_direction=[0,-1,0]`.

`layers` contains completion, stop reason, expected/examined layers, resolution,
work/time consumption, findings and total counts. The outer status is WARN if the
layer check warns. Native planar sections are sampled midway through successive
layers and scan-converted at the requested XY pitch, including section holes.
Adjacent-layer raster components distinguish:

- `floating_layer_island`: no previous-layer material within the allowance,
  including branches that connect to the body only at a later height;
- `unsupported_layer_expansion`: an anchored component extends beyond the
  geometric overhang allowance;
- `potential_bridge`: expansion connects two separate previous-layer anchors.
  This still requires slicer review; it does not approve the span or toolpath.

Findings include a world-coordinate point, layer number and sampled area. These
are raster locations and areas, not exact feature dimensions. The overhang
allowance uses layer height and angle, rounded up to an XY cell (capped at 20
cells for bounded work). Features smaller than the pitch or layer interval can
be missed; reduce pitch for small axle tips. Nonempty sections that disappear
in the raster increment `unresolved_empty_raster_layers` and produce WARN.
Fuse overlapping compound solids before screening: even/odd section fill assumes
nonoverlapping material contours.

Budgets are intentionally bounded: max_layers 1–10000, max_work 1–10000000,
time_budget >0–120 seconds, max_findings 1–1000. Work counts section vertices,
scan intersections and connectivity probes. Findings are capped independently
of counts. `complete=false` means the requested height was not fully screened;
it must not be treated as passing. A section failure is inconclusive and reports
its exception. Time checks are cooperative between native operations; one OCC
section operation cannot be interrupted by this API.

The screen does not predict adhesion, strength, support removal, bridge sag,
extrusion paths, or actual printing success. Review the final slicer preview
and warnings after optimization.
