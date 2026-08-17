"""Crop geometry and the minimum-crop gate.

The crop is the one stage that can silently corrupt the dataset — an off-by-one
here produces images that look fine individually and train a worse model. So the
invariants are asserted directly: the crop never leaves the image, never
distorts, and never falls below the training input size.
"""

from __future__ import annotations

import io

import pytest
from conftest import annotation_xml, encode, photo
from PIL import Image

from src import config, processing, storage
from src.models import BoundingBox, Candidate
from src.processing import CropTooSmall, _padded_crop_box, _output_filename


# ── the padded crop box ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bbox,width,height",
    [
        pytest.param(BoundingBox(0, 0, 300, 300), 500, 400, id="anchored-top-left"),
        pytest.param(BoundingBox(200, 100, 500, 400), 500, 400, id="anchored-bottom-right"),
        pytest.param(BoundingBox(0, 0, 500, 332), 500, 332, id="box-is-whole-image"),
        pytest.param(BoundingBox(79, 39, 500, 332), 500, 332, id="real-corpus-box"),
        pytest.param(BoundingBox(1, 1, 499, 331), 500, 332, id="one-pixel-margin"),
        pytest.param(BoundingBox(240, 160, 260, 180), 500, 332, id="tiny-central-box"),
    ],
)
def test_crop_box_never_leaves_the_image(bbox, width, height):
    box = _padded_crop_box(bbox, width, height)
    assert 0 <= box.xmin < box.xmax <= width
    assert 0 <= box.ymin < box.ymax <= height


def test_padding_expands_the_box():
    bbox = BoundingBox(200, 150, 300, 250)  # 100x100, centred, room on all sides
    box = _padded_crop_box(bbox, 500, 400)
    pad = round(100 * config.CROP_PADDING_RATIO)
    assert box.xmin == 200 - pad
    assert box.xmax == 300 + pad
    assert box.width == 100 + 2 * pad


def test_padding_is_clamped_not_shifted_at_the_edge():
    # A box touching the left edge cannot gain margin on that side, and the
    # crop must not slide right to compensate — that would move the subject.
    bbox = BoundingBox(0, 150, 100, 250)
    box = _padded_crop_box(bbox, 500, 400)
    assert box.xmin == 0


def test_crop_keeps_the_native_aspect_ratio():
    # Deliberately not square: forcing square is what caused letterboxing.
    bbox = BoundingBox(79, 39, 500, 332)
    box = _padded_crop_box(bbox, 500, 332)
    assert box.width != box.height


# ── process_candidate ─────────────────────────────────────────────────────────


def _stub_annotation(monkeypatch, xml: bytes | None):
    monkeypatch.setattr(storage, "download_annotation", lambda folder, name: xml)


def test_crops_to_the_annotated_box(monkeypatch):
    # A box with room around it on every side, so the 25% margin lands inside
    # the frame and the result is a genuine sub-region rather than the whole
    # image. The corpus's usual full-width boxes saturate the padding and crop
    # to the full frame, which would be indistinguishable from the fallback.
    _stub_annotation(monkeypatch, annotation_xml(xmin=150, ymin=100, xmax=350, ymax=250))
    candidate = Candidate("b", "n1.jpg", image_bytes=encode(photo(500, 332)))

    result = processing.process_candidate(candidate, "breed")

    pad_x = round(200 * config.CROP_PADDING_RATIO)
    pad_y = round(150 * config.CROP_PADDING_RATIO)
    expected = (200 + 2 * pad_x, 150 + 2 * pad_y)

    assert result.used_fallback_crop is False
    assert (result.width, result.height) == expected
    with Image.open(io.BytesIO(result.image_bytes)) as out:
        assert out.size == expected


def test_wide_corpus_box_crops_to_the_full_frame(monkeypatch):
    # The common real case: the body box spans nearly the whole photo, so the
    # padded box clamps to the image on every side. Documented because it looks
    # like the fallback but is not one.
    _stub_annotation(monkeypatch, annotation_xml())
    candidate = Candidate("b", "n1.jpg", image_bytes=encode(photo(500, 332)))

    result = processing.process_candidate(candidate, "breed")

    assert result.used_fallback_crop is False
    assert (result.width, result.height) == (500, 332)


def test_no_annotation_keeps_the_full_frame_and_flags_it(monkeypatch):
    # A missing annotation must not reject the image, and must not crop blind.
    _stub_annotation(monkeypatch, None)
    candidate = Candidate("b", "n1.jpg", image_bytes=encode(photo(500, 332)))

    result = processing.process_candidate(candidate, "breed")

    assert result.used_fallback_crop is True
    assert (result.width, result.height) == (500, 332)


def test_malformed_annotation_falls_back_rather_than_raising(monkeypatch):
    _stub_annotation(monkeypatch, b"<annotation><broken>")
    candidate = Candidate("b", "n1.jpg", image_bytes=encode(photo(500, 332)))

    assert processing.process_candidate(candidate, "breed").used_fallback_crop is True


def test_crop_below_training_size_is_rejected(monkeypatch):
    # A large photo of a small, distant dog: passes a source-resolution check
    # and still yields a crop too small to train on.
    small_box = annotation_xml(xmin=240, ymin=160, xmax=300, ymax=220)
    _stub_annotation(monkeypatch, small_box)
    candidate = Candidate("b", "n1.jpg", image_bytes=encode(photo(500, 332)))

    with pytest.raises(CropTooSmall):
        processing.process_candidate(candidate, "breed")


def test_gate_is_tied_to_the_training_input_size():
    assert config.MIN_CROP_SIZE == config.TRAINING_INPUT_SIZE


def test_surviving_crops_clear_the_gate(monkeypatch):
    _stub_annotation(monkeypatch, annotation_xml())
    candidate = Candidate("b", "n1.jpg", image_bytes=encode(photo(500, 332)))

    result = processing.process_candidate(candidate, "breed")

    assert min(result.width, result.height) >= config.MIN_CROP_SIZE


def test_candidate_without_image_data_raises():
    with pytest.raises(ValueError):
        processing.process_candidate(Candidate("b", "n1.jpg"), "breed")


def test_process_all_reports_gate_rejections_without_aborting(monkeypatch):
    calls = {"n": 0}

    def alternating(folder, name):
        calls["n"] += 1
        # Every other image gets a box too small to survive the gate.
        return annotation_xml() if calls["n"] % 2 else annotation_xml(240, 160, 300, 220)

    monkeypatch.setattr(storage, "download_annotation", alternating)
    candidates = [
        Candidate("b", f"n{i}.jpg", image_bytes=encode(photo(500, 332, seed=i)))
        for i in range(4)
    ]

    processed, failures = processing.process_all(candidates, "breed")

    assert len(processed) == 2
    assert len(failures) == 2
    assert all("crop too small" in reason for _, reason in failures)


@pytest.mark.parametrize(
    "source,expected",
    [("n1.jpg", "n1.jpg"), ("n1.jpeg", "n1.jpg"), ("n1.JPEG", "n1.jpg")],
)
def test_output_is_always_jpg(source, expected):
    assert _output_filename(source) == expected


def test_resize_is_off_by_default():
    # The training pipeline resizes to 224 itself; resizing here would resample
    # twice. If this ever flips, the crops stop being native resolution.
    assert config.OUTPUT_SIZE is None
