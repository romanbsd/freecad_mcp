"""Deterministic native-3MF benchmark corpus; every case is actually compared and sliced."""
import argparse
import json
import shutil
from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile
from .pipeline import compare_project
from .runner import Runner, Variant

CORE = 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'
KW = dict(machine='Bambu Lab A1 mini 0.4 nozzle', process='0.20mm Standard @BBL A1M',
          filaments=['Bambu PLA Basic @BBL A1M'])
# Each tuple is x,y,z,width,depth,height,filament-slot. Touching box solids form each assembly.
CASES = {
    'thin_shell': [(0,0,0,60,40,2,1),(0,0,2,2,40,18,1),(58,0,2,2,40,18,1),
                   (2,0,2,56,2,18,1),(2,38,2,56,2,18,1)],
    'narrow_footprint': [(0,0,0,6,8,50,1)],
    'overhang_bracket': [(0,0,0,30,20,3,1),(0,0,3,4,20,27,1),(4,0,27,26,20,3,1)],
    'fitted_connector': [(0,0,0,12,12,3,1),(4,4,3,4,4,10,1),(20,0,0,12,12,3,1),
                         (20,0,3,3.7,12,8,1),(28.3,0,3,3.7,12,8,1),
                         (23.7,0,3,4.6,3.7,8,1),(23.7,8.3,3,4.6,3.7,8,1)],
    'multicolor_assembly': [(0,0,0,30,20,3,1),(0,0,3,15,20,5,1),(15,0,3,15,20,5,2)],
}
FACES = [(0,1,3),(0,3,2),(4,6,7),(4,7,5),(0,4,5),(0,5,1),
         (2,3,7),(2,7,6),(0,2,6),(0,6,4),(1,5,7),(1,7,3)]


def make_project(path, name, settings):
    ET.register_namespace('', CORE)
    root = ET.Element('{%s}model' % CORE, unit='millimeter')
    ET.SubElement(root, '{%s}metadata' % CORE, name='Application').text = 'freecad-mcp-benchmark'
    resources = ET.SubElement(root, '{%s}resources' % CORE)
    config = ET.Element('config')
    assembly_id = len(CASES[name]) + 1
    parts = ET.SubElement(config, 'object', id=str(assembly_id))
    ET.SubElement(parts, 'metadata', key='name', value=name)
    for oid, (x,y,z,w,d,h,slot) in enumerate(CASES[name], 1):
        obj = ET.SubElement(resources, '{%s}object' % CORE, id=str(oid), type='model')
        mesh = ET.SubElement(obj, '{%s}mesh' % CORE)
        vertices = ET.SubElement(mesh, '{%s}vertices' % CORE)
        for a in (0,w):
            for b in (0,d):
                for c in (0,h):
                    ET.SubElement(vertices, '{%s}vertex' % CORE, x=str(x+a), y=str(y+b), z=str(z+c))
        triangles = ET.SubElement(mesh, '{%s}triangles' % CORE)
        for a,b,c in FACES:
            ET.SubElement(triangles, '{%s}triangle' % CORE, v1=str(a), v2=str(b), v3=str(c))
        part = ET.SubElement(parts, 'part', id=str(oid), subtype='normal_part')
        ET.SubElement(part, 'metadata', key='name', value=f'{name}_{oid}')
        ET.SubElement(part, 'metadata', key='extruder', value=str(slot))
        ET.SubElement(part, 'metadata', key='matrix', value='1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1')
    obj = ET.SubElement(resources, '{%s}object' % CORE, id=str(assembly_id), type='model')
    components = ET.SubElement(obj, '{%s}components' % CORE)
    for oid in range(1, assembly_id):
        ET.SubElement(components, '{%s}component' % CORE, objectid=str(oid))
    build = ET.SubElement(root, '{%s}build' % CORE)
    ET.SubElement(build, '{%s}item' % CORE, objectid=str(assembly_id), transform='1 0 0 0 1 0 0 0 1 80 80 0')
    process = dict(settings)
    process.update(enable_support='1' if name=='overhang_bracket' else '0', brim_type='outer_only',
                   filament_colour=['#00AA00','#FFFFFF'] if name=='multicolor_assembly' else ['#00AA00'])
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="/3D/3dmodel.model" Id="rel0" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr('3D/3dmodel.model', ET.tostring(root, encoding='utf-8', xml_declaration=True))
        z.writestr('Metadata/model_settings.config', ET.tostring(config, encoding='utf-8'))
        z.writestr('Metadata/project_settings.config', json.dumps(process))
    return Path(path)


def run_benchmarks(output_dir, binary=None, cases=None):
    destination = Path(output_dir); destination.mkdir(parents=True, exist_ok=False)
    r = Runner(binary=binary, home=destination/'cache')
    settings = {}
    for kind,key in [('machine','machine'),('process','process'),('filament','filaments')]:
        settings.update(r._load_preset(kind, KW[key] if kind!='filament' else KW[key][0]))
    rows = []
    for name in (list(CASES) if cases is None else cases):
        process = r._load_preset('process', KW['process'])
        process.update(brim_type='outer_only', enable_support='1' if name=='overhang_bracket' else '0',
                       wipe_tower_x=['20'], wipe_tower_y=['20'])
        process_path = destination/(name+'_process.json')
        process_path.write_text(json.dumps(process))
        raw_source = make_project(destination/(name+'_geometry.3mf'), name, {**settings, **process})
        filaments = list(KW['filaments'])
        if name == 'multicolor_assembly':
            white = r._load_preset('filament', KW['filaments'][0])
            white['filament_colour'] = ['#FFFFFF']
            white['name'] = 'Benchmark white PLA'
            white_path = destination/'white_pla.json'
            white_path.write_text(json.dumps(white))
            filaments.append(str(white_path.resolve()))
        # Let the real slicer author native IDs, profile metadata and filament maps.
        canonical = r.evaluate(raw_source, Variant(flags={'orient':0,'arrange':1},plate=1),
                               machine=KW['machine'], process=str(process_path.resolve()),
                               filaments=filaments, timeout_s=60)
        if not canonical.ok:
            raise RuntimeError(f'{name} canonicalization failed: {canonical.error}')
        source = destination/(name+'.3mf')
        shutil.copy(canonical.out_3mf, source)
        spec = dict(machine_preset=KW['machine'], process_preset=str(process_path.resolve()), filament_presets=filaments)
        report = compare_project(source, destination/name,
                                 [{'name':'coarser','settings':{'layer_height':.28}},
                                  {'name':'leaner','settings':{'sparse_infill_density':10}}],
                                 r.binary, 300, spec, 2, r.home)
        if report['candidates'][0]['status'] != 'success' or not report['winners']['fastest']:
            raise RuntimeError(f'{name} benchmark baseline failed: {report["candidates"][0].get("error", report["candidates"][0].get("constraint_violations"))}')
        rows.append(dict(case=name, report=report))
    summary = dict(slicer_version=r.version(), estimate_only=True, cases=rows)
    (destination/'benchmark.json').write_text(json.dumps(summary,indent=2))
    return summary


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', required=True)
    p.add_argument('--binary')
    args = p.parse_args()
    report = run_benchmarks(args.output, args.binary)
    print(json.dumps({'slicer_version':report['slicer_version'], 'cases':[r['case'] for r in report['cases']]}))

if __name__=='__main__':
    main()
