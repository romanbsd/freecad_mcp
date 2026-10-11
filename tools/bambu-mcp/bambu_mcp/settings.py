"""Resolve project/preset settings once; preserve the same preset inputs for slicing."""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import tempfile

from print_pipeline_core.spec import merge_presets


@dataclass
class SettingsContext:
    settings: dict
    objects: dict
    loaded: list
    errors: list

    def effective(self, variant):
        settings = {**self.settings, **variant.overrides}
        objects = {oid: {**settings, **values, **variant.object_overrides.get(oid, {})}
                   for oid, values in self.objects.items()}
        for oid, values in variant.object_overrides.items():
            objects.setdefault(oid, {**settings, **values})
        return settings, objects


def resolve_settings(package, loader, machine=None, process=None, filaments=None, *, strict=True):
    selected = [('machine', machine), ('process', process)]
    selected += [('filament', name) for name in filaments or []]
    loaded, errors = [], []
    for kind, name in selected:
        if name:
            try:
                loaded.append((kind, loader(kind, name)))
            except (ValueError, OSError) as exc:
                if strict:
                    raise
                errors.append(f'{kind} preset {name!r}: {exc}')
    return SettingsContext(merge_presets(package.project_settings(), loaded),
                           package.object_settings(), loaded, errors)


def snapshot_presets(loaded, home):
    """Content-addressed flattened presets shared by cache identity and CLI inputs."""
    selected = {'machine': None, 'process': None, 'filaments': []}
    if not loaded:
        return selected
    directory = Path(home) / 'preset_snapshots'
    directory.mkdir(parents=True, exist_ok=True)
    for kind, data in loaded:
        encoded = json.dumps(data, sort_keys=True).encode()
        path = directory / (hashlib.sha256(encoded).hexdigest() + '.json')
        if not path.exists() or path.read_bytes() != encoded:
            with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
                handle.write(encoded)
                temporary = Path(handle.name)
            try:
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        if kind == 'filament':
            selected['filaments'].append(str(path))
        else:
            selected[kind] = str(path)
    return selected
