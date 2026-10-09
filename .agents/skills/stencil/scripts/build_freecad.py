"""Run inside FreeCAD (GUI macro or MCP execute); no tracing dependencies needed."""
import json
from pathlib import Path


def build_stencil(trace_path, overwrite=False):
    import FreeCAD as App
    import FreeCADGui as Gui
    import Part
    import Mesh
    import MeshPart

    trace_path = Path(trace_path).expanduser().resolve()
    data = json.loads(trace_path.read_text())
    output = trace_path.parent
    if not overwrite and any((output / ('stencil' + ext)).exists() for ext in ('.FCStd', '.step', '.stl')):
        raise FileExistsError('CAD exports exist; pass overwrite=True to replace')
    doc = App.newDocument('ImageStencil')

    def face(coords):
        points = [App.Vector(x, y, 0) for x, y in coords]
        if points[0].distanceToPoint(points[-1]) > 1e-9:
            points.append(points[0])
        return Part.Face(Part.makePolygon(points))

    faces = []
    for polygon in data['polygons']:
        f = face(polygon['outer'])
        for hole in polygon['holes']:
            f = f.cut(face(hole))
        faces.extend(f.Faces)
    profile = doc.addObject('Part::Feature', 'LetteringProfile')
    profile.Label = 'Traced lettering with island bridges'
    profile.Shape = Part.makeCompound(faces)
    profile.addProperty('App::PropertyString', 'SourceImage', 'Source')
    profile.SourceImage = data['source']
    profile.addProperty('App::PropertyLength', 'LetteringWidth', 'Source')
    profile.LetteringWidth = data['lettering_width']
    profile.addProperty('App::PropertyLength', 'BridgeWidth', 'Source')
    profile.BridgeWidth = data['bridge_width']
    plate = doc.addObject('Part::Box', 'Plate')
    plate.Length = data['plate_width']
    plate.Width = data['plate_height']
    plate.Height = data['thickness']
    tool = doc.addObject('Part::Extrusion', 'CutoutTool')
    tool.Base = profile
    tool.DirMode = 'Custom'
    tool.Dir = App.Vector(0, 0, 1)
    tool.Solid = True
    tool.setExpression('LengthFwd', 'Plate.Height')
    stencil = doc.addObject('Part::Cut', 'Stencil')
    stencil.Label = 'Spray stencil — %.1f mm lettering / %.1f mm thick' % (data['lettering_width'], data['thickness'])
    stencil.Base = plate
    stencil.Tool = tool
    doc.recompute()
    shape = stencil.Shape
    if not shape.isValid() or not shape.isClosed() or len(shape.Solids) != 1:
        raise ValueError('Stencil must be one valid, closed solid; inspect bridges.')
    removed = plate.Shape.Volume - shape.Volume
    if removed <= 0 or abs(removed - tool.Shape.Volume) > 0.001:
        raise ValueError('Lettering must cut completely through the plate')
    for obj in (profile, plate, tool):
        obj.Visibility = False
    stencil.ViewObject.ShapeColor = (0.88, 0.78, 0.38)
    stencil.ViewObject.LineColor = (0.18, 0.16, 0.10)
    Gui.activeDocument().activeView().viewTop()
    Gui.activeDocument().activeView().fitAll()
    basename = output / 'stencil'
    doc.saveAs(str(basename.with_suffix('.FCStd')))
    Part.export([stencil], str(basename.with_suffix('.step')))
    mesh = MeshPart.meshFromShape(Shape=shape, LinearDeflection=0.03,
                                 AngularDeflection=0.12, Relative=False)
    mesh.write(str(basename.with_suffix('.stl')))
    imported = Part.read(str(basename.with_suffix('.step')))
    mesh_read = Mesh.Mesh(str(basename.with_suffix('.stl')))
    if not imported.isValid() or len(imported.Solids) != 1:
        raise ValueError('STEP read-back failed')
    if not mesh_read.isSolid() or mesh_read.countComponents() != 1:
        raise ValueError('STL read-back must be watertight and connected')
    expected = (data['plate_width'], data['plate_height'], data['thickness'])
    actual = (imported.BoundBox.XLength, imported.BoundBox.YLength, imported.BoundBox.ZLength)
    if any(abs(a-b) > 0.001 for a, b in zip(expected, actual)):
        raise ValueError('STEP read-back dimensions differ')
    report = {'valid': shape.isValid(), 'closed': shape.isClosed(),
              'solids': len(shape.Solids), 'dimensions_mm': actual,
              'volume_mm3': shape.Volume, 'bridges': data['bridges'],
              'cutout_volume_mm3': removed,
              'step_readback_valid': imported.isValid(),
              'stl_watertight': mesh_read.isSolid(),
              'stl_components': mesh_read.countComponents(),
              'stl_facets': mesh_read.CountFacets}
    (output / 'validation.json').write_text(json.dumps(report, indent=2))
    return report
