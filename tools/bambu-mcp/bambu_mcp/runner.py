"""Run the Bambu Studio CLI for one variant and parse result.json. Results are cached on disk."""
import hashlib
import json
import zipfile
import xml.etree.ElementTree as ET
import os
import platform
import subprocess
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field, PrivateAttr

from . import presets
from .threemf import Package
from print_pipeline_core.process import run_bounded
from print_pipeline_core.artifacts import ArtifactResult, read_artifact_result
from .settings import resolve_settings

HOME = Path(os.environ.get("BAMBU_MCP_HOME", Path.home() / ".bambu-mcp"))


def find_binary() -> str:
    cands = [os.environ.get("BAMBU_STUDIO_BIN", "")]
    s = platform.system()
    if s == "Darwin":
        cands.append("/Applications/BambuStudio.app/Contents/MacOS/BambuStudio")
    elif s == "Windows":
        cands.append(str(Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Bambu Studio/bambu-studio.exe"))
    else:
        cands += ["/usr/bin/bambu-studio", "/usr/local/bin/bambu-studio"]
    for c in cands:
        if c and Path(c).is_file():
            return c
    raise FileNotFoundError("Bambu Studio binary not found; set BAMBU_STUDIO_BIN")


@dataclass
class Variant:
    """Everything that changes a slice. Hashable via .key()."""
    overrides: dict = field(default_factory=dict)         # global print keys -> CLI strings
    object_overrides: dict = field(default_factory=dict)  # {obj id: {key: str}}  (3MF patch)
    object_rotation: dict = field(default_factory=dict)   # {obj id: (rx, ry, rz) deg}
    flags: dict = field(default_factory=dict)             # orient / arrange / allow_rotations ...
    plate: int = 0

    def copy(self):
        return Variant(dict(self.overrides), {k: dict(v) for k, v in self.object_overrides.items()},
                       dict(self.object_rotation), dict(self.flags), self.plate)

    def key(self) -> str:
        return json.dumps([self.overrides, self.object_overrides, self.object_rotation,
                           self.flags, self.plate], sort_keys=True, default=str)


class Metrics(BaseModel):
    """Outcome of one slice. Times are the slicer's own print-time prediction."""
    ok: bool = Field(description="false if slicing failed; see `error`")
    run_id: str = Field("", description="cache key; pass to `export` / `compare`")
    time_s: float = Field(0.0, description="predicted print time, seconds (sum of selected plates)")
    filament_g: float = Field(0.0, description="filament used, grams (sum over plates and filaments)")
    role_time_s: dict[str, float] = Field(default_factory=dict,
                                          description="print time per extrusion role (walls, infill, support...)")
    toolpath_profiles: list[dict] = Field(default_factory=list,
                                        description="bounded per-plate Bambu comment diagnostics; not mass or motion simulation")
    warnings: list[str] = Field(default_factory=list, description="slicer warnings")
    error: str = Field("", description="failure reason when ok=false")
    plates: int = Field(0, description="number of plates included")
    outside: bool = Field(False, description="selected sliced artifact is outside its build plate")
    out_3mf: str = Field("", description="sliced 3MF path (reopenable in Bambu Studio)")

    _artifact_result: ArtifactResult | None = PrivateAttr(default=None)

    def comparison_metrics(self):
        """Reuse validated metrics without exposing internal plate counts in MCP schemas."""
        if self._artifact_result is None:
            raise ValueError('Missing validated sliced artifact')
        return self._artifact_result.single_plate_metrics()


def parse_result(j: dict, plate: int = 0) -> Metrics:
    """result.json -> Metrics. plate 0 sums all plates."""
    if j.get("return_code", -1) != 0:
        return Metrics(ok=False, error=f"code {j.get('return_code')}: {j.get('error_string', '')}")
    plates = [p for p in j.get("sliced_plates", []) if plate in (0, p.get("id"))]
    if not plates:
        return Metrics(ok=False, error="no sliced plates in result.json")
    roles: dict = {}
    for p in plates:
        for k, v in (p.get("feature_type_times") or {}).items():
            roles[k] = roles.get(k, 0) + v
    return Metrics(
        ok=True,
        time_s=sum(p.get("total_predication", 0) for p in plates),
        filament_g=round(sum(f.get("total_used_g", 0) for p in plates for f in p.get("filaments", [])), 3),
        role_time_s=roles,
        warnings=[w for p in plates for w in ([p["warning_message"]] if p.get("warning_message") else [])],
        plates=len(plates),
    )


class Runner:
    def __init__(self, binary: str | None = None, home: Path = HOME):
        self.binary = str(Path(binary or find_binary()).expanduser().resolve())
        self.home = Path(home).expanduser().resolve()
        self.roots = [d for d in [presets.profiles_dir(self.binary)] if d] + presets.user_dirs()
        self._index = None
        self._version = None
        # ponytail: one Lock per distinct variant, never pruned (tens of bytes each); prune if a server lives for days
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        (self.home / "runs").mkdir(parents=True, exist_ok=True)

    @property
    def index(self):
        if self._index is None:
            self._index = presets.index(self.roots)
        return self._index

    def version(self) -> str:
        """First line of --help, cached. Run from the cache dir: the CLI drops a result.json in its cwd."""
        if self._version is None:
            try:
                with tempfile.TemporaryDirectory(prefix='version-', dir=self.home) as td:
                    code, failure, captured, elapsed = run_bounded([self.binary, '--help'], Path(td), 30, 1024*1024)
                if failure:
                    self._version = f'? ({failure})'
                else:
                    self._version = next((l for l in captured.splitlines()
                                          if l.startswith('BambuStudio-')), '?')
            except (OSError, subprocess.SubprocessError) as e:
                self._version = f"? ({e})"
        return self._version

    def argv(self, variant: Variant, in_file: str, out_dir: Path, settings: list[str],
             filaments: list[str], timeout_s: int) -> list[str]:
        a = [self.binary, "--debug", "1", "--outputdir", str(out_dir), "--slice", str(variant.plate),
             # no --min-save: it strips meshes, leaving a 3MF Studio cannot reopen
             "--export-3mf", "out.3mf", "--mstpp", str(timeout_s)]
        if settings:
            a += ["--load-settings", ";".join(settings)]
        if filaments:
            a += ["--load-filaments", ";".join(filaments)]
        f = variant.flags
        for k in ("orient", "arrange"):
            if k in f:
                a.append(f"--{k}={f[k]}")
        for k in ("allow_rotations", "avoid_extrusion_cali_region", "allow_multicolor_oneplate", "ensure_on_bed"):
            if f.get(k):
                a.append("--" + k.replace("_", "-"))
        # `--key=value` is the only accepted form for print keys; `--key value` is rejected.
        for k, v in sorted(variant.overrides.items()):
            a.append(f"--{k.replace('_', '-')}={v}")
        a.append(in_file)
        return a

    def _load_preset(self, kind: str, name_or_path: str) -> dict:
        """Flattened preset (inherits resolved) by name or file path."""
        p = Path(name_or_path)
        idx = self.index[kind]
        if not p.is_file():
            if name_or_path not in idx:
                raise ValueError(f"{kind} preset {name_or_path!r} not found")
            p = idx[name_or_path]
        return presets.flatten(p, idx)

    def _flatten(self, kind: str, name_or_path: str, dest: Path) -> str:
        out = dest / f"{kind}_{abs(hash(name_or_path))}.json"
        out.write_text(json.dumps(self._load_preset(kind, name_or_path)), encoding="utf-8")
        return str(out)

    def effective_settings(self, source, variant, machine=None, process=None, filaments=None):
        context = resolve_settings(Package(source), self._load_preset, machine, process, filaments)
        return context.effective(variant)

    def evaluate(self, source: Path, variant: Variant, machine: str | None = None,
                 process: str | None = None, filaments: list[str] | None = None,
                 timeout_s: int = 600) -> Metrics:
        h = hashlib.sha256(Path(source).read_bytes())
        h.update(variant.key().encode())
        h.update(repr((machine, process, filaments, self.version())).encode())
        try:  # preset *contents*, not names: user presets get edited
            for kind, name in [("machine", machine), ("process", process)] + [("filament", f) for f in filaments or []]:
                if name:
                    h.update(json.dumps(self._load_preset(kind, name), sort_keys=True).encode())
        except (ValueError, OSError) as e:
            return Metrics(ok=False, error=str(e))
        key = h.hexdigest()[:24]
        run = self.home / "runs" / key
        with self._locks_guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:  # identical variants in one batch must not slice into the same dir at once
            return self._evaluate_locked(source, variant, key, run, machine, process, filaments, timeout_s)

    def _cached(self, run: Path, key: str, plate: int, failures: bool = False, invalidate_corrupt: bool = True) -> Metrics | None:
        """Parsed result.json, or None. Failed slices are only returned with failures=True (never cache-hit)."""
        res = run / "result.json"
        if not res.is_file():
            return None
        try:
            m = parse_result(json.loads(res.read_text(encoding="utf-8")), plate)
        except (OSError, ValueError, AttributeError, TypeError):
            if invalidate_corrupt:
                res.unlink(missing_ok=True)  # corrupt / partial: treat as a miss
            return None
        if m.ok:
            try:
                artifact = read_artifact_result(run / 'out.3mf', run, plate=plate, strict_single_plate=False)
                strict = artifact.metrics
                m._artifact_result = artifact
                m.time_s, m.filament_g = strict['seconds'], strict['total_grams']
                m.role_time_s = strict['feature_seconds'] or {}
                m.toolpath_profiles = strict['toolpath_profiles']
                m.warnings = strict['warnings']
                m.outside = strict['outside']
                if m.outside:
                    m.ok, m.error = False, 'outside_plate'
            except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, ET.ParseError) as exc:
                m.ok, m.error = False, f'invalid sliced artifact: {exc}'
        if not m.ok and not failures:
            return None
        m.run_id, m.out_3mf = key, str(run / "out.3mf")
        return m

    def _evaluate_locked(self, source, variant, key, run, machine, process, filaments, timeout_s) -> Metrics:
        if (hit := self._cached(run, key, variant.plate)) is not None:
            return hit
        res = run / "result.json"
        run.mkdir(parents=True, exist_ok=True)
        try:
            in_file = str(source)
            if variant.object_overrides or variant.object_rotation:
                pkg = Package(source)
                for oid, rot in variant.object_rotation.items():
                    pkg.rotate_object(oid, *rot)
                for oid, vals in variant.object_overrides.items():
                    pkg.set_object_settings(oid, vals)
                in_file = str(run / "in.3mf")
                pkg.save(in_file)
            with tempfile.TemporaryDirectory() as td:
                settings = [self._flatten("machine", machine, Path(td))] if machine else []
                settings += [self._flatten("process", process, Path(td))] if process else []
                fils = [self._flatten("filament", f, Path(td)) for f in (filaments or [])]
                argv = self.argv(variant, in_file, run, settings, fils, timeout_s)
                (run / "cmd.json").write_text(json.dumps(argv))
                code, failure, tail, elapsed = run_bounded(argv, run, timeout_s)
                if failure or code != 0:
                    return Metrics(ok=False, run_id=key, error=f"{failure or 'exit'} (exit {code}): {tail[-2000:]}")
        except (ValueError, KeyError, OSError, zipfile.BadZipFile, ET.ParseError) as e:
            return Metrics(ok=False, run_id=key, error=f"{type(e).__name__}: {e}")
        if not res.is_file():
            return Metrics(ok=False, run_id=key, error=f"no result.json (exit {code}):\n{tail[-2000:]}")
        return self._cached(run, key, variant.plate, failures=True) or Metrics(
            ok=False, run_id=key, error="unreadable result.json")
