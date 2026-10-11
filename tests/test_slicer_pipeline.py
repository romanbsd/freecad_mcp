import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
import zipfile

import pytest
import slicer_pipeline as sp

MODEL = '''<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" unit="millimeter"><metadata name="Application">BambuStudio-02.08.02.61</metadata><resources><object id="1"><mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="20" z="0"/><vertex x="0" y="0" z="5"/></vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object><object id="2"><components><component objectid="1"/></components></object></resources><build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 100 100 5"/></build></model>'''
PARTS = '<config><object id="2"><part id="1"><metadata key="extruder" value="2"/></part></object></config>'


@pytest.fixture
def project(tmp_path):
    path = tmp_path/'source.3mf'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('3D/3dmodel.model', MODEL)
        z.writestr('Metadata/project_settings.config', json.dumps({'layer_height':'0.2','filament_colour':['#00AA00','#FFFFFF'],'printer_settings_id':'test'}))
        z.writestr('Metadata/model_settings.config', PARTS)
    return path


@pytest.fixture
def slicer(tmp_path):
    # Independent executable fixture emits fixed Bambu-shaped artifacts, without
    # importing the module under test or duplicating its metric parser.
    script = tmp_path/'fake-bambu'
    script.write_text('#!'+sys.executable+'\n'+'''import json,sys,time,zipfile
from pathlib import Path
args=sys.argv[1:]
out=Path(args[args.index('--export-3mf')+1])
folder=Path(args[args.index('--outputdir')+1])
if not out.is_absolute(): out=folder/out
mode=folder.name
(folder/'invoked').write_text(json.dumps(args))
if mode=='timeout': time.sleep(30)
if mode=='flood':
 while True:
  sys.stdout.write('x'*65536);sys.stdout.flush()
if mode=='exit': print('preset missing');sys.exit(3)
if mode=='missing': sys.exit(0)
if mode=='broken': out.write_text('not a zip');sys.exit(0)
with zipfile.ZipFile(args[-1]) as inp:
 settings=json.loads(inp.read('Metadata/project_settings.config'))
 seconds=100 if settings['layer_height']=='0.2' else 80
outside='true' if mode=='outside' else 'false'
xml='<config><plate><metadata key="prediction" value="%s"/><metadata key="weight" value="42.5"/><metadata key="outside" value="%s"/></plate></config>'%(seconds,outside)
if mode=='nan': xml=xml.replace('42.5','NaN')
if mode=='zero': xml=xml.replace('42.5','0')
if mode=='zerotime': xml=xml.replace(str(seconds),'0')
with zipfile.ZipFile(out,'w') as z:
 z.writestr('Metadata/slice_info.config',xml)
 if mode!='nogcode': z.writestr('Metadata/plate_1.gcode','' if mode=='emptygcode' else '; total layer number: 50\\n; total filament change times: 7\\nG1 X1 Y1 E1\\n')
if mode=='badjson': (folder/'result.json').write_text('{}')
print('warning: preset missing')
''')
    script.chmod(0o755)
    return script


def test_subprocess_metrics_and_source_preserved(project,slicer,tmp_path):
    before=project.read_bytes()
    report=sp.compare_print_candidates(project,tmp_path/'results',[{'name':'fast','settings':{'layer_height':0.28,'sparse_infill_density':10}}],slicer)
    assert report['best_candidate']=='fast'
    assert report['source_sha256']==hashlib.sha256(before).hexdigest()
    assert project.read_bytes()==before
    assert report['source_unchanged']
    baseline,fast=report['candidates']
    assert baseline['metrics']['seconds']==100
    assert fast['metrics']['seconds']==80
    assert fast['metrics']['total_grams']==42.5
    assert fast['metrics']['layers']==50
    assert fast['metrics']['filament_changes']==7
    assert fast['metrics']['model_grams'] is None
    assert 'preset missing' in fast['log']
    assert (tmp_path/'results/fast/invoked').exists()
    with zipfile.ZipFile(fast['input_path']) as z,zipfile.ZipFile(project) as source:
        assert z.read('Metadata/model_settings.config')==source.read('Metadata/model_settings.config')
        assert z.read('3D/3dmodel.model')==source.read('3D/3dmodel.model')
        settings=json.loads(z.read('Metadata/project_settings.config'))
        assert settings['filament_colour']==['#00AA00','#FFFFFF']
        assert settings['sparse_infill_density']=='10%'


