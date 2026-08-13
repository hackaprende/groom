"""Stage 5 — crop to the subject, at native resolution.

The annotation gives a body box; this stage pads it, clamps it to the image and
saves that region as-is. Deliberately absent: any resize, and any attempt to
force the crop square.

Both were tried and removed. The training code downstream does its own resize,
so resizing here would resample twice and lose detail irreversibly, and it
would bake one target resolution into every file. Forcing the crop square was
worse: on this corpus the body box is usually wider than the photo is tall, so
a square crop could only be had by padding with black bars — which happened on
roughly half of all images. Dropping the square requirement removed that
problem outright rather than trading it for a different one.

Order of operations for one image:

1. pad the body box by `CROP_PADDING_RATIO` on all sides
2. clamp to the image bounds — never crop outside the image
3. reject the crop if it is smaller than the training input size
4. save the region at its native size and aspect ratio
"""

from __future__ import annotations

import io
import logging

from PIL import Image

from src import annotations, config, storage
from src.models import BoundingBox, Candidate, ProcessedImage

logger = logging.getLogger(__name__)


class CropTooSmall(ValueError):
    """The crop is smaller than the training input size.

    Its own type so the pipeline can record it as a resolution rejection with a
    clean reason, rather than lumping it in with genuine processing errors.
    """


def _padded_crop_box(
    bbox: BoundingBox, image_width: int, image_height: int
) -> BoundingBox:
    """The body box grown by the padding ratio and clamped to the image.

    Padding is proportional to the box, so a small dog in a large frame gets a
    proportionally small margin rather than a fixed one.
    """
    pad_x = round(bbox.width * config.CROP_PADDING_RATIO)
    pad_y = round(bbox.height * config.CROP_PADDING_RATIO)

    return BoundingBox(
        xmin=max(0, bbox.xmin - pad_x),
        ymin=max(0, bbox.ymin - pad_y),
        xmax=min(image_width, bbox.xmax + pad_x),
        ymax=min(image_height, bbox.ymax + pad_y),
    )


def process_candidate(candidate: Candidate, folder: str) -> ProcessedImage:
    """Crop one survivor to its dog.

    Fetches the candidate's annotation; if there is none, or it is unusable,
    keeps the full image and flags that on the result so the report can warn.
    An image is never rejected for a missing annotation — an uncropped dog is
    still a usable training sample.

    Raises `ValueError` if the candidate carries no image data, and propagates
    Pillow errors for genuinely corrupt images.
    """
    if candidate.image_bytes is None:
        raise ValueError(f"{candidate.filename} has no image data to process")

    with Image.open(io.BytesIO(candidate.image_bytes)) as opened:
        # Deliberately no EXIF transpose: the annotation coordinates describe
        # the stored pixel grid, so rotating first would misplace the box.
        image = opened.convert("RGB")

    bbox: BoundingBox | None = None
    xml_bytes = storage.download_annotation(folder, candidate.filename)
    if xml_bytes is not None:
        bbox = annotations.parse_body_bbox(xml_bytes)

    used_fallback = bbox is None
    if bbox is None:
        # No box means no basis for choosing a crop. Keeping the whole frame is
        # the honest fallback; cropping blind would risk cutting the dog out.
        cropped = image
    else:
        candidate.bbox = bbox
        crop_box = _padded_crop_box(bbox, image.width, image.height)
        cropped = image.crop(
            (crop_box.xmin, crop_box.ymin, crop_box.xmax, crop_box.ymax)
        )

    # Checked before any resize, because a resize would hide the shortfall by
    # upscaling it away.
    if min(cropped.width, cropped.height) < config.MIN_CROP_SIZE:
        raise CropTooSmall(
            f"crop too small ({cropped.width}x{cropped.height}, short side "
            f"below {config.MIN_CROP_SIZE}px)"
        )

    # Opt-in only. Left as None so the training pipeline owns the resize.
    if config.OUTPUT_SIZE is not None:
        cropped = cropped.resize(
            (config.OUTPUT_SIZE, config.OUTPUT_SIZE), Image.Resampling.LANCZOS
        )

    buffer = io.BytesIO()
    cropped.save(buffer, format="JPEG", quality=config.OUTPUT_JPEG_QUALITY)

    return ProcessedImage(
        filename=_output_filename(candidate.filename),
        image_bytes=buffer.getvalue(),
        width=cropped.width,
        height=cropped.height,
        used_fallback_crop=used_fallback,
    )


def _output_filename(source_filename: str) -> str:
    """Normalise the output name to `.jpg` regardless of the source extension."""
    stem = source_filename.rsplit(".", 1)[0]
    return f"{stem}.jpg"


def process_all(
    candidates: list[Candidate], folder: str
) -> tuple[list[ProcessedImage], list[tuple[str, str]]]:
    """Process a batch. Returns the outputs and `(filename, reason)` failures."""
    processed: list[ProcessedImage] = []
    failures: list[tuple[str, str]] = []

    for candidate in candidates:
        try:
            processed.append(process_candidate(candidate, folder))
        except CropTooSmall as exc:
            # Expected and common, not an error — reported on its own terms.
            failures.append((candidate.filename, str(exc)))
        except Exception as exc:  # noqa: BLE001 — one bad image must not end the run
            logger.warning("Could not process %s: %s", candidate.filename, exc)
            failures.append((candidate.filename, f"processing failed: {exc}"))

    return processed, failures
