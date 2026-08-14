"""Pipeline orchestration — runs the stages in order over a matched folder.

Kept separate from `agent.py` so the whole pipeline can be exercised from a
script or a test without constructing an ADK agent or a model session.

A breed folder can hold well over a thousand images while a request asks for a
hundred, so candidates are pulled in batches and the pipeline stops as soon as
it has enough. When rejections leave it short it tops up from the remainder,
which is why deduplication state has to persist across batches.
"""

from __future__ import annotations

import hashlib
import logging
import math

from src import config, drive, inspection, prefilter, processing, storage
from src.dedup import Deduplicator
from src.models import (
    STAGE_PROCESSING,
    STAGE_UPLOAD,
    Candidate,
    Rejection,
    RunReport,
)

logger = logging.getLogger(__name__)


class RequestError(ValueError):
    """The request itself is invalid and no work should start."""


def validate_request(count: int) -> None:
    """Stage 0 — reject an unusable count outright.

    The ceiling is a safety boundary, not a preference: this service may be
    exposed publicly, and an unbounded request would consume Gemini quota and
    Drive storage without limit. An over-large request is refused rather than
    quietly clamped, so the caller always knows what they are getting.
    """
    if count <= 0:
        raise RequestError(f"Requested count must be at least 1, got {count}.")
    if count > config.MAX_IMAGES_PER_REQUEST:
        raise RequestError(
            f"Requested {count} images, which exceeds the maximum of "
            f"{config.MAX_IMAGES_PER_REQUEST} per request. Ask for "
            f"{config.MAX_IMAGES_PER_REQUEST} or fewer."
        )


def sample_order(candidates: list[Candidate]) -> list[Candidate]:
    """Order candidates so that examining the first N samples the whole folder.

    Sorting by a hash of the filename rather than shuffling the list is
    deliberate. A shuffle depends on the list as a whole, so adding images to a
    breed folder later reorders everything and a repeated request would pick a
    largely different set — filling the Drive folder with a second, overlapping
    batch. A per-filename hash gives each image a fixed position of its own, so
    new arrivals slot in without disturbing what was already chosen.

    Deterministic for a given `SAMPLE_SEED`: the same request always yields the
    same images.
    """

    def position(candidate: Candidate) -> str:
        key = f"{config.SAMPLE_SEED}:{candidate.filename}".encode()
        return hashlib.sha256(key).hexdigest()

    return sorted(candidates, key=position)


def _next_batch_size(still_needed: int, survival_rate: float) -> int:
    """How many candidates to pull to stand a good chance of filling the gap."""
    estimate = still_needed / survival_rate if survival_rate > 0 else still_needed
    return max(config.MIN_BATCH_SIZE, math.ceil(estimate * config.CANDIDATE_OVERSHOOT_RATIO))


def run_pipeline(breed_requested: str, requested_count: int, folder: str) -> RunReport:
    """Run stages 2 through 6 over `folder` and return the finished report.

    Assumes the folder was already confirmed by Stage 1. Raises `RequestError`
    for an invalid count and `storage.StorageError` if the corpus cannot be
    listed at all; every other failure is recorded in the report rather than
    raised, so a partial run still reports what it completed.
    """
    validate_request(requested_count)

    all_candidates = sample_order(storage.list_candidates(folder))
    report = RunReport(
        breed_requested=breed_requested,
        folder_matched=folder,
        requested_count=requested_count,
        candidates_examined=0,
        kept=0,
    )

    if not all_candidates:
        report.warnings.append(f"Folder '{folder}' contains no images.")
        return report

    if len(all_candidates) < requested_count:
        report.warnings.append(
            f"Folder '{folder}' holds only {len(all_candidates)} images, "
            f"fewer than the {requested_count} requested."
        )

    # The Drive folder is created up front: if this fails, nothing has been
    # processed yet and the run can stop cheaply.
    try:
        folder_id, folder_name = drive.ensure_breed_folder(breed_requested)
        report.drive_folder_id = folder_id
        report.drive_folder_name = folder_name
    except drive.DriveError as exc:
        report.warnings.append(f"Could not open the Drive destination: {exc}")
        return report

    deduplicator = Deduplicator()
    cursor = 0
    fallback_crops = 0
    survival_rate = 1.0

    # Candidates that cleared pre-filter and dedup but were not needed by the
    # batch that fetched them. Carried across iterations so a top-up round
    # draws on images already in hand instead of downloading replacements for
    # images it just discarded.
    ready: list[Candidate] = []

    while report.kept < requested_count and (ready or cursor < len(all_candidates)):
        still_needed = requested_count - report.kept

        # Only reach for more of the corpus when what is already in hand
        # cannot cover the shortfall.
        if len(ready) < still_needed and cursor < len(all_candidates):
            shortfall = still_needed - len(ready)
            batch_size = _next_batch_size(shortfall, survival_rate)
            batch = all_candidates[cursor : cursor + batch_size]
            cursor += len(batch)
            report.candidates_examined += len(batch)

            survivors, rejections = prefilter.prefilter(batch)
            report.rejections.extend(rejections)
            survivors, rejections = deduplicator.filter(survivors)
            report.rejections.extend(rejections)
            ready.extend(survivors)

        take, ready = ready[:still_needed], ready[still_needed:]
        if not take:
            break  # nothing in hand and the corpus is exhausted

        kept_here, fallbacks_here = _process_and_file(take, folder, report)
        report.kept += kept_here
        fallback_crops += fallbacks_here

        # Steer the next fetch by what actually reached Drive so far.
        survival_rate = (
            report.kept / report.candidates_examined if report.candidates_examined else 1.0
        )
        logger.info(
            "Filed %d/%d (%d candidates examined, %d in hand)",
            report.kept,
            requested_count,
            report.candidates_examined,
            len(ready),
        )

    # Whatever is still in hand was fetched but never needed.
    report.unused_surplus += len(ready)

    if fallback_crops:
        report.warnings.append(
            f"{fallback_crops} images had no usable annotation, so the full "
            "frame was kept instead of a crop around the dog."
        )

    if report.kept < requested_count and cursor >= len(all_candidates):
        report.warnings.append(
            f"The corpus folder was exhausted after {report.candidates_examined} "
            f"candidates; no more images were available to make up the shortfall."
        )

    return report


def _process_and_file(
    survivors: list[Candidate],
    folder: str,
    report: RunReport,
) -> tuple[int, int]:
    """Take deduplicated survivors through stages 5 and 6.

    Returns `(filed, fallback_crops)`, counting only images that actually
    reached Drive.
    """
    # Stage 4 — the judgement a person would otherwise make by hand, several
    # hundred times per breed. Runs on the full image, before cropping: the
    # body box frames a single dog, so cropping first would hide the second and
    # third dog in the frame, which is one of the things this stage looks for.
    survivors, rejections = inspection.inspect(survivors, report.breed_requested)
    report.rejections.extend(rejections)

    processed, processing_failures = processing.process_all(survivors, folder)
    report.rejections.extend(
        Rejection(filename, STAGE_PROCESSING, reason)
        for filename, reason in processing_failures
    )

    uploaded, upload_failures = drive.upload_all(processed, report.drive_folder_id)
    report.rejections.extend(
        Rejection(filename, STAGE_UPLOAD, reason) for filename, reason in upload_failures
    )

    failed_names = {name for name, _ in upload_failures}
    filed = [image for image in processed if image.filename not in failed_names]
    fallback_crops = sum(1 for image in filed if image.used_fallback_crop)

    return uploaded, fallback_crops
