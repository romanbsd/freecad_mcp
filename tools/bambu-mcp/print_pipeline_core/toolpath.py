"""Bounded Bambu comment diagnostics; no G-code execution or motion inference (MIT)."""
import math
import re


def comment_profile(stream, max_bytes=64 * 1024**2):
    heights, features = {}, {}
    scanned, complete, malformed = 0, True, 0
    while True:
        line = stream.readline(min(65537, max_bytes - scanned + 1))
        if not line:
            break
        scanned += len(line)
        if scanned > max_bytes or len(line) > 65536:
            complete = False
            break
        if not line.startswith(b';'):
            continue
        text = line.decode('utf-8', errors='replace').strip()
        match = re.fullmatch(r';\s*LAYER_HEIGHT:\s*(\S+)\s*', text)
        if match:
            try:
                value = float(match[1])
                if not math.isfinite(value) or value <= 0:
                    raise ValueError('invalid height')
                key = format(value, '.12g')
                heights[key] = heights.get(key, 0) + 1
            except ValueError:
                malformed += 1
        match = re.fullmatch(r';\s*FEATURE:\s*(.{1,120})', text)
        if match:
            key = match[1].strip()
            features[key] = features.get(key, 0) + 1
        if len(heights) > 4096 or len(features) > 256:
            complete = False
            break
    return dict(source='Bambu G-code comments', complete=complete, bytes_scanned=scanned,
                declared_layer_heights_mm=heights, feature_blocks=features,
                min_declared_layer_height_mm=min(map(float, heights)) if heights else None,
                max_declared_layer_height_mm=max(map(float, heights)) if heights else None,
                malformed_height_comments=malformed,
                limitations='Comment declarations and block counts; LAYER_HEIGHT may describe combined infill or support extrusion thickness. Not a nominal layer ladder, extrusion mass, executed motion, or physical quality.')
