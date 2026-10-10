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
