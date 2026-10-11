"""Strict sliced-3MF validation, with absent cost buckets kept as null (MIT)."""
import json
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from .toolpath import comment_profile


def _positive(value, field, strictly_positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError('Invalid ' + field)
    value = float(value)
    if not math.isfinite(value) or value < 0 or strictly_positive and value == 0:
        raise ValueError('Invalid ' + field)
    return value


def read_artifacts(path, directory, max_archive_bytes=1024**3, plate=0, strict_single_plate=True):
    result = dict(seconds=0., total_grams=0., outside=False, layers=None, filament_changes=None,
                  model_grams=None, support_grams=None, purge_grams=None, tower_grams=None,
                  feature_seconds=None, slicer_version=None, warning_message='', warnings=[], toolpath_profiles=[])
    ids = []
    found_counts = {'layers': 0, 'filament_changes': 0}
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len({i.filename for i in infos}) != len(infos) or sum(i.file_size for i in infos) > max_archive_bytes:
            raise ValueError('Duplicate or oversized sliced 3MF entries')
        if archive.getinfo('Metadata/slice_info.config').file_size > 1024*1024:
            raise ValueError('Oversized slicing metadata')
        root = ET.fromstring(archive.read('Metadata/slice_info.config'))
        result['slicer_version'] = next((v.get('value') for v in root.findall('header/header_item') if v.get('key') == 'X-BBL-Client-Version'), None)
        plates = root.findall('plate')
        if strict_single_plate and len(plates) != 1:
            raise ValueError('Expected exactly one sliced plate')
        for index, item in enumerate(plates, 1):
            metadata = {m.get('key'): m.get('value') for m in item.findall('metadata')}
            pid = int(metadata.get('index', index))
            if plate not in (0, pid):
                continue
            if pid in ids:
                raise ValueError('Duplicate plate id')
            ids.append(pid)
            outside = metadata.get('outside')
            if outside not in ('true', 'false'):
                raise ValueError('Missing or invalid plate-boundary status')
            result['outside'] |= outside == 'true'
            result['seconds'] += _positive(float(metadata['prediction']), 'prediction', True)
            result['total_grams'] += _positive(float(metadata['weight']), 'weight', True)
            gcode = f'Metadata/plate_{pid}.gcode'
            if gcode not in archive.namelist() or archive.getinfo(gcode).file_size == 0:
                raise ValueError('Missing or empty sliced G-code artifact')
            with archive.open(gcode) as stream:
                header = stream.read(65536).decode('utf-8', errors='replace')
            with archive.open(gcode) as stream:
                result['toolpath_profiles'].append(dict(plate=pid, **comment_profile(stream)))
            for field, pattern in [('layers', r';\s*total layer number\s*:\s*(\d+)'), ('filament_changes', r';\s*total filament change(?: times)?\s*[:=]\s*(\d+)')]:
                match = re.search(pattern, header, re.I)
                if match:
                    result[field] = (result[field] or 0) + int(match.group(1))
                    found_counts[field] += 1
            result['warnings'] += [x.get('msg') for x in item.findall('warning') if x.get('msg')]
    if not ids:
        raise ValueError('No sliced plates for requested plate')
    result['plates'] = len(ids)
    for key in found_counts:
        if found_counts[key] != len(ids):
            result[key] = None
    raw_path = directory / 'result.json'
    if raw_path.exists():
        if raw_path.stat().st_size > 16*1024*1024:
            raise ValueError('Oversized slicer result document')
        raw = json.loads(raw_path.read_text())
        if not isinstance(raw, dict) or isinstance(raw.get('return_code'), bool) or raw.get('return_code') != 0:
            raise ValueError('Slicer result reports failure')
        sliced = raw.get('sliced_plates')
        if not isinstance(sliced, list) or not sliced:
            raise ValueError('Invalid sliced_plates')
        if any(not isinstance(p, dict) for p in sliced):
            raise ValueError('Invalid sliced plate')
        if strict_single_plate and len(sliced) != 1:
            raise ValueError('Invalid sliced_plates')
        selected = [p for p in sliced if plate in (0, p.get('id'))]
        if len(selected) != len(ids) or any(p.get('id') not in ids for p in selected):
            raise ValueError('Sliced JSON and artifact plate counts disagree')
        if all('filament_change_times' in p for p in selected):
            counts = [_positive(p['filament_change_times'], 'filament changes') for p in selected]
            count = sum(counts)
            if any(int(v) != v for v in counts):
                raise ValueError('Invalid filament changes')
            result['filament_changes'] = int(count)
        features = {}
        for p in selected:
            f = p.get('feature_type_times')
            if f is not None:
                if not isinstance(f, dict):
                    raise ValueError('Invalid feature time breakdown')
                for k, v in f.items():
                    features[k] = features.get(k, 0) + _positive(v, 'feature time')
            message = p.get('warning_message', '')
            if message:
                result['warnings'].append(message)
        result['feature_seconds'] = features if all(p.get('feature_type_times') is not None for p in selected) else None
    result['warning_message'] = '; '.join(result['warnings'])
    # main_used_g includes support; never label it model-only mass.
    return result
