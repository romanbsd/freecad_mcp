"""Versioned print requirements; geometry requirements remain explicit review items (MIT)."""
import math

DEFAULTS = dict(schema_version=1, functional=True, machine_preset='', process_preset='',
                filament_presets=[], min_wall_loops=2, min_top_layers=3, min_bottom_layers=2,
                min_infill_pct=10., max_layer_height=None, min_wall_mm=None,
                dimensional_tolerance_mm=None, protected_surfaces=[], locked_keys=[],
                locked_objects=[], require_brim=False, keep_supports='auto',
                max_time_increase_pct=10., max_material_increase_pct=10., nozzle_mm=None)


def number(value):
    if isinstance(value, (list, tuple)):
        if not value:
            raise ValueError('Expected a nozzle/setting value')
        values = [number(v) for v in value]
        if any(v != values[0] for v in values):
            raise ValueError('Mixed nozzle/setting values are not supported')
        return values[0]
    if isinstance(value, bool):
        raise ValueError('Boolean is not a numeric setting')
    n = float(str(value).rstrip('%'))
    if not math.isfinite(n):
        raise ValueError('Setting must be finite')
    return n


def equivalent(a, b):
    try:
        return number(a) == number(b)
    except (ValueError, TypeError):
        return str(a) == str(b)


def validate_preset_fields(raw):
    """Validate names before callers load files or iterate filament slots."""
    for key in ('machine_preset', 'process_preset'):
        if not isinstance(raw.get(key, ''), str):
            raise ValueError(key + ' must be a name or path')
    values = raw.get('filament_presets', [])
    if not isinstance(values, list) or not all(isinstance(x, str) and x for x in values):
        raise ValueError('filament_presets must contain nonempty strings')


def resolve_spec(raw, settings):
    if not isinstance(raw, dict) or set(raw) - (set(DEFAULTS) | {'bounds', 'review_requirements'}):
        raise ValueError('Unknown print specification field or invalid specification')
    defaults = {k: list(v) if isinstance(v, list) else v for k, v in DEFAULTS.items()}
    if raw.get('functional') is False:
        defaults.update(min_wall_loops=1, min_top_layers=1, min_bottom_layers=1, min_infill_pct=5.)
    spec = {**defaults, **{k: list(v) if isinstance(v, list) else v for k, v in raw.items() if k in DEFAULTS}}
    if type(spec['schema_version']) is not int or spec['schema_version'] != 1:
        raise ValueError('Unsupported print specification version')
    for key in ('functional', 'require_brim'):
        if type(spec[key]) is not bool:
            raise ValueError(key + ' must be boolean')
    validate_preset_fields(spec)
    for key in ('locked_keys', 'protected_surfaces'):
        if not isinstance(spec[key], list) or not all(isinstance(x, str) and x for x in spec[key]):
            raise ValueError(key + ' must contain nonempty strings')
    if not isinstance(spec['locked_objects'], list) or not all(isinstance(x, (int, str)) and not isinstance(x, bool) and str(x).isdigit() for x in spec['locked_objects']):
        raise ValueError('locked_objects must contain object ids')
    if spec['keep_supports'] not in ('auto', 'required', 'forbidden'):
        raise ValueError('Invalid keep_supports')
    for key in ('min_wall_loops', 'min_top_layers', 'min_bottom_layers'):
        if type(spec[key]) is not int or not 0 <= spec[key] <= 30:
            raise ValueError(key + ' must be an integer in 0..30')
    for key in ('min_infill_pct', 'max_time_increase_pct', 'max_material_increase_pct'):
        spec[key] = number(spec[key])
        if not 0 <= spec[key] <= 100:
            raise ValueError(key + ' must be in 0..100')
    for key in ('min_wall_mm', 'dimensional_tolerance_mm', 'max_layer_height', 'nozzle_mm'):
        if spec[key] is not None:
            spec[key] = number(spec[key])
            if spec[key] <= 0:
                raise ValueError(key + ' must be positive')
    effective_nozzle = settings.get('nozzle_diameter')
    nozzle = number(effective_nozzle) if effective_nozzle is not None else spec['nozzle_mm']
    if nozzle is None or not 0.1 <= nozzle <= 2:
        raise ValueError('Resolve a machine/nozzle preset or specify nozzle_mm')
    if spec['nozzle_mm'] is not None and effective_nozzle is not None and not equivalent(nozzle, spec['nozzle_mm']):
        raise ValueError('nozzle_mm disagrees with resolved printer settings')
    spec['nozzle_mm'] = nozzle
    lo = number(settings.get('min_layer_height', nozzle * .25))
    hi = number(settings.get('max_layer_height', nozzle * .75))
    if not 0 < lo <= hi:
        raise ValueError('Invalid resolved printer layer-height limits')
    hi = min(hi, nozzle * .75, spec['max_layer_height'] or hi)
    if hi < lo:
        raise ValueError('Requested layer-height limit is below the printer minimum')
    spec['max_layer_height'] = hi
    spec['bounds'] = dict(min_layer_height=lo, max_layer_height=hi, nozzle_mm=nozzle)
    if spec['min_wall_mm'] is not None:
        width = number(settings.get('line_width', nozzle))
        if width < 0:
            raise ValueError('Invalid extrusion width')
        explicit_width = 'line_width' in settings and width > 0
        if width == 0:  # auto width; use an explicitly labeled proxy, not a claimed resolved wall width
            width = nozzle
        spec['min_wall_loops'] = max(spec['min_wall_loops'], math.ceil(spec['min_wall_mm'] / width))
        spec['bounds']['wall_width_source'] = 'line_width' if explicit_width else 'nozzle_proxy'
    spec['review_requirements'] = []
    for key in ('min_wall_mm', 'dimensional_tolerance_mm', 'protected_surfaces'):
        if spec[key]:
            spec['review_requirements'].append({'requirement': key, 'value': list(spec[key]) if isinstance(spec[key], list) else spec[key],
                                                'verification': 'geometry_or_preview_required'})
    return spec


