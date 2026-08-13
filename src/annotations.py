"""Parse Tsinghua Dogs annotation XML into a bounding box.

Only `bodybndbox` is used — `headbndbox` is ignored in this block.

Real files in this corpus start with a UTF-8 BOM and carry no XML declaration,
so parsing is done defensively rather than assuming well-formed input.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from src.models import BoundingBox

logger = logging.getLogger(__name__)

_UTF8_BOM = b"\xef\xbb\xbf"


def parse_body_bbox(xml_bytes: bytes) -> BoundingBox | None:
    """The first `bodybndbox` in the document, or None if unusable.

    Returns None rather than raising: a malformed annotation should demote the
    image to the fallback crop path, not abort the run.
    """
    if xml_bytes.startswith(_UTF8_BOM):
        xml_bytes = xml_bytes[len(_UTF8_BOM) :]

    try:
        root = ET.fromstring(xml_bytes.strip())
    except ET.ParseError as exc:
        logger.warning("Malformed annotation XML: %s", exc)
        return None

    box_element = root.find(".//bodybndbox")
    if box_element is None:
        logger.warning("Annotation has no bodybndbox")
        return None

    try:
        # Coordinates are occasionally written as floats; int(float(...)) takes
        # both without a ValueError on "173.0".
        coords = {
            name: int(float(box_element.findtext(name, "").strip()))
            for name in ("xmin", "ymin", "xmax", "ymax")
        }
    except (AttributeError, ValueError) as exc:
        logger.warning("Unreadable bodybndbox coordinates: %s", exc)
        return None

    bbox = BoundingBox(**coords)
    if bbox.width <= 0 or bbox.height <= 0:
        logger.warning("Degenerate bodybndbox %s", coords)
        return None
    return bbox


def parse_declared_size(xml_bytes: bytes) -> tuple[int, int] | None:
    """The `<size>` the annotation claims, as (width, height).

    Not authoritative — the image header wins — but useful for diagnostics.
    """
    if xml_bytes.startswith(_UTF8_BOM):
        xml_bytes = xml_bytes[len(_UTF8_BOM) :]
    try:
        root = ET.fromstring(xml_bytes.strip())
        size = root.find("size")
        if size is None:
            return None
        return (
            int(float(size.findtext("width", "").strip())),
            int(float(size.findtext("height", "").strip())),
        )
    except (ET.ParseError, AttributeError, ValueError):
        return None
