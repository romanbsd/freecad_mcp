"""Pure validation is available without loading the FreeCAD runtime."""
import math
import pytest
from printability import _parameters, _uv_samples


@pytest.mark.parametrize("kwargs", [
    {"min_wall": 0}, {"min_wall": float("nan")}, {"min_wall": True},
    {"samples_per_face": 0}, {"samples_per_face": 1.5}, {"max_samples": 10001},
    {"build_direction": [0, 0, 0]}, {"build_direction": [0, float("inf"), 1]},
    {"build_direction": [1, 2]}, {"overhang_angle": 91}, {"overhang_angle": -1},
])
def test_reject_invalid_settings(kwargs):
    settings = dict(min_wall=.8, samples_per_face=5, build_direction=[0,0,1],
                    overhang_angle=45, max_samples=2000)
    settings.update(kwargs)
    with pytest.raises(ValueError):
        _parameters(**settings)


def test_build_direction_normalizes():
    assert _parameters(.8, 5, [1, 0, 1], 45, 100)[2] == pytest.approx(
        [1/math.sqrt(2), 0, 1/math.sqrt(2)])


def test_uv_samples_are_unique_bounded_and_center_first():
    class Face:
        ParameterRange = (0, 10, -2, 2)
    for count in (1, 2, 5, 10, 26, 50):
        samples = list(_uv_samples(Face(), count))
        assert samples[0] == (5, 0)
        assert len(samples) == count
        assert len(set(samples)) == len(samples)
        assert all(0 < u < 10 and -2 < v < 2 for u,v in samples)


from printability import _cell_components, _layer_events, _raster_section, analyze_layers


def test_floating_branch_connected_higher_up_is_detected_at_its_start():
    # A branch can be part of one eventual solid but have no printable ancestry.
    previous = {(0,y) for y in range(4)}
    current = previous | {(5,y) for y in range(4)}
    assert _layer_events(current, previous, 1) == [
        ("floating_layer_island", {(5,y) for y in range(4)})]


def test_45_degree_diagonal_has_previous_layer_support():
    previous = {(x,y) for x in range(3) for y in range(4)}
    current = {(x+1,y) for x,y in previous}
    assert _layer_events(current, previous, 1) == []


def test_bridge_between_two_anchors_is_not_a_floating_island():
    previous = {(0,0),(0,1),(9,0),(9,1)}
    current = {(x,y) for x in range(10) for y in range(2)}
    events = _layer_events(current, previous, 1)
    assert [kind for kind,_cells in events] == ["potential_bridge"]
    assert (5,0) in events[0][1]


def test_single_anchored_cantilever_is_reported_as_expansion():
    assert _layer_events({(x,0) for x in range(10)}, {(0,0)}, 1)[0][0] == "unsupported_layer_expansion"


def test_raster_keeps_holes_and_does_not_join_diagonal_contact():
    outer = [(0,0),(5,0),(5,5),(0,5)]
    hole = [(1,1),(4,1),(4,4),(1,4)]
    cells = _raster_section([outer,hole], 1, (0,0), lambda n: None)
    assert len(cells) == 16
    assert (2,2) not in cells
    assert len(_cell_components({(0,0),(1,1)})) == 2


@pytest.fixture
def native_layer_stub(monkeypatch):
    """Exercise section/budget orchestration without a GUI or OCC installation."""
    import sys
    from types import SimpleNamespace
    class Vector:
        def __init__(self,x=0,y=0,z=0): self.x,self.y,self.z=x,y,z
    rotations = []
    class Placement:
        def __init__(self,*args): pass
        def multiply(self,other): return self
        def inverse(self): return self
        def multVec(self,p): return p
    def rotation(first,second):
        rotations.append([first.x,first.y,first.z])
        return None
    monkeypatch.setitem(sys.modules, 'FreeCAD', SimpleNamespace(Vector=Vector, Placement=Placement, Rotation=rotation))
    class Wire:
        Length = 12
        def __init__(self,poly): self.poly=poly
        def isClosed(self): return True
        def discretize(self,**kw): return [Vector(x,y,0) for x,y in self.poly]
    class Shape:
        ShapeType = "Solid"
        def isClosed(self): return True
        Solids = [1]
        def __init__(self): self.Placement = Placement()
        BoundBox = SimpleNamespace(XMin=0,YMin=0,ZMin=0,ZMax=2)
        def isNull(self): return False
        def isValid(self): return True
        def copy(self): return self
        def slice(self,normal,z): return [Wire([(0,0),(3,0),(3,3),(0,3)])]
    return Shape(), rotations


def test_ordinary_base_layer_screen_complete(native_layer_stub):
    shape,_rotations = native_layer_stub
    result = analyze_layers(shape, layer_height=.2, xy_resolution=1)
    assert result['complete'] and result['examined_layers'] == 10
    assert result['status'] == 'PASS' and result['findings'] == []


def test_layer_and_work_limits_are_explicit_incomplete(native_layer_stub):
    shape,_ = native_layer_stub
    result = analyze_layers(shape, max_layers=2)
    assert result['complete'] is False and result['stop_reason'] == 'max_layers'
    assert result['examined_layers'] == 2
    result = analyze_layers(shape, max_work=5)
    assert result['complete'] is False and result['stop_reason'] == 'max_work'
    assert result['status'] == 'WARN'


