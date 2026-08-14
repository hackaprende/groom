"""Stage 2 — technical pre-filter.

Drops images that are too small or cannot be decoded at all. This runs before
anything expensive by design: every image it rejects is one the pipeline never
hashes, never sends to Gemini, and never crops.

`Image.open` parses the header and stops — it does not decode pixel data — so
dimensions cost little beyond the download itself.
"""

from __future__ import annotations

import io
import logging
from concurrent.futures import ThreadPoolExecutor

from PIL import Image, UnidentifiedImageError

from src import config, storage
from src.models import STAGE_PREFILTER, Candidate, Rejection

logger = logging.getLogger(__name__)


def _load(candidate: Candidate) -> tuple[Candidate, Rejection | None]:
    """Download one candidate and read its dimensions from the header."""
    try:
        candidate.image_bytes = storage.download_bytes(candidate.blob_name)
    except storage.StorageError as exc:
        return candidate, Rejection(candidate.filename, STAGE_PREFILTER, f"download failed: {exc}")

    try:
        with Image.open(io.BytesIO(candidate.image_bytes)) as image:
            candidate.width, candidate.height = image.size
    except (UnidentifiedImageError, OSError, ValueError):
        candidate.image_bytes = None  # release: this one is going no further
        return candidate, Rejection(candidate.filename, STAGE_PREFILTER, "unreadable image")

    if candidate.width < config.MIN_SOURCE_WIDTH or candidate.height < config.MIN_SOURCE_HEIGHT:
        reason = (
            f"below minimum resolution "
            f"({candidate.width}x{candidate.height} < "
            f"{config.MIN_SOURCE_WIDTH}x{config.MIN_SOURCE_HEIGHT})"
        )
        candidate.image_bytes = None
        return candidate, Rejection(candidate.filename, STAGE_PREFILTER, reason)

    return candidate, None


def prefilter(candidates: list[Candidate]) -> tuple[list[Candidate], list[Rejection]]:
    """Download and screen candidates.

    Returns the survivors — each with `image_bytes`, `width` and `height`
    populated — and one `Rejection` per image that did not pass.
    """
    survivors: list[Candidate] = []
    rejections: list[Rejection] = []

    # Build the storage client before any thread exists — see the note in
    # inspection.inspect(): a cached-but-unbuilt client is a race, not a cache.
    storage.warm_client()

    with ThreadPoolExecutor(max_workers=config.DOWNLOAD_CONCURRENCY) as pool:
        for candidate, rejection in pool.map(_load, candidates):
            if rejection is None:
                survivors.append(candidate)
            else:
                rejections.append(rejection)

    logger.info("Pre-filter: %d kept, %d rejected", len(survivors), len(rejections))
    return survivors, rejections
