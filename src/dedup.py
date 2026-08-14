"""Stage 3 — near-duplicate detection via perceptual hashing.

Byte comparison only catches identical files. Public dog corpora are full of the
same photo at two sizes, or two frames from one burst — visually redundant
images that add nothing to a training set. A perceptual hash catches those:
similar images produce similar hashes, and closeness is measured as Hamming
distance between them.

The pipeline downloads in batches, so `Deduplicator` keeps its hash registry
across calls. Use the module-level `deduplicate` for one-shot testing.
"""

from __future__ import annotations

import io
import logging

import imagehash
from PIL import Image

from . import config
from .models import STAGE_DEDUP, Candidate, Rejection

logger = logging.getLogger(__name__)


class Deduplicator:
    """Rejects candidates that closely resemble one already kept.

    The first occurrence of an image wins; later near-matches are rejected with
    the name of the file they duplicate.
    """

    def __init__(self, threshold: int = config.DEDUP_HASH_THRESHOLD) -> None:
        self._threshold = threshold
        # Parallel lists rather than a dict: hashes are not hashable keys for a
        # *near*-match lookup, so every comparison is linear anyway.
        self._hashes: list[imagehash.ImageHash] = []
        self._filenames: list[str] = []

    def filter(self, candidates: list[Candidate]) -> tuple[list[Candidate], list[Rejection]]:
        """Partition candidates into survivors and duplicate rejections."""
        survivors: list[Candidate] = []
        rejections: list[Rejection] = []

        for candidate in candidates:
            if candidate.image_bytes is None:
                rejections.append(
                    Rejection(candidate.filename, STAGE_DEDUP, "no image data to hash")
                )
                continue

            try:
                with Image.open(io.BytesIO(candidate.image_bytes)) as image:
                    fingerprint = imagehash.dhash(image)
            except (OSError, ValueError) as exc:
                rejections.append(
                    Rejection(candidate.filename, STAGE_DEDUP, f"could not hash image: {exc}")
                )
                continue

            duplicate_of = self._find_match(fingerprint)
            if duplicate_of is not None:
                rejections.append(
                    Rejection(
                        candidate.filename,
                        STAGE_DEDUP,
                        f"near-duplicate of {duplicate_of}",
                    )
                )
                continue

            self._hashes.append(fingerprint)
            self._filenames.append(candidate.filename)
            survivors.append(candidate)

        logger.info("Dedup: %d kept, %d rejected", len(survivors), len(rejections))
        return survivors, rejections

    def _find_match(self, fingerprint: imagehash.ImageHash) -> str | None:
        """The filename of the first image within the distance threshold."""
        for known, filename in zip(self._hashes, self._filenames):
            if (fingerprint - known) <= self._threshold:
                return filename
        return None


def deduplicate(candidates: list[Candidate]) -> tuple[list[Candidate], list[Rejection]]:
    """One-shot deduplication over a single list, for standalone testing."""
    return Deduplicator().filter(candidates)
