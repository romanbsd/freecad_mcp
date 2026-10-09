#!/usr/bin/env python3
"""Trace dark artwork on a light background into a connected spray stencil."""
import argparse
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np
from shapely.geometry import LineString, Polygon, box
from shapely.ops import nearest_points, unary_union


def trace_image(source, width=70, thickness=2, margin=8, bridge_width=0.8,
                threshold=128, simplify=0.025):
    source = Path(source).expanduser().resolve()
    im = cv2.imread(str(source), cv2.IMREAD_GRAYSCALE)
    if im is None:
        raise ValueError(f'Cannot read image: {source}')
    if not all(math.isfinite(v) and v > 0 for v in (width, thickness, margin, bridge_width)):
        raise ValueError('Dimensions and bridge width must be positive and finite')
    if not 1 <= threshold <= 254 or not math.isfinite(simplify) or simplify < 0:
        raise ValueError('Threshold must be 1..254; simplification must be nonnegative')
    ys, xs = np.where(im < threshold)
    if len(xs) == 0 or xs.max() == xs.min():
        raise ValueError('No usable dark artwork found')
    up = cv2.resize(im, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
    up = cv2.GaussianBlur(up, (5, 5), 0.8)
    mask = (up < threshold).astype(np.uint8) * 255
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    raw = []
    def points(contour):
        return cv2.approxPolyDP(contour, 0.38, True).reshape(-1, 2).tolist()
    for i, contour in enumerate(contours):
        if hierarchy[0][i][3] != -1 or cv2.contourArea(contour) < 20:
            continue
        holes = []
        j = hierarchy[0][i][2]
        while j != -1:
            if cv2.contourArea(contours[j]) > 10:
                holes.append(points(contours[j]))
            j = hierarchy[0][j][0]
        raw.append(Polygon(points(contour), holes).buffer(0))
    if not raw:
        raise ValueError('No contours survived noise filtering')
    xmin, ymin, xmax, ymax = unary_union(raw).bounds
    scale = width / (xmax-xmin)
    def convert(coords):
        return [((x-xmin)*scale+margin, (ymax-y)*scale+margin) for x, y in coords]
    cuts, bridges = [], []
    for geometry in raw:
        for p in polygons(geometry):
            poly = Polygon(convert(p.exterior.coords), [convert(r.coords) for r in p.interiors])
            poly = poly.simplify(simplify, preserve_topology=True)
            for ring in poly.interiors:
                a, b = nearest_points(Polygon(ring), LineString(poly.exterior.coords))
                dx, dy = b.x-a.x, b.y-a.y
                length = math.hypot(dx, dy)
                if length <= 1e-9:
                    raise ValueError('Degenerate island connection')
                overlap = bridge_width
                line = LineString([(a.x-dx/length*overlap, a.y-dy/length*overlap),
                                   (b.x+dx/length*overlap, b.y+dy/length*overlap)])
                bridges.append(line.buffer(bridge_width/2, cap_style=2))
            cuts.append(poly)
    cut = unary_union(cuts).difference(unary_union(bridges)).buffer(0)
    height = (ymax-ymin)*scale
    plate_width, plate_height = width+2*margin, height+2*margin
    material = box(0, 0, plate_width, plate_height).difference(cut)
    if material.geom_type != 'Polygon' or not material.is_valid:
        raise ValueError('Disconnected stencil material; adjust bridge width or artwork')
    if cut.is_empty:
        raise ValueError('Bridges removed all artwork; reduce bridge width')
    return {'schema_version': 1, 'source': str(source),
            'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
            'lettering_width': width, 'height': height, 'margin': margin,
            'thickness': thickness, 'bridge_width': bridge_width, 'threshold': threshold,
            'simplify_mm': simplify, 'bridges': len(bridges),
            'plate_width': plate_width, 'plate_height': plate_height,
            'polygons': [{'outer': list(p.exterior.coords),
                          'holes': [list(r.coords) for r in p.interiors]} for p in polygons(cut)]}


def polygons(geometry):
    if geometry.geom_type == 'Polygon':
        return [geometry]
    if geometry.geom_type == 'MultiPolygon':
        return list(geometry.geoms)
    raise ValueError(f'Unexpected contour geometry: {geometry.geom_type}')


def save_trace(data, output, overwrite=False):
    output = Path(output).expanduser().resolve()
    files = [output / name for name in ('trace.json', 'stencil_preview.png', 'build.FCMacro')]
    if not overwrite and any(p.exists() for p in files):
        raise FileExistsError('Output exists; choose a new directory or pass --overwrite')
    output.mkdir(parents=True, exist_ok=True)
    files[0].write_text(json.dumps(data, indent=2))
    factor = min(12, 2000/max(data['plate_width'], data['plate_height']))
    height = data['plate_height']
    preview = np.full((math.ceil(height*factor)+1, math.ceil(data['plate_width']*factor)+1, 3), 220, np.uint8)
    def pix(coords):
        return np.array([(round(x*factor), round((height-y)*factor)) for x, y in coords], np.int32)
    for p in data['polygons']:
        cv2.fillPoly(preview, [pix(p['outer'])], (40, 40, 40))
        for hole in p['holes']:
            cv2.fillPoly(preview, [pix(hole)], (220, 220, 220))
    if not cv2.imwrite(str(files[1]), preview):
        raise OSError('Could not save preview')
    builder = Path(__file__).resolve().with_name('build_freecad.py')
    files[2].write_text('import runpy\n'
                       f"result = runpy.run_path({str(builder)!r})['build_stencil']({str(files[0])!r}, overwrite={overwrite!r})\n"
                       'print(result)\n')
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    for name, default in [('width', 70), ('thickness', 2), ('margin', 8), ('bridge-width', .8), ('simplify', .025)]:
        parser.add_argument('--'+name, type=float, default=default)
    parser.add_argument('--threshold', type=int, default=128)
    parser.add_argument('--overwrite', action='store_true')
    args = parser.parse_args()
    try:
        data = trace_image(args.image, args.width, args.thickness, args.margin,
                           args.bridge_width, args.threshold, args.simplify)
        files = save_trace(data, args.output, args.overwrite)
    except (ValueError, OSError) as exc:
        parser.exit(1, f'Error: {exc}\n')
    print(json.dumps({'plate_mm': [data['plate_width'], data['plate_height'], data['thickness']],
                      'bridges': data['bridges'], 'files': [str(p) for p in files]}, indent=2))


if __name__ == '__main__':
    main()
