"""Measured Pareto alternatives with conservative, explicit quality tradeoffs (MIT)."""
import math
from .spec import equivalent, number


def summarize(records, baseline_settings):
    good = []
    alternatives = []
    for row in records:
        m = row.get('metrics', {})
        if row.get('status') != 'success' or row.get('constraint_violations'):
            continue
        if any(isinstance(m.get(k), bool) or not isinstance(m.get(k), (int, float)) or not math.isfinite(m[k]) or m[k] <= 0 for k in ('seconds', 'total_grams')):
            continue
        effective = row.get('effective_settings', {**baseline_settings, **row.get('settings', {})})
        diff = {k: {'before': baseline_settings.get(k), 'after': v} for k, v in effective.items()
                if not equivalent(v, baseline_settings.get(k))}
        risks, notes = 0, []
        for k, d in diff.items():
            lower_strength = k in ('wall_loops', 'top_shell_layers', 'bottom_shell_layers', 'sparse_infill_density')
            try:
                risk = (number(d['after']) < number(d['before']) if lower_strength else
                        number(d['after']) > number(d['before']) if k == 'layer_height' else
                        k not in ('skirt_loops', 'flush_into_infill'))
            except (ValueError, TypeError):
                risk = True
            if risk:
                risks += 1
                notes.append(k + (': reduced section/infill; verify strength' if lower_strength else
                                  ': coarser layers; review finish' if k == 'layer_height' else
                                  ': changed; review print behavior'))
        if row.get('rotation_matrix') is not None or row.get('rotation'):
            risks += 1
            notes.append('Orientation changed; review finish, supports and load direction')
        item = dict(name=row['name'], metrics=m, diff=diff, rotation_matrix=row.get('rotation_matrix'),
                    quality_risk_count=risks, tradeoffs=notes)
        good.append(item)
    if not good:
        return dict(winners=dict(fastest=None, least_material=None, conservative=None), pareto=[], alternatives=[])
    fastest = min(good, key=lambda r: (r['metrics']['seconds'], r['metrics']['total_grams'], r['name']))
    light = min(good, key=lambda r: (r['metrics']['total_grams'], r['metrics']['seconds'], r['name']))
    conservative = min(good, key=lambda r: (r['quality_risk_count'], r['metrics']['seconds'], r['metrics']['total_grams'], r['name']))
    pareto = [r['name'] for r in good if not any(
        q['metrics']['seconds'] <= r['metrics']['seconds'] and q['metrics']['total_grams'] <= r['metrics']['total_grams'] and
        (q['metrics']['seconds'] < r['metrics']['seconds'] or q['metrics']['total_grams'] < r['metrics']['total_grams']) for q in good)]
    for row in (fastest, light, conservative):
        if row['name'] not in [x['name'] for x in alternatives]:
            alternatives.append(row)
    return dict(winners=dict(fastest=fastest['name'], least_material=light['name'], conservative=conservative['name']),
                pareto=sorted(pareto), alternatives=alternatives)
