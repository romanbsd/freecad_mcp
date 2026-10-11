"""Optional, read-only lib3mf mesh inspection in a bounded subprocess (MIT)."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import zipfile


def _inspect(path):
    try:
        import lib3mf
    except ImportError:
        return dict(status='unavailable', error='Install bambu-mcp[validation] to enable lib3mf inspection')
    wrapper = lib3mf.get_wrapper()
    result = dict(status='failed', library_version='.'.join(map(str, wrapper.GetLibraryVersion())),
                  strict_compliant=False, geometry_valid=False, warnings=[], meshes=[],
                  limitations=['Mesh validity and manifold checks do not detect every self-intersection or certify printability.',
                               'Bambu vendor metadata may fail strict 3MF compliance. No file is rewritten.'])
    model = wrapper.CreateModel()
    reader = model.QueryReader('3mf')
    reader.SetStrictModeActive(False)
    try:
        reader.ReadFromFile(str(path))
        result['warning_count'] = reader.GetWarningCount()
        result['warnings'] = [dict(code=code, message=message) for code, message in
                              (reader.GetWarning(i) for i in range(min(100, reader.GetWarningCount()))) ]
        meshes = model.GetMeshObjects()
        total = 0
        while meshes.MoveNext():
            mesh = meshes.GetCurrentMeshObject()
            total += mesh.GetTriangleCount()
            if len(result['meshes']) >= 1024 or total > 6000000:
                raise ValueError('Mesh inspection exceeds object/triangle budget')
            result['meshes'].append(dict(id=mesh.GetResourceID(), vertices=mesh.GetVertexCount(),
                                         triangles=mesh.GetTriangleCount(), valid=mesh.IsValid(),
                                         manifold_and_oriented=mesh.IsManifoldAndOriented()))
        result['geometry_valid'] = bool(result['meshes']) and all(
            m['valid'] and m['manifold_and_oriented'] for m in result['meshes'])
        strict_model = wrapper.CreateModel()
        strict = strict_model.QueryReader('3mf')
        strict.SetStrictModeActive(True)
        try:
            strict.ReadFromFile(str(path))
            result['strict_compliant'] = True
        except lib3mf.ELib3MFException as exc:
            result['strict_error'] = str(exc)
        result['status'] = ('needs_attention' if result['warning_count'] or not result['strict_compliant'] else 'valid') if result['geometry_valid'] else 'failed'
    except (lib3mf.ELib3MFException, ValueError) as exc:
        result['error'] = str(exc)
    return result


def validate_file(path, python_executable=None, timeout=30):
    """Inspect a snapshot; statuses are advisory and never change optimizer ranking."""
    from .process import run_bounded
    source = Path(path).expanduser().resolve(strict=True)
    with source.open('rb') as handle:
        data = handle.read(128 * 1024**2 + 1)
    if len(data) > 128 * 1024**2:
        raise ValueError('Validation input exceeds 128 MiB')
    # Bound decompression before handing the archive to a native library.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > 10000 or len({i.filename for i in infos}) != len(infos) or sum(i.file_size for i in infos) > 1024**3:
            raise ValueError('Duplicate or oversized validation archive')
    with tempfile.TemporaryDirectory(prefix='print-validation-') as directory:
        work = Path(directory)
        snapshot = work / 'input.3mf'
        snapshot.write_bytes(data)
        code, error, log, elapsed = run_bounded(
            [str(python_executable or sys.executable), str(Path(__file__).resolve()), '--worker', str(snapshot)],
            work, timeout)
        try:
            result = json.loads((work / 'slicer.log').read_text()) if code == 0 and not error else dict(status='error', error=error or f'worker exit {code}')
        except (ValueError, OSError):
            result = dict(status='error', error='Invalid validation worker response')
    result.update(source_sha256=hashlib.sha256(data).hexdigest(), elapsed_seconds=elapsed, read_only=True)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', required=True)
    args = parser.parse_args()
    print(json.dumps(_inspect(Path(args.worker)), allow_nan=False))