def setting_breaches(effective, baseline, floors, functional, minimum_layer, maximum_layer,
                     *, strict=True, tolerance=0):
    """Shared numeric/pattern checks; callers supply limits and format their API messages."""
    for key, value in effective.items():
        if equivalent(value, baseline.get(key)):
            continue
        if key in floors or key == 'layer_height':
            try:
                numeric = number(value)
            except (ValueError, TypeError):
                if strict:
                    yield key, 'unresolved'
                continue
            if key in floors and numeric < floors[key]:
                yield key, 'floor'
            if key == 'layer_height' and ((minimum_layer is not None and numeric < minimum_layer)
                                         or numeric > maximum_layer + tolerance):
                yield key, 'layer'
        if key == 'sparse_infill_pattern' and functional and value == 'lightning':
            yield key, 'lightning'


def violations(effective, baseline, spec, rotation=False):
    """Reject new breaches; unchanged baseline floors remain the user's explicit choice."""
    bad = []
    for key in spec['locked_keys']:
        if not equivalent(effective.get(key), baseline.get(key)):
            bad.append(key + ' is locked')
    if rotation and spec['locked_objects']:
        bad.append('whole-assembly rotation conflicts with locked_objects')
    floors = {'wall_loops': spec['min_wall_loops'], 'top_shell_layers': spec['min_top_layers'],
              'bottom_shell_layers': spec['min_bottom_layers'], 'sparse_infill_density': spec['min_infill_pct']}
    messages = {'floor': ' below minimum', 'unresolved': ' is unresolved',
                'lightning': 'lightning infill is forbidden for functional parts',
                'layer': 'layer_height outside resolved printer/spec limits'}
    for key, reason in setting_breaches(effective, baseline, floors, spec['functional'],
                                      spec['bounds']['min_layer_height'], spec['max_layer_height']):
        bad.append(messages[reason] if reason in ('lightning', 'layer') else key + messages[reason])
    support = effective.get('enable_support')
    if spec['keep_supports'] != 'auto':
        wanted = spec['keep_supports'] == 'required'
        if support is None or str(support).lower() not in (('1', 'true') if wanted else ('0', 'false')):
            bad.append('enable_support conflicts with keep_supports')
    if spec['require_brim'] and effective.get('brim_type') not in ('outer_only', 'inner_only', 'outer_and_inner', 'brim_ears'):
        bad.append('require_brim needs an explicit brim type (auto_brim is not a guarantee)')
    return bad


def metric_violations(metrics, baseline, spec):
    return [name + ' exceeds allowed increase' for name, field, cap in
            [('time', 'seconds', 'max_time_increase_pct'), ('material', 'total_grams', 'max_material_increase_pct')]
            if baseline and metrics[field] > baseline[field] * (1 + spec[cap] / 100)]


def merge_presets(stored, loaded):
    """Resolve machine/process and per-slot filament arrays without collapsing slots."""
    effective = dict(stored)
    filaments = []
    for kind, data in loaded:
        if kind == 'filament':
            filaments.append(data)
        else:
            effective.update(data)
    if filaments:
        for key in set().union(*(set(f) for f in filaments)):
            values = [f.get(key) for f in filaments]
            if all(isinstance(v, list) and len(v) == 1 for v in values):
                effective[key] = [v[0] for v in values]
            elif all(v is not None for v in values):
                # Preset metadata is not an effective scalar printer setting.
                if key not in ('name', 'inherits', 'from', 'setting_id', 'filament_id'):
                    effective[key] = values[0] if all(v == values[0] for v in values) else values
    return effective
