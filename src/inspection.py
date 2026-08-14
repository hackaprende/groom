"""Stage 4 — Gemini looks at each image and decides whether it is worth training on.

Every other filter in this pipeline measures something: pixel dimensions, a
hash distance, a crop size. This one makes the judgement a person would
otherwise make by hand, several hundred times per breed — is there a dog, is it
the right one, is there only one, and does the photo resemble what the app will
actually be handed at inference time.

That last criterion is the one that surprises people. A studio portrait on a
clean backdrop is a technically excellent photograph and poor training data for
an app whose users photograph dogs outdoors, in bad light, with a phone.
Rejecting those is deliberate.

Inspection runs on the *full* image, before cropping, on purpose. The body box
frames a single dog, so a crop taken first would hide the second and third dog
in the frame — exactly what this stage exists to catch.
"""

from __future__ import annotations

import io
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache

from google.genai import Client, types
from PIL import Image
from pydantic import BaseModel, Field

from . import config
from .models import STAGE_INSPECTION, Candidate, Rejection

logger = logging.getLogger(__name__)

_SYSTEM_INSTRUCTION = """\
You screen photographs for a dog breed recognition training set. The model
being trained runs on a phone: a user points the camera at a dog and the app
names the breed.

Judge the image against these criteria and return a verdict.

REJECT the image if any of the following is true:

1. No dog is present, or the dog is so small, obscured or cut off that the
   breed could not be told from it.
2. More than one dog is prominently in frame. A single clear subject is
   required — several dogs make the label ambiguous. Ignore a dog that is
   plainly incidental background, far away and out of focus.
3. It does not appear to be the requested breed. Public corpora contain
   mislabelled images. Only reject on this when you are confident; close or
   commonly confused breeds should pass rather than be discarded on a guess.
4. It is too blurred, too dark, too washed out, or too heavily filtered for the
   dog's features to read clearly.
5. It is not a photograph of a real dog: an illustration, a cartoon, a painting,
   a screenshot, a collage, or an image with substantial overlaid text or
   watermarks.
6. It is a commercial studio or catalogue shot. This means a professional
   photography setup, and you should require clear evidence of one: a seamless
   backdrop with no visible floor line, corner or skirting board; studio
   lighting with soft even shadows or visible highlights from softboxes; a
   cut-out or pure white or pure black background; the look of stock or
   advertising photography.

   A plain wall is NOT a studio backdrop. An ordinary room with a painted wall
   behind the dog, a domestic floor, daylight from a window, or a tidy home
   interior is exactly the kind of photo this training set wants. Household
   snapshots frequently have uncluttered backgrounds and they must be kept.
   If you are looking at a home or any real location, keep it.

KEEP everything else. Ordinary, imperfect, real-world snapshots are exactly
what this training set needs. Do not reject an image for being unflattering,
badly composed, cluttered, oddly cropped, or unusual in pose. Casual quality is
the target, not a defect.

When in doubt, keep. A wrongly discarded image costs one sample; an overly
aggressive filter costs the diversity that makes the model work in the field.

In `reason`, state the single most important ground for the decision in one
short phrase, in English, understandable to a person reviewing the run. For a
keep, describe the image briefly instead.\
"""


# Fixed categories, so the report can count rejections by kind. Free-text
# reasons alone would give every rejection a group of one and turn the summary
# into a wall of singletons.
REJECTION_CATEGORIES = (
    "no dog",
    "multiple dogs",
    "wrong breed",
    "unusable quality",
    "not a photograph",
    "studio shot",
)


class ImageVerdict(BaseModel):
    """Structured output contract for one inspected image."""

    keep: bool = Field(description="True if the image belongs in the training set.")
    category: str = Field(
        default="",
        description=(
            "When rejecting, exactly one of: "
            + ", ".join(f'"{c}"' for c in REJECTION_CATEGORIES)
            + '. When keeping, "".'
        ),
    )
    reason: str = Field(description="One short phrase giving the deciding factor.")
    dogs_visible: int = Field(
        default=1, description="How many dogs are prominently in frame."
    )
    confidence: str = Field(default="high", description='"high", "medium" or "low".')


class InspectionError(RuntimeError):
    """Inspection could not be completed for an image."""


@lru_cache(maxsize=1)
def _client() -> Client:
    """Vertex-backed Gemini client, built once per process."""
    return Client(
        enterprise=True,
        project=config.google_cloud_project(),
        location=config.google_cloud_location(),
    )


