"""Validated native whole-assembly 3MF transforms; MIT, extracted from FreeCAD pipeline."""
import json, math, xml.etree.ElementTree as ET, zipfile
CORE = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
PROD = 'http://schemas.microsoft.com/3dmanufacturing/production/2015/06'

def _identity():
    return [[1.,0.,0.,0.], [0.,1.,0.,0.], [0.,0.,1.,0.], [0.,0.,0.,1.]]


def _transform(text):
    if text is None:
        return _identity()
    values = [float(v) for v in text.split()]
    if len(values) != 12 or not all(math.isfinite(v) for v in values):
        raise ValueError('Invalid model placement')
    return [[values[c*3+r] for c in range(4)] for r in range(3)] + [[0.,0.,0.,1.]]


def _multiply(a, b):
    return [[sum(a[r][k]*b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def _point(matrix, xyz):
    return [sum(matrix[r][c]*xyz[c] for c in range(3))+matrix[r][3] for r in range(3)]


def _rotate_project(entries, matrix, max_object_visits=10000, max_vertex_visits=6000000):
    # Keep component and mesh bytes intact; transform the one build item as an assembly.
    trees = {}
    def tree(path):
        if path not in trees:
            trees[path] = ET.fromstring(entries[path])
        return trees[path]
    root = tree('3D/3dmodel.model')
    if root.get('unit', 'millimeter') != 'millimeter':
        raise ValueError('Orientation requires millimeter units')
    items = root.findall('{%s}build/{%s}item' % (CORE, CORE))
    if len(items) != 1:
        raise ValueError('Orientation requires one whole assembly build item')
    bounds = [[math.inf]*3, [-math.inf]*3]
    object_visits = vertex_visits = 0
    def visit(path, oid, placement, ancestors):
        nonlocal object_visits, vertex_visits
        object_visits += 1
        if object_visits > max_object_visits:
            raise ValueError(f'Assembly exceeds {max_object_visits} object visits')
        key = (path, oid)
        if key in ancestors or len(ancestors) > 64:
            raise ValueError('Cyclic or excessively nested model components')
        obj = tree(path).find("{%s}resources/{%s}object[@id='%s']" % (CORE, CORE, oid))
        if obj is None:
            raise ValueError('Missing component object')
        for vertex in obj.iterfind('{%s}mesh/{%s}vertices/{%s}vertex' % (CORE, CORE, CORE)):
            vertex_visits += 1
            if vertex_visits > max_vertex_visits:
                raise ValueError(f'Assembly exceeds {max_vertex_visits} vertex visits')
            p = _point(placement, [float(vertex.get(k)) for k in ('x','y','z')])
            if not all(math.isfinite(v) for v in p):
                raise ValueError('Nonfinite mesh vertex')
            for i in range(3):
                bounds[0][i] = min(bounds[0][i], p[i]); bounds[1][i] = max(bounds[1][i], p[i])
        for comp in obj.iterfind('{%s}components/{%s}component' % (CORE, CORE)):
            child = comp.get('{%s}path' % PROD, path).lstrip('/')
            visit(child, comp.get('objectid'), _multiply(placement, _transform(comp.get('transform'))), ancestors | {key})
    item = items[0]
    original = _transform(item.get('transform'))
    visit('3D/3dmodel.model', item.get('objectid'), original, set())
    if not all(math.isfinite(v) for row in bounds for v in row):
        raise ValueError('Assembly has no vertices')
    center = [(a+b)/2 for a,b in zip(*bounds)]
    rotation = [list(row)+[0.] for row in matrix]+[[0.,0.,0.,1.]]
    for i in range(3):
        rotation[i][3] = center[i]-sum(matrix[i][j]*center[j] for j in range(3))
    # Measure exact rotated vertices, rather than bounding-box corners.
    transformed = _multiply(rotation, original)
    bounds = [[math.inf]*3, [-math.inf]*3]
    visit('3D/3dmodel.model', item.get('objectid'), transformed, set())
    transformed[2][3] -= bounds[0][2]
    item.set('transform', ' '.join(format(transformed[r][c], '.17g') for c in range(4) for r in range(3)))
    # Preserve original namespace prefixes (requiredextensions refers to p).
    ET.register_namespace('', CORE); ET.register_namespace('p', PROD)
    ET.register_namespace('BambuStudio', 'http://schemas.bambulab.com/package/2021')
    entries['3D/3dmodel.model'] = ET.tostring(root, encoding='utf-8', xml_declaration=True)


def _read_project(path, max_archive_bytes=1024**3, max_object_visits=10000, max_vertex_visits=6000000):
    """Read only native millimeter projects containing one assembly build item."""
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len({i.filename for i in infos}) != len(infos) or sum(i.file_size for i in infos) > max_archive_bytes:
                raise ValueError('Duplicate or oversized 3MF entries')
            entries = {i.filename: archive.read(i) for i in infos}
        for key in ('Metadata/project_settings.config', 'Metadata/model_settings.config', '3D/3dmodel.model'):
            if key not in entries:
                raise ValueError('Input must be a native Bambu project with filament/part settings')
        settings = json.loads(entries['Metadata/project_settings.config'])
        if not isinstance(settings, dict):
            raise ValueError('Invalid project settings')
        ET.fromstring(entries['Metadata/model_settings.config'])
        model = ET.fromstring(entries['3D/3dmodel.model'])
        if not any(m.get('name') == 'Application' and (m.text or '').startswith('BambuStudio-')
                   for m in model.findall('{%s}metadata' % CORE)):
            raise ValueError('Input must identify itself as a native Bambu project; export it from Bambu Studio first')
        # Validate every referenced object and placement even for an unchanged baseline.
        try:
            _rotate_project(dict(entries), [[1,0,0],[0,1,0],[0,0,1]], max_object_visits, max_vertex_visits)
        except (KeyError, TypeError, ET.ParseError, OverflowError) as exc:
            raise ValueError('Invalid native assembly: ' + str(exc)) from exc
        return entries, settings
    except (zipfile.BadZipFile, ET.ParseError) as exc:
        raise ValueError("Invalid native Bambu project: " + str(exc)) from exc
