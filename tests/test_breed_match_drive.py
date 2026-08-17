"""Breed-match validation and Drive helpers, without calling either service.

The matcher's guard rail is tested here rather than the model's judgement: a
folder the model invents must be treated as no match at all, because a
near-miss correction is exactly the guess the stage exists to avoid.
"""

from __future__ import annotations

import pytest

from src.breed_match import _BreedMatchResponse, _validate
from src.drive import _describe, _stale_token, normalize_folder_name

FOLDERS = [
    "1043-n000001-Shiba_Dog",
    "1160-n000003-Siberian_husky",
    "1324-n000004-malamute",
]


# ── breed match validation ────────────────────────────────────────────────────


def test_clear_match_passes_through():
    result = _validate(
        _BreedMatchResponse(
            matched_folder="1160-n000003-Siberian_husky", confidence="high", note="ok"
        ),
        FOLDERS,
    )
    assert result.is_match
    assert result.is_ambiguous is False


def test_hallucinated_folder_becomes_no_match():
    # Not corrected to the nearest real folder — refused outright.
    result = _validate(
        _BreedMatchResponse(matched_folder="9999-n000009-poodle", confidence="high"),
        FOLDERS,
    )
    assert result.matched_folder is None
    assert result.confidence == "none"
    assert "not in the corpus" in result.note


def test_alternatives_make_a_match_ambiguous():
    result = _validate(
        _BreedMatchResponse(
            matched_folder="1160-n000003-Siberian_husky",
            confidence="high",
            alternatives=["1324-n000004-malamute"],
        ),
        FOLDERS,
    )
    assert result.is_ambiguous is True


def test_low_confidence_is_ambiguous_even_without_alternatives():
    result = _validate(
        _BreedMatchResponse(matched_folder="1324-n000004-malamute", confidence="medium"),
        FOLDERS,
    )
    assert result.is_ambiguous is True


def test_invented_alternatives_are_dropped():
    result = _validate(
        _BreedMatchResponse(
            matched_folder="1324-n000004-malamute",
            confidence="medium",
            alternatives=["9999-n000009-invented", "1160-n000003-Siberian_husky"],
        ),
        FOLDERS,
    )
    assert result.alternatives == ["1160-n000003-Siberian_husky"]


def test_match_is_not_listed_as_its_own_alternative():
    result = _validate(
        _BreedMatchResponse(
            matched_folder="1324-n000004-malamute",
            confidence="medium",
            alternatives=["1324-n000004-malamute"],
        ),
        FOLDERS,
    )
    assert result.alternatives == []


def test_null_match_is_forced_to_none_confidence():
    result = _validate(
        _BreedMatchResponse(matched_folder=None, confidence="high"), FOLDERS
    )
    assert result.confidence == "none"
    assert result.is_match is False


def test_confidence_casing_is_normalised():
    result = _validate(
        _BreedMatchResponse(matched_folder="1324-n000004-malamute", confidence="HIGH"),
        FOLDERS,
    )
    assert result.is_ambiguous is False


# ── drive helpers ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "breed,expected",
    [
        ("Siberian Husky", "siberian_husky"),
        ("SHIBA INU", "shiba_inu"),
        ("  golden   retriever  ", "golden_retriever"),
        ("malamute", "malamute"),
    ],
)
def test_folder_names_are_normalised(breed, expected):
    assert normalize_folder_name(breed) == expected


class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakeHttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.resp = _FakeResponse(status)
        self._message = message

    def __str__(self):
        return self._message


def test_storage_quota_error_explains_the_real_cause():
    # The failure that cost the most time: it reads as a permissions problem.
    err = _describe(
        "Could not upload x.jpg",
        _FakeHttpError(403, "Service Accounts do not have storage quota"),
    )
    assert "service accounts have no Drive storage quota" in str(err)
    assert "authorize_drive" in str(err)


def test_missing_scope_error_points_at_the_fix():
    err = _describe("Could not search", _FakeHttpError(403, "insufficient scope"))
    assert "Drive scope" in str(err)


def test_not_found_mentions_reachability():
    err = _describe("Could not create", _FakeHttpError(404, "File not found"))
    assert "DRIVE_OUTPUT_FOLDER_ID" in str(err)


def test_stale_token_error_names_the_remedy():
    err = _stale_token("Could not upload x.jpg", Exception("invalid_grant"))
    assert "authorize_drive.py" in str(err)
    assert "Testing mode" in str(err)
