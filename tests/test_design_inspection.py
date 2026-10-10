import pytest
from design_inspection import calibration
from surface_design import coons_net


def test_calibration_uses_uniform_scale_and_y_flip():
    project, scale = calibration([[0,0],[10,0]], [[20,100],[120,100]])
    assert scale == 10
    assert project([5,3]) == pytest.approx([70,70])
    rotated, scale = calibration([[0,0],[10,0]], [[20,100],[20,200]])
    assert rotated([5,3]) == pytest.approx([50,150])


def test_calibration_rejects_coincident_anchors():
    with pytest.raises(ValueError, match="distinct"):
        calibration([[0,0],[0,0]], [[1,2],[3,4]])


@pytest.mark.parametrize("model,pixels", [
    (None, [[0,0],[10,0]]),
    ([[0,0],[None,0]], [[0,0],[10,0]]),
    ([[0,0],None], [[0,0],[10,0]]),
    ([[0,0],[10,0]], None),
    ([[0,0],[10,0]], [[0,0],[{},0]]),
    ([[0,0],[10,0]], [[0,0],["not a number",0]]),
    ([[0],[10,0]], [[0,0],[10,0]]),
    ([[0,0],[float('inf'),0]], [[0,0],[10,0]]),
])
def test_calibration_reports_malformed_anchors_consistently(model, pixels):
    with pytest.raises(ValueError, match="two finite 2D model and pixel anchors"):
        calibration(model, pixels)


def test_calibration_preserves_numeric_string_anchor_support():
    project, scale = calibration([["0","0"],["10","0"]], [["20","100"],["120","100"]])
    assert scale == 10
    assert project([5,3]) == pytest.approx([70,70])


def test_coons_patch_contains_guides_and_rounded_interior():
    bottom = [[0,0,0],[5,0,0],[10,0,0]]
    top = [[0,10,0],[5,10,0],[10,10,0]]
    left = [[0,0,0],[0,5,6],[0,10,0]]
    right = [[10,0,0],[10,5,6],[10,10,0]]
    net = coons_net(bottom,top,left,right)
    assert [row[0] for row in net] == bottom
    assert [row[-1] for row in net] == top
    assert net[0] == left
    assert net[-1] == right
    # Quadratic Bernstein weights at u=v=.5: center height is 3 mm.
    height = sum(net[i][j][2]*[.25,.5,.25][i]*[.25,.5,.25][j] for i in range(3) for j in range(3))
    assert height == 3
    with pytest.raises(ValueError, match="corners"):
        coons_net(bottom,top,[[1,0,0],[0,5,6],[0,10,0]],right)