@pytest.mark.parametrize('reference', ['1', '2'])
@pytest.mark.parametrize('object_id', ['0', '-1', '', "1' or '1'='1", "1']"])
def test_malformed_object_references_are_cleanly_rejected(project, reference, object_id):
    with zipfile.ZipFile(project) as z:
        entries = {i.filename:z.read(i) for i in z.infolist()}
    entries['3D/3dmodel.model'] = entries['3D/3dmodel.model'].replace(
        f'objectid="{reference}"'.encode(), f'objectid="{object_id}"'.encode())
    with zipfile.ZipFile(project, 'w') as z:
        for name, data in entries.items():
            z.writestr(name, data)
    with pytest.raises(ValueError, match='Invalid component object id'):
        sp._read_project(project)


@pytest.mark.parametrize('name,error',[('exit','exit'),('missing','missing_output'),('broken','malformed_output'),('nan','malformed_output'),('nogcode','malformed_output'),('emptygcode','malformed_output'),('badjson','malformed_output'),('timeout','timeout'),('flood','log_limit'),('zero','malformed_output'),('zerotime','malformed_output')])
def test_failures_not_ranked(project,slicer,tmp_path,name,error):
    report=sp.compare_print_candidates(project,tmp_path/'results',[{'name':name}],slicer,timeout=1 if name=='timeout' else 5)
    assert report['candidates'][1]['error_type']==error
    assert report['best_candidate']=='baseline'


def test_outside_not_ranked(project,slicer,tmp_path):
    report=sp.compare_print_candidates(project,tmp_path/'r',[{'name':'outside','settings':{'layer_height':0.28}}],slicer)
    assert report['candidates'][1]['status']=='outside'
    assert report['best_candidate']=='baseline'


@pytest.mark.parametrize('candidate',[{'name':'../escape'},{'name':'baseline'}, {'name':'bad','settings':{'start_gcode':'evil'}}, {'name':'bad','settings':{'layer_height':float('nan')}}, {'name':'bad','rotation_matrix':[[2,0,0],[0,1,0],[0,0,1]]}, {'name':'bad','rotation_matrix':[[-1,0,0],[0,1,0],[0,0,1]]}])
def test_prevalidate_before_any_subprocess(project,slicer,tmp_path,candidate):
    with pytest.raises(ValueError):
        sp.compare_print_candidates(project,tmp_path/'results',[{'name':'fine'},candidate],slicer)
    assert not (tmp_path/'results').exists()


def test_refuse_existing_directory(project,slicer,tmp_path):
    folder=tmp_path/'results';folder.mkdir();(folder/'keep').write_text('keep')
    with pytest.raises(FileExistsError):
        sp.compare_print_candidates(project,folder,[{'name':'fast'}],slicer)
    assert (folder/'keep').read_text()=='keep'


def test_source_provenance_uses_parsed_snapshot_if_source_changes(project,slicer,tmp_path,monkeypatch):
    original=project.read_bytes()
    read=sp._read_project
    def changed_source(snapshot):
        project.write_bytes(b'newer externally saved project')
        return read(snapshot)
    monkeypatch.setattr(sp,'_read_project',changed_source)
    report=sp.compare_print_candidates(project,tmp_path/'results',[{'name':'candidate'}],slicer)
    assert report['source_sha256']==hashlib.sha256(original).hexdigest()
    assert report['source_unchanged'] is False
    assert report['candidates'][0]['status']=='success'
    assert project.read_bytes()==b'newer externally saved project'


