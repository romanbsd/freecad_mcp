# Make a spray-paint stencil from an image

Trace dark artwork on a light background, retain enclosed centers with bridges,
and build a flat stencil in FreeCAD. Photos, shaded artwork, and color separation
need preprocessing. Review the preview before printing; small source details may
be too fine for your nozzle, and bridges leave small interruptions in the paint.

## Generate the trace

Run from the repository root using Python with the dependencies below. The
Lamborghini example was verified with Python 3.13.

```sh
python3 -m venv /tmp/stencil-venv
/tmp/stencil-venv/bin/python -m pip install -r .agents/skills/stencil/scripts/requirements.txt
/tmp/stencil-venv/bin/python .agents/skills/stencil/scripts/trace_image.py \
  ~/Downloads/lamborghini.jpeg --width 70 --thickness 2 \
  --output outputs/lamborghini_stencil_70mm
```

`--width` is the artwork width; the default 8 mm margin adds 16 mm to the plate
width. The image aspect ratio determines artwork height. All lengths use mm.

| Option | Default | Meaning |
| --- | --- | --- |
| `--width` | 70 | Artwork width |
| `--thickness` | 2 | Plate thickness |
| `--margin` | 8 | Border on each side |
| `--bridge-width` | 0.8 | Retaining bridge width |
| `--threshold` | 128 | Pixels darker than this become cutouts |
| `--simplify` | 0.025 | Contour simplification tolerance in mm |
| `--overwrite` | Off | Permit replacing generated files and CAD exports |

The tracer writes `trace.json`, `stencil_preview.png`, and `build.FCMacro`. It
upscales and smooths the image, traces closed polygons, scales them, and inserts
bridges along shortest paths from enclosed centers to the outer contour. It
rejects disconnected stencil material and unusable images. Tiny noise contours
under 20 upscaled pixel area and holes under 10 are discarded.

## Build in FreeCAD

Open the generated `build.FCMacro` in FreeCAD and execute it, or send this Python
code to the FreeCAD MCP `execute` tool:

```python
from pathlib import Path
macro = Path('~/projects/freecad_mcp/outputs/lamborghini_stencil_70mm/build.FCMacro').expanduser()
exec(macro.read_text())
```

The macro embeds absolute paths to the trace and this repo's builder. After
moving either folder, regenerate the macro. Each build creates a new document
and leaves existing documents intact. FreeCAD needs no OpenCV or Shapely install.

Outputs are `stencil.FCStd`, `stencil.step`, `stencil.stl`, and `validation.json`
next to the trace. The native feature tree retains the plate, planar cutout
profile, extrusion, and cut. Editing `Plate.Height` updates the cut depth;
regenerate the trace to change artwork size or bridges.

Validation requires one closed, valid solid and verifies that the entire cutout
tool removes material. STEP read-back checks solid validity and dimensions; STL
read-back checks watertightness and connectivity. These checks establish CAD
integrity; slicer performance and physical printing need separate verification.
If validation fails, inspect the model and report before using any export.

## Check the pipeline

```sh
/tmp/stencil-venv/bin/python -m unittest discover -s .agents/skills/stencil/scripts -v
```

Tests cover enclosed-center retention, scaling, blank images, invalid dimensions,
and overwrite protection. The Lamborghini trace was also built and visually
checked through FreeCAD MCP, with STEP and STL read-back validation.
