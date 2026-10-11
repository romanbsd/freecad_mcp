"""Bounded settings and proper rigid rotations for native projects (MIT)."""
import math,re
ALLOWED_SETTINGS = {
    'layer_height': (0.05, 0.6), 'wall_loops': (1, 20),
    'sparse_infill_density': (0, 100), 'enable_support': None,
    'support_type': ('normal(auto)', 'tree(auto)', 'normal(manual)', 'tree(manual)'),
    'support_threshold_angle': (0, 90), 'support_base_pattern_spacing': (0.1, 20),
    'top_shell_layers': (0, 30), 'top_shell_thickness': (0, 10),
    'top_color_penetration_layers': (0, 30), 'support_on_build_plate_only': None,
}
_INTEGER = {'wall_loops', 'top_shell_layers', 'top_color_penetration_layers'}



def _numeric(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _validate(candidates):
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 12:
        raise ValueError('candidates must contain 1..12 entries')
    names = {'baseline'}
    result = []
    for candidate in candidates:
        if not isinstance(candidate, dict) or set(candidate) - {'name', 'settings', 'rotation', 'rotation_matrix'}:
            raise ValueError('Unsupported candidate fields')
        name = candidate.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name) or name in names:
            raise ValueError('Candidate names must be unique safe names, excluding baseline')
        names.add(name)
        settings = candidate.get('settings', {})
        if not isinstance(settings, dict):
            raise ValueError('settings must be a dictionary')
        for key, value in settings.items():
            if key not in ALLOWED_SETTINGS:
                raise ValueError('Unsupported print setting: ' + key)
            constraint = ALLOWED_SETTINGS[key]
            if constraint is None:
                valid = isinstance(value, bool) or isinstance(value, int) and value in (0, 1) or value in ('0', '1')
            elif key == 'support_type':
                valid = value in constraint
            else:
                valid = _numeric(value) and constraint[0] <= value <= constraint[1]
                if key in _INTEGER:
                    valid = valid and int(value) == value
            if not valid:
                raise ValueError('Invalid value for ' + key)
        if 'rotation' in candidate and 'rotation_matrix' in candidate:
            raise ValueError('Specify only one rotation representation')
        matrix = None
        if 'rotation' in candidate:
            angles = candidate['rotation']
            if not isinstance(angles, (list, tuple)) or len(angles) != 3 or not all(_numeric(v) for v in angles):
                raise ValueError('rotation requires three finite angles in degrees')
            x, y, z = (math.radians(v) for v in angles)
            sx, cx, sy, cy, sz, cz = math.sin(x), math.cos(x), math.sin(y), math.cos(y), math.sin(z), math.cos(z)
            matrix = [[cz*cy, cz*sy*sx-sz*cx, cz*sy*cx+sz*sx], [sz*cy, sz*sy*sx+cz*cx, sz*sy*cx-cz*sx], [-sy, cy*sx, cy*cx]]
        elif 'rotation_matrix' in candidate:
            matrix = candidate['rotation_matrix']
            if not isinstance(matrix, (list, tuple)) or len(matrix) != 3 or any(not isinstance(row, (list, tuple)) or len(row) != 3 or not all(_numeric(v) for v in row) for row in matrix):
                raise ValueError('rotation_matrix requires a finite 3x3 matrix')
        if matrix is not None:
            for i in range(3):
                for j in range(3):
                    if abs(sum(matrix[i][k]*matrix[j][k] for k in range(3)) - (i == j)) > 1e-6:
                        raise ValueError('rotation_matrix must be orthonormal')
            det = sum(matrix[0][i]*(matrix[1][(i+1)%3]*matrix[2][(i+2)%3]-matrix[1][(i+2)%3]*matrix[2][(i+1)%3]) for i in range(3))
            if abs(det - 1) > 1e-6:
                raise ValueError('rotation_matrix must not mirror or scale')
        result.append({'name': name, 'settings': dict(settings), 'rotation_matrix': matrix})
    return result
