import io
import json
import unittest
import zipfile
from unittest.mock import Mock, patch

from PIL import Image

from render_errors import RenderError
from visual_diff import MAX_DIFF_PIXELS, _open_image, compare_images, create_diff_bundle


def png(color, size=(4, 3)):
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, "PNG")
    return output.getvalue()


class VisualDiffTests(unittest.TestCase):
    def test_pixel_limit_is_checked_before_decode(self):
        image = Mock(width=MAX_DIFF_PIXELS + 1, height=1)
        with patch(
            "visual_diff.Image.open", return_value=image
        ), self.assertRaises(RenderError) as raised:
            _open_image(b"compressed", "baseline")
        self.assertEqual(raised.exception.code, "diff_pixel_limit_exceeded")
        image.load.assert_not_called()

    def test_identical_images_pass(self):
        body = png("white")
        result = compare_images(body, body)
        self.assertTrue(result.passed)
        self.assertEqual(result.changed_pixels, 0)
        self.assertIsNone(result.bounding_box)

    def test_changed_image_reports_ratio_and_bundle(self):
        baseline = png("white")
        current_image = Image.new("RGB", (4, 3), "white")
        current_image.putpixel((1, 1), (255, 0, 0))
        output = io.BytesIO()
        current_image.save(output, "PNG")
        result = compare_images(baseline, output.getvalue(), max_difference_ratio=0.05)
        self.assertFalse(result.passed)
        self.assertEqual(result.changed_pixels, 1)
        self.assertAlmostEqual(result.ratio, 1 / 12)
        with zipfile.ZipFile(io.BytesIO(create_diff_bundle(result))) as archive:
            self.assertEqual(set(archive.namelist()), {"report.json", "diff.png"})
            self.assertEqual(json.loads(archive.read("report.json"))["changed_pixels"], 1)
            self.assertTrue(
                all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())
            )
        self.assertEqual(create_diff_bundle(result), create_diff_bundle(result))

    def test_threshold_and_dimension_validation(self):
        self.assertTrue(compare_images(png((0, 0, 0)), png((2, 2, 2)), pixel_threshold=2).passed)
        with self.assertRaises(RenderError) as raised:
            compare_images(png("white"), png("white", (2, 2)))
        self.assertEqual(raised.exception.code, "diff_dimensions_mismatch")

    def test_alpha_changes_are_visible(self):
        opaque = io.BytesIO()
        transparent = io.BytesIO()
        Image.new("RGBA", (1, 1), (255, 0, 0, 255)).save(opaque, "PNG")
        Image.new("RGBA", (1, 1), (255, 0, 0, 0)).save(transparent, "PNG")
        self.assertEqual(
            compare_images(opaque.getvalue(), transparent.getvalue()).changed_pixels,
            1,
        )

    def test_hidden_rgb_in_transparent_pixels_is_ignored(self):
        first = io.BytesIO()
        second = io.BytesIO()
        Image.new("RGBA", (1, 1), (255, 0, 0, 0)).save(first, "PNG")
        Image.new("RGBA", (1, 1), (0, 0, 255, 0)).save(second, "PNG")
        self.assertEqual(
            compare_images(first.getvalue(), second.getvalue()).changed_pixels,
            0,
        )

    def test_exif_orientation_is_applied_before_comparison(self):
        oriented = io.BytesIO()
        normalized = io.BytesIO()
        source = Image.new("RGB", (2, 1), "red")
        exif = source.getexif()
        exif[274] = 6
        source.save(oriented, "JPEG", exif=exif, quality=100, subsampling=0)
        Image.new("RGB", (1, 2), "red").save(
            normalized,
            "JPEG",
            quality=100,
            subsampling=0,
        )
        self.assertEqual(
            compare_images(
                oriented.getvalue(),
                normalized.getvalue(),
            ).changed_pixels,
            0,
        )

    def test_expanded_pixel_working_set_is_bounded(self):
        side = int(MAX_DIFF_PIXELS**0.5) + 1
        with self.assertRaises(RenderError) as raised:
            compare_images(png("white", (side, side)), png("white", (side, side)))
        self.assertEqual(raised.exception.code, "diff_pixel_limit_exceeded")


if __name__ == "__main__":
    unittest.main()
