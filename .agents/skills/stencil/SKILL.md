---
name: stencil
description: Create spray-paint stencils from high-contrast images using FreeCAD MCP, with traced cutouts, retaining bridges, and validated FCStd, STEP, and STL exports. Use for flat image-derived paint templates, lettering, and logos.
---

# Image-to-stencil workflow

Use the bundled pipeline rather than reimplementing tracing or FreeCAD geometry.
Paths below are relative to this skill folder; resolve its location before
running scripts. Read [references/pipeline.md](references/pipeline.md) for CLI
options, dependency setup, and troubleshooting when needed.

## Trace the artwork

- Inspect the source image. Dark pixels become paint openings; the light
  background and enclosed letter centers remain stencil material. Photos and
  shaded images need preprocessing before this threshold-based tracer applies.
- Honor the requested dimensions. `--width` controls artwork width, not plate
  width; `--margin` adds a border on each side. All lengths are mm. Clarify this
  distinction if the request is ambiguous and it materially affects the result.
- Run `scripts/trace_image.py` with the image, dimensions, and output directory.
  Use an isolated Python environment with `scripts/requirements.txt`; FreeCAD
  itself does not need these dependencies. Keep generated output outside the
  skill folder.
- Review `stencil_preview.png`. The tracer connects enclosed centers with
  bridges, which interrupt the painted strokes. Choose bridge width for the
  intended scale and manufacturing method; the 0.8 mm default is configurable.
  Stop and adjust the trace when details disappear or material is disconnected.

The trace includes source identity, polygons, dimensions, and bridge settings.
`trace.json`, the preview, and `build.FCMacro` are intermediate files, not the
finished stencil. Existing output is protected unless `--overwrite` is supplied;
use it only when replacement is within the user's request.

## Build and validate with FreeCAD MCP

Execute the generated `build.FCMacro` through FreeCAD MCP `execute`, using its
absolute path. It loads the bundled `scripts/build_freecad.py`, creates a new
document, and preserves other open documents. If the skill or output directory
moved, regenerate the macro because it embeds absolute paths.

The builder creates editable plate, profile, extrusion, and cut features.
`Plate.Height` drives the extrusion depth; regenerate the trace for artwork
dimensions or bridge changes. It exports `stencil.FCStd`, `stencil.step`, and
`stencil.stl`, then writes `validation.json` after successful checks:

- One valid, closed solid with lettering cut through the plate.
- STEP read-back with valid solid geometry and expected dimensions.
- STL read-back with one connected, watertight mesh.

Inspect a FreeCAD MCP screenshot after building, especially lettering direction,
cutouts, and retained centers. If validation fails, inspect and correct the model
before treating any already-written export as a deliverable. Report artifact
paths and actual dimensions. Distinguish CAD validation from slicer checks or
physical print results; run those only when requested or needed for the task.

## Maintain the pipeline

After relocating or modifying scripts, run:

```sh
python -m unittest discover -s <skill-folder>/scripts -v
```

Use a separate output directory for a real-image smoke test and check that its
macro resolves the current builder path. For geometry changes, execute the
macro in FreeCAD and review validation and a screenshot.