def _downscale(image_bytes: bytes) -> bytes:
    """Shrink an image before sending it, leaving it untouched if already small.

    Only the bytes on the wire change. The crop written to Drive is always made
    from the original, never from this copy.
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as opened:
            image = opened.convert("RGB")
            if max(image.size) <= config.INSPECTION_MAX_EDGE:
                return image_bytes
            image.thumbnail(
                (config.INSPECTION_MAX_EDGE, config.INSPECTION_MAX_EDGE),
                Image.Resampling.LANCZOS,
            )
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=85)
            return buffer.getvalue()
    except (OSError, ValueError):
        # Unreadable here means unreadable later too; let the caller's error
        # handling deal with it rather than guessing.
        return image_bytes


def inspect_image(image_bytes: bytes, breed: str) -> ImageVerdict:
    """Ask Gemini whether one image belongs in the training set.

    Raises `InspectionError` if the model cannot be reached or its answer
    cannot be read after retries.
    """
    part = types.Part.from_bytes(
        data=_downscale(image_bytes),
        mime_type="image/jpeg",
        media_resolution=config.INSPECTION_MEDIA_RESOLUTION,
    )
    prompt = f"Requested breed: {breed}. Should this image go into the training set?"

    last_error: Exception | None = None
    for attempt in range(config.INSPECTION_MAX_RETRIES + 1):
        try:
            response = _client().models.generate_content(
                model=config.INSPECTION_MODEL,
                contents=[part, prompt],
                config=types.GenerateContentConfig(
                    system_instruction=_SYSTEM_INSTRUCTION,
                    temperature=0.0,
                    response_mime_type="application/json",
                    response_schema=ImageVerdict,
                ),
            )
            parsed = response.parsed
            if isinstance(parsed, ImageVerdict):
                return parsed
            last_error = ValueError(f"unreadable verdict: {response.text!r}")
        except Exception as exc:  # noqa: BLE001 — quota, network and parse failures alike
            last_error = exc
            logger.debug("Inspection attempt %d failed: %s", attempt + 1, exc)

    raise InspectionError(f"Gemini could not inspect the image: {last_error}")


def _inspect_one(
    candidate: Candidate, breed: str
) -> tuple[Candidate, Rejection | None]:
    """Inspect a single candidate, converting a verdict into a rejection or None."""
    if candidate.image_bytes is None:
        return candidate, Rejection(
            candidate.filename, STAGE_INSPECTION, "no image data to inspect"
        )

    try:
        verdict = inspect_image(candidate.image_bytes, breed)
    except InspectionError as exc:
        logger.warning("Could not inspect %s: %s", candidate.filename, exc)
        if config.INSPECTION_REJECT_ON_ERROR:
            return candidate, Rejection(
                candidate.filename, STAGE_INSPECTION, f"inspection failed: {exc}"
            )
        return candidate, None

    if verdict.keep:
        return candidate, None

    # "category: detail" — the report groups on the part before the colon, so
    # counts come out by kind while the individual rejection keeps the model's
    # own wording.
    category = verdict.category.strip().lower()
    if category not in REJECTION_CATEGORIES:
        category = "other"
    return candidate, Rejection(
        candidate.filename, STAGE_INSPECTION, f"{category}: {verdict.reason}"
    )


def inspect(
    candidates: list[Candidate], breed: str
) -> tuple[list[Candidate], list[Rejection]]:
    """Screen a batch of candidates.

    Returns the images worth training on and one `Rejection` per image turned
    down, carrying the model's own wording for why.
    """
    if not candidates:
        return [], []

    survivors: list[Candidate] = []
    rejections: list[Rejection] = []

    # Build the client before any thread exists. `lru_cache` guards its cache
    # but not the call that fills it, so concurrent first-callers each
    # construct a client, all but one is discarded, and whichever thread holds
    # a discarded one fails with "client has been closed".
    _client()

    with ThreadPoolExecutor(max_workers=config.INSPECTION_CONCURRENCY) as pool:
        results = pool.map(lambda c: _inspect_one(c, breed), candidates)
        for candidate, rejection in results:
            if rejection is None:
                survivors.append(candidate)
            else:
                rejections.append(rejection)

    logger.info("Inspection: %d kept, %d rejected", len(survivors), len(rejections))
    return survivors, rejections
