"""OSD renderer tests (pure PIL, no window is created)."""
from __future__ import annotations

import unittest

from PIL import Image

from app.osd import CARD_H, CARD_W, SHADOW, OsdModel, premultiply, render_osd


class RenderTests(unittest.TestCase):
    def test_size_and_transparency(self):
        image = render_osd(OsdModel(name="Bus A1", percent=72, db=-6.0), 1.0)
        self.assertEqual(image.mode, "RGBA")
        # 2x supersampling is downsampled back to the design size.
        self.assertAlmostEqual(image.width, CARD_W + SHADOW * 2, delta=2)
        self.assertAlmostEqual(image.height, CARD_H + SHADOW * 2, delta=2)
        alpha = image.getchannel("A").getextrema()
        self.assertEqual(alpha[0], 0)      # transparent corners / shadow edge
        self.assertEqual(alpha[1], 255)    # opaque text/card content

    def test_scale(self):
        small = render_osd(OsdModel(percent=10), 0.6)
        large = render_osd(OsdModel(percent=10), 1.6)
        self.assertGreater(large.width, small.width * 2)

    def test_muted_and_warning_differ_from_normal(self):
        normal = render_osd(OsdModel(name="A", percent=50, db=-24.0), 1.0)
        muted = render_osd(OsdModel(name="A", percent=50, db=-24.0, muted=True), 1.0)
        warning = render_osd(OsdModel(name="Voicemeeter is not running", percent=None,
                                      db=None, warning=True), 1.0)
        self.assertNotEqual(normal.tobytes(), muted.tobytes())
        self.assertNotEqual(normal.tobytes(), warning.tobytes())

    def test_zero_and_full(self):
        zero = render_osd(OsdModel(name="A", percent=0, db=-60.0), 1.0)
        full = render_osd(OsdModel(name="A", percent=100, db=0.0), 1.0)
        self.assertNotEqual(zero.tobytes(), full.tobytes())

    def test_over_100_percent(self):
        hundred = render_osd(OsdModel(name="A", percent=100, db=0.0), 1.0)
        over = render_osd(OsdModel(name="A", percent=110, db=6.0), 1.0)
        self.assertNotEqual(hundred.tobytes(), over.tobytes())
        # The bar stays full but the number keeps growing without breaking layout.
        huge = render_osd(OsdModel(name="A", percent=300, db=12.0), 1.0)
        self.assertEqual(huge.mode, "RGBA")

    def test_long_name_is_truncated_without_crashing(self):
        image = render_osd(OsdModel(name="X" * 300, percent=50, db=0.0), 1.0)
        self.assertEqual(image.mode, "RGBA")

    def test_premultiply(self):
        image = Image.new("RGBA", (2, 2), (255, 0, 0, 128))
        premul = premultiply(image)
        r, g, b, a = premul.getpixel((0, 0))
        self.assertEqual(a, 128)
        self.assertAlmostEqual(r, 128, delta=1)   # 255 * 128 / 255
        self.assertEqual(g, 0)
        self.assertEqual(b, 0)

    def test_premultiplied_bgra_bytes_are_valid(self):
        image = render_osd(OsdModel(name="A", percent=50, db=0.0), 1.0)
        data = premultiply(image).tobytes("raw", "BGRA")
        self.assertEqual(len(data), image.width * image.height * 4)


if __name__ == "__main__":
    unittest.main()
