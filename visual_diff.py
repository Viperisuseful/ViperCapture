"""Deterministic, dependency-light visual regression comparisons."""

from __future__ import annotations

from dataclasses import dataclass
import io
import json
import zipfile

from PIL import Image, ImageChops, ImageDraw, UnidentifiedImageError

from render_errors import RenderError


MAX_DIFF_INPUT_BYTES = 20 * 1024 * 1024
# Pillow expands compressed inputs into several simultaneous RGBA/mask
# buffers. Eight million pixels keeps the bounded single-worker peak well
# below the approximately 1 GiB worst case of the former 50 MP allowance.
MAX_DIFF_PIXELS = 8_000_000


@dataclass(frozen=True)
class VisualDiff:
    changed_pixels: int
    total_pixels: int
    ratio: float
    passed: bool
    bounding_box: tuple[int, int, int, int] | None
    diff_png: bytes

    def report(self) -> dict[str, object]:
        return {
            "changed_pixels": self.changed_pixels,
            "total_pixels": self.total_pixels,
            "difference_ratio": self.ratio,
            "passed": self.passed,
            "bounding_box": list(self.bounding_box) if self.bounding_box else None,
        }


def _open_image(body: bytes, label: str) -> Image.Image:
    if not body or len(body) > MAX_DIFF_INPUT_BYTES:
        raise RenderError(
            "diff_input_invalid",
            f"{label} must be a non-empty image no larger than 50 MiB.",
            413,
            False,
        )
    try:
        image = Image.open(io.BytesIO(body))
        if image.width * image.height > MAX_DIFF_PIXELS:
            raise RenderError("diff_pixel_limit_exceeded", "Visual diff input exceeds the pixel limit.", 413, False)
        image.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise RenderError("diff_input_invalid", f"{label} is not a safe supported image.", 422, False) from exc
    return image.convert("RGBA")


def compare_images(
    baseline_body: bytes,
    current_body: bytes,
    *,
    pixel_threshold: int = 0,
    max_difference_ratio: float = 0,
) -> VisualDiff:
    if not 0 <= pixel_threshold <= 255:
        raise ValueError("pixel_threshold must be between 0 and 255")
    if not 0 <= max_difference_ratio <= 1:
        raise ValueError("max_difference_ratio must be between 0 and 1")
    baseline = _open_image(baseline_body, "baseline")
    current = _open_image(current_body, "current")
    if baseline.size != current.size:
        raise RenderError(
            "diff_dimensions_mismatch",
            "Baseline and current images must have identical dimensions.",
            422,
            False,
            {"baseline": list(baseline.size), "current": list(current.size)},
        )

    def visible_rgba(image: Image.Image) -> Image.Image:
        red, green, blue, alpha = image.split()
        visible = alpha.point(lambda value: 255 if value else 0)
        return Image.merge(
            "RGBA",
            (
                ImageChops.multiply(red, visible),
                ImageChops.multiply(green, visible),
                ImageChops.multiply(blue, visible),
                alpha,
            ),
        )

    difference = ImageChops.difference(
        visible_rgba(baseline),
        visible_rgba(current),
    )
    # A difference in any color or alpha channel changes the rendered pixel.
    channel_masks = [
        channel.point(lambda value: 255 if value > pixel_threshold else 0)
        for channel in difference.split()
    ]
    mask = channel_masks[0]
    for channel_mask in channel_masks[1:]:
        mask = ImageChops.lighter(mask, channel_mask)
    histogram = mask.histogram()
    changed = histogram[255]
    total = baseline.width * baseline.height
    ratio = changed / total if total else 0
    bounding_box = mask.getbbox()

    visualization = current.convert("RGBA")
    overlay = Image.new("RGBA", current.size, (255, 0, 140, 0))
    overlay.putalpha(mask.point(lambda value: 180 if value else 0))
    visualization = Image.alpha_composite(visualization, overlay)
    if bounding_box:
        ImageDraw.Draw(visualization).rectangle(bounding_box, outline=(255, 255, 0, 255), width=2)
    output = io.BytesIO()
    visualization.save(output, format="PNG", optimize=False)
    return VisualDiff(
        changed_pixels=changed,
        total_pixels=total,
        ratio=ratio,
        passed=ratio <= max_difference_ratio,
        bounding_box=bounding_box,
        diff_png=output.getvalue(),
    )


def create_diff_bundle(result: VisualDiff) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("report.json", json.dumps(result.report(), sort_keys=True, indent=2) + "\n")
        archive.writestr("diff.png", result.diff_png)
    return output.getvalue()
