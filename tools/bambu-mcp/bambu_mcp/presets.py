"""Preset index + `inherits` flattening.

The CLI does not resolve `inherits` for --load-settings/--load-filaments (filament density and
diameter come out missing, so grams read 0), so we flatten the chain ourselves.
"""
import json
import os
import platform
from pathlib import Path

KINDS = ("machine", "process", "filament")


def profiles_dir(binary: str) -> Path | None:
    b = Path(binary).resolve()
    for p in (b.parent.parent / "Resources" / "profiles" / "BBL",   # macOS bundle
              b.parent / "resources" / "profiles" / "BBL",           # win / portable
              b.parent.parent / "resources" / "profiles" / "BBL"):   # linux tarball
        if p.is_dir():
            return p
    return None


def user_dirs() -> list[Path]:
    home = Path.home()
    base = {"Darwin": home / "Library/Application Support/BambuStudio",
            "Windows": Path(os.environ.get("APPDATA", home)) / "BambuStudio"}.get(
        platform.system(), home / ".config/BambuStudio")
    return [d for d in (base / "user").glob("*") if d.is_dir()] if (base / "user").is_dir() else []


def index(roots: list[Path]) -> dict[str, dict[str, Path]]:
    """{kind: {preset name: file}}. First root wins on name clashes."""
    out: dict[str, dict[str, Path]] = {k: {} for k in KINDS}
    for root in roots:
        for kind in KINDS:
            for f in sorted((root / kind).glob("*.json")) if (root / kind).is_dir() else []:
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(data, dict):
                    continue
                name = data.get("name", f.stem)
                if not isinstance(name, str):
                    continue
                out[kind].setdefault(name, f)
    return out


def flatten(path: Path, kind_index: dict[str, Path], _seen: tuple = ()) -> dict:
    """Merge the inherits chain, root first. Child values win."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: preset is not a JSON object")
    parent = data.get("inherits")
    if parent is not None and not isinstance(parent, str):
        raise ValueError(f"{path}: inherits must be a string, got {type(parent).__name__}")
    base: dict = {}
    if parent:
        if parent in _seen:
            raise ValueError(f"inherits cycle at {parent}")
        pf = kind_index.get(parent)
        if pf is None:
            raise ValueError(f"{path}: parent preset {parent!r} not found")
        base = flatten(pf, kind_index, _seen + (parent,))
    base.update({k: v for k, v in data.items() if k != "inherits"})
    return base
