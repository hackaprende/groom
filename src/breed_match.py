"""Stage 1 — match a requested breed name to a corpus folder, using Gemini.

Folder names look like ``1324-n000004-malamute``: a count, an id, and a breed
with underscores for spaces. Users type "Dálmata", "cocker spaniel", or
"Coquer Spaniel", and all three should land on the right folder.

The hard requirement here is the *refusal*. A language model asked to pick the
closest item from a list will always pick something, and a confidently wrong
breed match silently poisons a training set — nothing downstream can detect it.
So the model is instructed to return no match when nothing corresponds, and to
surface alternatives when several plausibly do. The agent stops in both cases.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from google.genai import Client, types
from pydantic import BaseModel, Field

from src import config, storage
from src.models import BreedMatch

logger = logging.getLogger(__name__)

_SYSTEM_INSTRUCTION = """\
You match a user's requested dog breed to a folder in an image corpus.

Folder names have the form {count}-{id}-{breed_name}, where breed_name uses
underscores for spaces, e.g. "1160-n000003-Siberian_husky".

Match across languages (the user may write Spanish, English, or another
language while folders are in English), across underscores and spacing,
across capitalisation, and through typos and partial names.

Rules you must follow:

1. Only return a folder that is present verbatim in the supplied list. Never
   invent, reformat, or correct a folder name.
2. If no folder clearly corresponds to the request, return matched_folder as
   null with confidence "none". Do NOT return the nearest available breed. A
   breed that is absent from the corpus must be reported as absent.
3. If several folders plausibly match, put your best in matched_folder, list
   every other plausible folder in alternatives, and lower the confidence.
   Ambiguity means the request genuinely does not pick one folder out — for
   instance when one breed name is contained within another, so the shorter
   request could refer to either. Judge this from the folder list in front of
   you, not from a fixed list of confusable breeds.
4. Use confidence "high" only when exactly one folder is the unambiguous
   answer and alternatives is empty.
5. Related but different breeds are NOT matches. A request for a breed the
   corpus lacks is a "none", even if a cousin breed is present.

Put anything the user should know in `note`, briefly.\
"""


class _BreedMatchResponse(BaseModel):
    """Structured output contract for the matcher."""

    matched_folder: str | None = Field(
        default=None, description="Exact folder name from the list, or null if none matches."
    )
    confidence: str = Field(description='One of "high", "medium", "low", "none".')
    alternatives: list[str] = Field(
        default_factory=list, description="Other plausible folder names from the list."
    )
    note: str = Field(default="", description="Short explanation for the user.")


@lru_cache(maxsize=1)
def _client() -> Client:
    """Vertex-backed Gemini client, built once per process.

    `location` must be `global` — Gemini 3.5 models are not served from
    regional endpoints.
    """
    return Client(
        enterprise=True,
        project=config.google_cloud_project(),
        location=config.google_cloud_location(),
    )


class BreedMatchError(RuntimeError):
    """The matcher could not reach Gemini or could not read its answer."""


def match_breed(requested_breed: str, folders: list[str] | None = None) -> BreedMatch:
    """Find the corpus folder for a requested breed name.

    Pass `folders` to test this stage without touching Cloud Storage; otherwise
    the cached corpus listing is used.
    """
    if folders is None:
        folders = list(storage.list_breed_folders())

    folder_list = "\n".join(folders)
    prompt = (
        f"Available folders:\n{folder_list}\n\n"
        f"Requested breed: {requested_breed!r}\n\n"
        "Which folder does this request refer to?"
    )

    try:
        response = _client().models.generate_content(
            model=config.BREED_MATCH_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_SYSTEM_INSTRUCTION,
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=_BreedMatchResponse,
            ),
        )
    except Exception as exc:  # noqa: BLE001 — network/quota/auth all report the same way
        raise BreedMatchError(f"Gemini breed matching failed: {exc}") from exc

    parsed = response.parsed
    if not isinstance(parsed, _BreedMatchResponse):
        raise BreedMatchError(f"Gemini returned an unreadable match result: {response.text!r}")

    return _validate(parsed, folders)


def _validate(parsed: _BreedMatchResponse, folders: list[str]) -> BreedMatch:
    """Hold the model to the folder list it was given.

    A hallucinated folder name is treated as no match at all rather than
    corrected, because a near-miss correction is exactly the guess this stage
    exists to avoid.
    """
    matched = parsed.matched_folder
    note = parsed.note

    if matched is not None and matched not in folders:
        logger.warning("Gemini proposed a folder outside the corpus: %r", matched)
        return BreedMatch(
            matched_folder=None,
            confidence="none",
            alternatives=[],
            note=(
                f"The matcher proposed {matched!r}, which is not in the corpus. "
                "Treating this as no match."
            ),
        )

    alternatives = [name for name in parsed.alternatives if name in folders and name != matched]

    confidence = parsed.confidence.lower().strip()
    if matched is None:
        confidence = "none"

    return BreedMatch(
        matched_folder=matched,
        confidence=confidence,
        alternatives=alternatives,
        note=note,
    )