def test_oversized_input_rejected_before_outputs(project,slicer,tmp_path,monkeypatch):
    monkeypatch.setattr(sp,'MAX_ARCHIVE_BYTES',8)
    with pytest.raises(ValueError,match='Oversized input'):
        sp.compare_print_candidates(project,tmp_path/'results',[{'name':'candidate'}],slicer)
    assert not (tmp_path/'results').exists()


def test_rotation_changes_only_build_transform(project,slicer,tmp_path):
    report=sp.compare_print_candidates(project,tmp_path/'r',[{'name':'rotate','rotation':[180,0,0]}],slicer)
    assert report['candidates'][1]['status']=='success'
    with zipfile.ZipFile(report['candidates'][1]['input_path']) as z:
        root=ET.fromstring(z.read('3D/3dmodel.model'))
        source=ET.fromstring(MODEL)
        assert ET.tostring(root.find('{%s}resources'%sp.CORE))==ET.tostring(source.find('{%s}resources'%sp.CORE))
        assert z.read('Metadata/model_settings.config').decode()==PARTS
        item=root.find('{%s}build/{%s}item'%(sp.CORE,sp.CORE))
        m=sp._transform(item.get('transform'))
        points=[sp._point(m,p) for p in [[0,0,0],[10,0,0],[0,20,0],[0,0,5]]]
        assert min(p[2] for p in points)==pytest.approx(0)
        assert sorted(p[2] for p in points)==pytest.approx([0,5,5,5])


def test_generic_3mf_rejected(project,slicer,tmp_path):
    with zipfile.ZipFile(project,'w') as z:z.writestr('3D/3dmodel.model',MODEL)
    with pytest.raises(ValueError,match='native Bambu'):
        sp.compare_print_candidates(project,tmp_path/'r',[{'name':'fast'}],slicer)


@pytest.mark.parametrize('mutation',[
    lambda entries: entries.update({'Metadata/model_settings.config':b'<not-closed'}),
    lambda entries: entries.update({'3D/3dmodel.model':MODEL.replace('millimeter','inch').encode()}),
    lambda entries: entries.update({'3D/3dmodel.model':MODEL.replace('</build>','<item objectid="2"/></build>').encode()}),
])
def test_unsupported_native_project_rejected_as_value_error_before_outputs(project,slicer,tmp_path,mutation):
    with zipfile.ZipFile(project) as z:
        entries={n:z.read(n) for n in z.namelist()}
    mutation(entries)
    with zipfile.ZipFile(project,'w') as z:
        for name,data in entries.items():z.writestr(name,data)
    with pytest.raises(ValueError):
        sp.compare_print_candidates(project,tmp_path/'results',[{'name':'candidate'}],slicer)
    assert not (tmp_path/'results').exists()


def test_invalid_zip_rejected_as_value_error_before_outputs(project,slicer,tmp_path):
    project.write_bytes(b'not a zip')
    with pytest.raises(ValueError,match='Invalid native Bambu project'):
        sp.compare_print_candidates(project,tmp_path/'results',[{'name':'candidate'}],slicer)
    assert not (tmp_path/'results').exists()


@pytest.mark.parametrize('mutation',[
    lambda xml: xml.replace('objectid="2"', 'objectid="999"'),
    lambda xml: xml.replace('objectid="1"', 'objectid="2"'),
    lambda xml: xml.replace('100 100 5', '100 100'),
    lambda xml: xml.replace('x="10"', 'x="NaN"'),
    lambda xml: '<not-closed',
])
def test_native_geometry_invalid_before_any_process(project,slicer,tmp_path,mutation):
    with zipfile.ZipFile(project) as z: entries={n:z.read(n) for n in z.namelist()}
    entries['3D/3dmodel.model']=mutation(MODEL).encode()
    with zipfile.ZipFile(project,'w') as z:
        for name,data in entries.items():z.writestr(name,data)
    with pytest.raises(ValueError):
        sp.compare_print_candidates(project,tmp_path/'results',[{'name':'candidate'}],slicer)
    assert not (tmp_path/'results').exists()


