"""Spline declaration validation independent of a FreeCAD process."""
import sys
import types

import pytest
from surface_design import curve


@pytest.mark.parametrize("spec,message", [
    ({"knots":[0,1]}, "together"),
    ({"multiplicities":[3,3]}, "together"),
    ({"knots":[0,1],"multiplicities":[3]}, "match"),
    ({"knots":[0,1],"multiplicities":[2,2]}, "pole count"),
    ({"knots":[1,0],"multiplicities":[3,3]}, "increasing"),
    ({"knots":[0,float('nan')],"multiplicities":[3,3]}, "finite"),
    ({"knots":[0,1],"multiplicities":[3.0,3]}, "integer"),
    ({"periodic":True}, "explicit knots"),
])
def test_invalid_knot_declarations_fail_before_native_build(monkeypatch, spec, message):
    class Spline:
        def buildFromPolesMultsKnots(self, *args):
            pytest.fail("invalid declaration reached native geometry construction")
    monkeypatch.setitem(sys.modules,"Part",types.SimpleNamespace(BSplineCurve=Spline))
    with pytest.raises(ValueError, match=message):
        curve(dict(type="bspline",mode="poles",degree=2,**spec), [object()]*3)
