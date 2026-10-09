"""Run with the tracing environment: python -m unittest discover -s .agents/skills/stencil/scripts."""
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from trace_image import save_trace, trace_image


class TraceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'ring.png'
        im = np.full((120, 160), 255, np.uint8)
        cv2.circle(im, (80, 60), 40, 0, -1)
        cv2.circle(im, (80, 60), 25, 255, -1)
        cv2.imwrite(str(self.source), im)

    def test_ring_center_attached_and_width_scaled(self):
        d = trace_image(self.source, width=70)
        cut = unary_union([Polygon(p['outer'], p['holes']) for p in d['polygons']])
        material = box(0, 0, d['plate_width'], d['plate_height']).difference(cut)
        self.assertEqual(d['bridges'], 1)
        self.assertEqual(material.geom_type, 'Polygon')
        self.assertTrue(material.is_valid)
        self.assertAlmostEqual(cut.bounds[2]-cut.bounds[0], 70, delta=.06)
        self.assertAlmostEqual(d['plate_width'], 86)

    def test_blank_image_rejected(self):
        cv2.imwrite(str(self.source), np.full((120, 160), 255, np.uint8))
        with self.assertRaisesRegex(ValueError, 'No usable dark artwork'):
            trace_image(self.source)

    def test_output_not_silently_overwritten(self):
        d = trace_image(self.source)
        target = self.root / 'output'
        save_trace(d, target)
        with self.assertRaises(FileExistsError):
            save_trace(d, target)
        self.assertTrue((target / 'build.FCMacro').read_text().startswith('import runpy'))

    def test_invalid_dimensions_rejected(self):
        for value in (0, -1, float('nan'), float('inf')):
            with self.subTest(width=value), self.assertRaises(ValueError):
                trace_image(self.source, width=value)


if __name__ == '__main__':
    unittest.main()