def test_disk_and_returned_logs_are_bounded(project,slicer,tmp_path):
    report=sp.compare_print_candidates(project,tmp_path/'results',[{'name':'flood'}],slicer,timeout=5)
    entry=report['candidates'][1]
    assert entry['error_type']=='log_limit'
    assert (tmp_path/'results/flood/slicer.log').stat().st_size<=sp.MAX_LOG_BYTES
    assert len(entry['log'])<=32810


@pytest.mark.parametrize('value',[True,False,float('nan'),float('inf'),-1,'3',[],{}])
def test_metrics_reject_non_numbers_and_negative_values(value):
    with pytest.raises(ValueError):sp._positive(value,'metric')


def test_zero_counts_allowed_but_empty_print_not_allowed():
    assert sp._positive(0,'changes')==0
    with pytest.raises(ValueError):sp._positive(0,'prediction',True)



def test_oversized_sliced_archive_rejected_before_decompression(tmp_path,monkeypatch):
    path=tmp_path/'sliced.3mf'
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('Metadata/slice_info.config','x'*10000)
    monkeypatch.setattr(sp,'MAX_ARCHIVE_BYTES',1000)
    with pytest.raises(ValueError,match='oversized'):
        sp._parse_result(path,tmp_path)


def test_oversized_slice_metadata_rejected(tmp_path):
    path=tmp_path/'sliced.3mf'
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('Metadata/slice_info.config','x'*(1024*1024+1))
    with pytest.raises(ValueError,match='Oversized slicing metadata'):
        sp._parse_result(path,tmp_path)


def test_repeated_subassemblies_have_shared_work_budget(project,slicer,tmp_path,monkeypatch):
    with zipfile.ZipFile(project) as z:
        entries={n:z.read(n) for n in z.namelist()}
    repeated=''.join('<object id="%s"><components><component objectid="%s"/><component objectid="%s"/></components></object>'%(i,i-1,i-1) for i in range(3,13))
    xml=MODEL.replace('</resources>',repeated+'</resources>').replace('<item objectid="2"','<item objectid="12"')
    entries['3D/3dmodel.model']=xml.encode()
    with zipfile.ZipFile(project,'w') as z:
        for name,data in entries.items():z.writestr(name,data)
    monkeypatch.setattr(sp,'MAX_ASSEMBLY_OBJECT_VISITS',10)
    with pytest.raises(ValueError,match='Assembly exceeds 10 object visits'):
        sp.compare_print_candidates(project,tmp_path/'results',[{'name':'candidate'}],slicer)
    assert not (tmp_path/'results').exists()


def test_vertex_work_budget_is_shared_between_bounds_passes(project,slicer,tmp_path,monkeypatch):
    monkeypatch.setattr(sp,'MAX_ASSEMBLY_VERTEX_VISITS',7)
    with pytest.raises(ValueError,match='Assembly exceeds 7 vertex visits'):
        sp.compare_print_candidates(project,tmp_path/'results',[{'name':'candidate'}],slicer)
    assert not (tmp_path/'results').exists()


def test_settings_only_3mf_without_native_origin_rejected_before_execution(project,slicer,tmp_path):
    with zipfile.ZipFile(project) as z:
        entries={n:z.read(n) for n in z.namelist()}
    entries['3D/3dmodel.model']=MODEL.replace('<metadata name="Application">BambuStudio-02.08.02.61</metadata>','').encode()
    with zipfile.ZipFile(project,'w') as z:
        for name,data in entries.items():z.writestr(name,data)
    with pytest.raises(ValueError,match='export it from Bambu Studio'):
        sp.compare_print_candidates(project,tmp_path/'results',[{'name':'candidate'}],slicer)
    assert not (tmp_path/'results').exists()
