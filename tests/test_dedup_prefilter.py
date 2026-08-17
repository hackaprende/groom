"""The two cheap filters that run before any model call.

Deduplication is tested against images that differ in bytes but not in
appearance — recompressed, resized, brightened — because catching those is the
whole point of a perceptual hash over a checksum.
"""

from __future__ import annotations

from conftest import encode, photo
from PIL import Image

from src import config, prefilter, storage
from src.dedup import Deduplicator, deduplicate
from src.models import STAGE_DEDUP, STAGE_PREFILTER, Candidate


# ── dedup ─────────────────────────────────────────────────────────────────────


def _candidate(name: str, image: Image.Image, quality: int = 90) -> Candidate:
    return Candidate("b", name, image_bytes=encode(image, quality))


def test_identical_bytes_are_caught():
    base = photo()
    survivors, rejections = deduplicate(
        [_candidate("a.jpg", base), _candidate("b.jpg", base)]
    )
    assert [c.filename for c in survivors] == ["a.jpg"]
    assert rejections[0].stage == STAGE_DEDUP
    assert "a.jpg" in rejections[0].reason


def test_recompressed_copy_is_caught():
    base = photo()
    survivors, _ = deduplicate(
        [_candidate("a.jpg", base), _candidate("b.jpg", base, quality=40)]
    )
    assert len(survivors) == 1


def test_resized_copy_is_caught():
    base = photo()
    half = base.resize((base.width // 2, base.height // 2))
    survivors, _ = deduplicate([_candidate("a.jpg", base), _candidate("b.jpg", half)])
    assert len(survivors) == 1


def test_brightened_copy_is_caught():
    base = photo()
    brighter = Image.eval(base, lambda p: min(255, int(p * 1.15)))
    survivors, _ = deduplicate([_candidate("a.jpg", base), _candidate("b.jpg", brighter)])
    assert len(survivors) == 1


def test_genuinely_different_images_both_survive():
    survivors, rejections = deduplicate(
        [_candidate("a.jpg", photo(seed=1)), _candidate("b.jpg", photo(seed=99))]
    )
    assert len(survivors) == 2
    assert rejections == []


def test_first_occurrence_is_the_one_kept():
    base = photo()
    survivors, _ = deduplicate(
        [_candidate("first.jpg", base), _candidate("second.jpg", base)]
    )
    assert survivors[0].filename == "first.jpg"


def test_state_persists_across_batches():
    # The pipeline fetches in batches; a duplicate in batch two must still be
    # caught against batch one.
    base = photo()
    deduper = Deduplicator()
    deduper.filter([_candidate("a.jpg", base)])
    survivors, rejections = deduper.filter([_candidate("b.jpg", base)])
    assert survivors == []
    assert len(rejections) == 1


def test_candidate_without_bytes_is_rejected_not_crashed():
    survivors, rejections = deduplicate([Candidate("b", "a.jpg")])
    assert survivors == []
    assert "no image data" in rejections[0].reason


# ── prefilter ─────────────────────────────────────────────────────────────────


def test_prefilter_screens_size_and_decodability(monkeypatch):
    payloads = {
        "good.jpg": encode(photo(500, 332)),
        "tiny.jpg": encode(photo(150, 120)),
        "broken.jpg": b"this is not an image",
    }
    monkeypatch.setattr(storage, "download_bytes", lambda name: payloads[name])
    monkeypatch.setattr(prefilter.storage, "download_bytes", lambda name: payloads[name])
    monkeypatch.setattr(prefilter.storage, "warm_client", lambda: None)

    survivors, rejections = prefilter.prefilter(
        [Candidate(name, name) for name in payloads]
    )

    assert [c.filename for c in survivors] == ["good.jpg"]
    reasons = {r.filename: r.reason for r in rejections}
    assert "below minimum resolution" in reasons["tiny.jpg"]
    assert "unreadable" in reasons["broken.jpg"]
    assert all(r.stage == STAGE_PREFILTER for r in rejections)


def test_prefilter_records_dimensions_on_survivors(monkeypatch):
    monkeypatch.setattr(
        prefilter.storage, "download_bytes", lambda name: encode(photo(500, 332))
    )
    monkeypatch.setattr(prefilter.storage, "warm_client", lambda: None)

    survivors, _ = prefilter.prefilter([Candidate("a", "a.jpg")])

    assert (survivors[0].width, survivors[0].height) == (500, 332)


def test_download_failure_becomes_a_rejection_not_an_exception(monkeypatch):
    def boom(name):
        raise storage.StorageError("network gone")

    monkeypatch.setattr(prefilter.storage, "download_bytes", boom)
    monkeypatch.setattr(prefilter.storage, "warm_client", lambda: None)

    survivors, rejections = prefilter.prefilter([Candidate("a", "a.jpg")])

    assert survivors == []
    assert "download failed" in rejections[0].reason


def test_thresholds_are_the_configured_ones():
    assert config.MIN_SOURCE_WIDTH == 200
    assert config.MIN_SOURCE_HEIGHT == 200
