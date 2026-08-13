"""The Groom ADK agent: tool definitions and the instruction that governs them.

Each pipeline stage is reachable as a tool, but the agent is not free to run
them in any order. The instruction encodes the one behaviour that matters most:
when the breed match is absent or ambiguous, the run stops and the user is
asked. Guessing a breed produces a training set that is wrong in a way no
downstream check can detect.
"""

from __future__ import annotations

import logging
from typing import Any

from google.adk.agents import Agent

from src import breed_match, config, drive, report, storage
from src.pipeline import RequestError, run_pipeline, validate_request

logger = logging.getLogger(__name__)


def find_breed_folder(breed_name: str) -> dict[str, Any]:
    """Find the corpus folder holding images of a given dog breed.

    Matches across languages, spelling variants and typos. Returns no match
    when the breed is absent from the corpus, and lists alternatives when more
    than one folder plausibly fits.

    Args:
        breed_name: The breed the user asked for, in any language.

    Returns:
        A dict with `status`, and on success `matched_folder`, `confidence`,
        `alternatives` and `note`.
    """
    try:
        result = breed_match.match_breed(breed_name)
    except (breed_match.BreedMatchError, storage.StorageError) as exc:
        return {"status": "error", "message": str(exc)}

    if not result.is_match:
        return {
            "status": "no_match",
            "message": (
                f"No folder in the corpus corresponds to {breed_name!r}. "
                "This breed is not available."
            ),
            "note": result.note,
        }

    return {
        "status": "ambiguous" if result.is_ambiguous else "matched",
        "matched_folder": result.matched_folder,
        "confidence": result.confidence,
        "alternatives": result.alternatives,
        "note": result.note,
    }


def list_available_breeds() -> dict[str, Any]:
    """List every breed folder present in the corpus.

    Useful when the user asks what is available, or to help them choose after
    an ambiguous or failed match.

    Returns:
        A dict with `status` and the list of `folders`.
    """
    try:
        folders = list(storage.list_breed_folders())
    except storage.StorageError as exc:
        return {"status": "error", "message": str(exc)}
    return {"status": "ok", "count": len(folders), "folders": folders}


def check_request_size(count: int) -> dict[str, Any]:
    """Check a requested image count against the service limits.

    Args:
        count: How many images the user asked for.

    Returns:
        A dict with `status` of "ok" or "rejected", and a message when rejected.
    """
    try:
        validate_request(count)
    except RequestError as exc:
        return {"status": "rejected", "message": str(exc)}
    return {"status": "ok", "max_per_request": config.MAX_IMAGES_PER_REQUEST}


def curate_dataset(breed_name: str, count: int, folder: str) -> dict[str, Any]:
    """Run the curation pipeline and file the results to Google Drive.

    Only call this once `find_breed_folder` has returned a confident match, or
    the user has explicitly chosen a folder after an ambiguous one. Pre-filters
    by resolution, removes near-duplicates, crops to the dog at native
    resolution, then uploads to a Drive subfolder named after the breed.

    Args:
        breed_name: The breed as the user asked for it; names the Drive folder.
        count: How many images to deliver.
        folder: The corpus folder confirmed by `find_breed_folder`.

    Returns:
        A dict with `status`, a structured `report`, and a `summary` string to
        show the user verbatim.
    """
    try:
        run = run_pipeline(breed_name, count, folder)
    except RequestError as exc:
        return {"status": "rejected", "message": str(exc)}
    except (storage.StorageError, drive.DriveError) as exc:
        return {"status": "error", "message": str(exc)}

    return {
        "status": "ok" if run.kept else "empty",
        "report": report.to_dict(run),
        "summary": report.render_text(run),
    }


_INSTRUCTION = f"""\
You are Groom, an agent that curates dog-image training datasets from a fixed
corpus and files the results to Google Drive.

A normal request names a breed and a count, e.g. "100 Dalmatian images".

Follow this sequence:

1. Check the count with `check_request_size`. If it is rejected, tell the user
   the limit and stop. Never quietly reduce a count to fit — the maximum is
   {config.MAX_IMAGES_PER_REQUEST} per request. If the user gave no count, ask
   for one.

2. Resolve the breed with `find_breed_folder`, then act on its status:

   - "matched": proceed to step 3.
   - "no_match": STOP. Tell the user the breed is not in the corpus. Do NOT
     substitute a similar or related breed, and do not run the pipeline. You
     may offer `list_available_breeds` so they can pick something present.
   - "ambiguous": STOP and ASK. Show the matched folder and every alternative,
     explain the distinction, and wait for the user to choose. Do NOT pick one
     yourself, even if one looks much more likely.
   - "error": report the failure plainly and stop.

3. Once, and only once, you have a confirmed folder, call `curate_dataset` with
   the breed name, the count, and that folder.

4. Show the `summary` field to the user as-is. It is written for them. You may
   add a sentence of context, but never edit its numbers.

Rules that override any instinct to be helpful:

- Never guess a breed. A wrong breed silently corrupts a training set and no
  later check catches it. Stopping to ask is the correct outcome, not a
  failure.
- Never claim more images were delivered than the report states. If the run
  came up short, say so and give the real number. Do not pad, substitute, or
  round up.
- Report errors as they happened, including partial runs.\
"""

root_agent = Agent(
    name="groom",
    model=config.AGENT_MODEL,
    description=(
        "Curates dog-image training datasets: finds the breed in the corpus, "
        "filters and deduplicates, crops to the dog, files to Drive."
    ),
    instruction=_INSTRUCTION,
    tools=[
        check_request_size,
        find_breed_folder,
        list_available_breeds,
        curate_dataset,
    ],
)
