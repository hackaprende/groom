"""Shared fixtures.

Every test in this suite runs offline. Nothing here calls Gemini, Cloud Storage
or Drive — the stages that talk to them are exercised through stubs. That is
deliberate: an image-inspection call costs money, so the test suite must never
be the thing that spends it.
"""

from __future__ import annotations

import io
import math
import os

import pytest

# Set before importing anything from src: config reads these lazily, but a test
# machine has no .env and should not need one.
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("GCS_BUCKET_NAME", "test-bucket")
os.environ.setdefault("DRIVE_OUTPUT_FOLDER_ID", "test-folder")

from PIL import Image  # noqa: E402

from src.models import Candidate  # noqa: E402


def encode(image: Image.Image, quality: int = 90) -> bytes:
    """JPEG-encode an image to bytes."""
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def photo(width: int = 500, height: int = 332, seed: int = 0) -> Image.Image:
    """A deterministic image with photograph-like frequency content.

    The variation is smooth and low-frequency on purpose. A flat colour would
    hash identically at any size and make dedup tests meaningless, but the
    opposite extreme is just as wrong: a high-frequency pattern such as
    `(x * 7) % 256` is a sawtooth, and JPEG ringing around those edges scrambles
    the neighbour comparisons a perceptual hash is built on. Such a fixture
    fails dedup tests that real photographs pass, which says nothing about the
    code.

    Measured on this fixture, against a threshold of 5: recompression to
    quality 40 moves the hash by 1, halving the size by 0, a 15% brightness
    lift by 1, and a genuinely different image by 28.
    """
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            a = math.sin((x / width) * 2.2 + seed) * math.cos((y / height) * 1.7 + seed * 0.7)
            b = math.sin((x + y) / (width * 0.8) + seed * 1.3)
            pixels[x, y] = (
                int(128 + 100 * a),
                int(128 + 90 * b),
                int(128 + 80 * a * b),
            )
    return image


@pytest.fixture
def sample_photo() -> Image.Image:
    return photo()


@pytest.fixture
def candidate_factory():
    """Build a Candidate carrying real image bytes."""

    def make(name: str, image: Image.Image | None = None, **kwargs) -> Candidate:
        image = image if image is not None else photo()
        return Candidate(
            blob_name=f"low-resolution/breed/{name}",
            filename=name,
            image_bytes=encode(image),
            **kwargs,
        )

    return make


def annotation_xml(
    xmin: int = 79,
    ymin: int = 39,
    xmax: int = 500,
    ymax: int = 332,
    with_bom: bool = True,
) -> bytes:
    """An annotation shaped like the real corpus files, BOM included."""
    body = f"""
<annotation>
\t<folder>1324-n000004-malamute</folder>
\t<filename>n103339.jpg</filename>
\t<size><width>500</width><height>332</height><depth>3</depth></size>
\t<object>
\t\t<name>malamute</name>
\t\t<headbndbox><xmin>83</xmin><ymin>62</ymin><xmax>285</xmax><ymax>326</ymax></headbndbox>
\t\t<bodybndbox>
\t\t\t<xmin>{xmin}</xmin><ymin>{ymin}</ymin><xmax>{xmax}</xmax><ymax>{ymax}</ymax>
\t\t</bodybndbox>
\t</object>
</annotation>
""".strip()
    raw = body.encode("utf-8")
    return b"\xef\xbb\xbf\r\n" + raw if with_bom else raw