def test_layer_time_limit_is_explicit_incomplete(native_layer_stub, monkeypatch):
    shape,_ = native_layer_stub
    import time
    ticks = iter([0,2,2])
    monkeypatch.setattr(time, 'monotonic', lambda: next(ticks))
    result = analyze_layers(shape, time_budget=1)
    assert result['stop_reason'] == 'time_budget' and not result['complete']


def test_rotated_build_direction_reaches_native_rotation(native_layer_stub):
    shape,rotations = native_layer_stub
    result = analyze_layers(shape, build_direction=[0,-2,0])
    assert rotations == [[0,-1,0]]
    assert result['build_direction'] == [0,-1,0]


def test_section_failures_are_inconclusive(native_layer_stub):
    shape,_ = native_layer_stub
    def broken(normal,z): raise RuntimeError('bad section')
    shape.slice = broken
    result = analyze_layers(shape)
    assert result['status'] == 'WARN' and not result['complete']
    assert result['stop_reason'].startswith('section_evaluation_failed: RuntimeError')


@pytest.mark.parametrize('options', [
    {'layer_height':0}, {'xy_resolution':float('nan')}, {'max_layers':0},
    {'max_work':True}, {'time_budget':121}, {'max_findings':1001}])
def test_invalid_layer_options_rejected(native_layer_stub, options):
    with pytest.raises(ValueError):
        analyze_layers(native_layer_stub[0], **options)


def test_native_target_copies_shape_and_applies_global_placement(monkeypatch):
    import importlib
    import sys
    from types import SimpleNamespace
    copied = SimpleNamespace(Placement='local')
    shape = SimpleNamespace(copy=lambda: copied, isNull=lambda: False)
    obj = SimpleNamespace(Shape=shape, getGlobalPlacement=lambda: 'global')
    doc = SimpleNamespace(getObject=lambda name: obj if name == 'Final' else None)
    app = SimpleNamespace(listDocuments=lambda: {'D':doc}, getDocument=lambda name: doc)
    monkeypatch.setitem(sys.modules, 'FreeCAD', app)
    monkeypatch.delitem(sys.modules, 'parametric', raising=False)
    module = importlib.import_module('parametric')
    import printability
    received = []
    monkeypatch.setattr(printability, 'analyze_shape', lambda *args: received.append(args) or {'status':'PASS'})
    try:
        result = module.op_inspect_printability(document_name='D',object_name='Final',
                                                layer_sampling={'max_layers':5})
        assert result['status'] == 'PASS'
        assert received[0][0] is copied and copied.Placement == 'global'
        assert received[0][-1] == {'max_layers':5}
        with pytest.raises(ValueError,match='choose'):
            module.op_inspect_printability(component_id='component://D/C',document_name='D',object_name='Final')
        with pytest.raises(ValueError,match='both required'):
            module.op_inspect_printability(document_name='D')
        with pytest.raises(ValueError,match='not open'):
            module.op_inspect_printability(document_name='Missing',object_name='Final')
        with pytest.raises(ValueError,match='unknown or empty'):
            module.op_inspect_printability(document_name='D',object_name='Missing')
        with pytest.raises(ValueError,match='are required'):
            module.op_inspect_printability()
        with pytest.raises(ValueError,match='are required'):
            module.op_inspect_printability(feature_id='Final')
    finally:
        sys.modules.pop('parametric', None)


@pytest.mark.parametrize('options', [{'pitch':.5}, {'shape':object()},
                                  {'build_direction':[0,0,1]}, {'max_layers':2, 1:3}, []])
def test_unknown_layer_options_are_input_errors(options):
    from printability import analyze_shape
    with pytest.raises(ValueError,match='layer_sampling'):
        analyze_shape(None, layer_sampling=options)


def test_partial_top_layer_samples_actual_midpoint(native_layer_stub):
    shape,_ = native_layer_stub
    shape.BoundBox.ZMax = 1.1
    native_slice, positions = shape.slice, []
    def recording(normal,z):
        positions.append(z)
        return native_slice(normal,z)
    shape.slice = recording
    result = analyze_layers(shape,layer_height=.2,xy_resolution=1)
    assert result['complete'] and result['expected_layers'] == 6
    assert positions[-1] == pytest.approx(1.05)


@pytest.mark.parametrize('missing_wires', [True,False])
def test_unresolved_base_anchors_do_not_claim_floating_island(native_layer_stub,missing_wires):
    shape,_ = native_layer_stub
    native_slice = shape.slice
    def thin_base(normal,z):
        wires = native_slice(normal,z)
        if z < .4:
            if missing_wires:
                return []
            wires[0].poly = [(0,0),(.1,0),(.1,.1),(0,.1)]
        return wires
    shape.slice = thin_base
    result = analyze_layers(shape,layer_height=.2,xy_resolution=1)
    assert result['complete'] and result['status'] == 'WARN'
    assert result['unresolved_empty_raster_layers'] == 2
    assert result['unresolved_anchor_layers'] == 1
    assert 'floating_layer_island' not in result['finding_counts']


def test_native_discretization_rejected_before_large_allocation(native_layer_stub):
    shape,_ = native_layer_stub
    native_slice = shape.slice
    def huge_section(normal,z):
        wire = native_slice(normal,z)[0]
        wire.Length = 1000000
        def forbidden(**kwargs):
            pytest.fail('must budget before native discretization')
        wire.discretize = forbidden
        return [wire]
    shape.slice = huge_section
    result = analyze_layers(shape,xy_resolution=.05)
    assert not result['complete'] and result['stop_reason'] == 'section_point_limit'
    with pytest.raises(ValueError,match='at least'):
        analyze_layers(shape,xy_resolution=.000001)
