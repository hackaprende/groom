"""Annotation parsing.

Corpus files carry a UTF-8 BOM and no XML declaration, and not every one is
well formed. A bad annotation must demote the image to the fallback crop, never
abort the run — so every failure here returns None rather than raising.
"""

from __future__ import annotations

from conftest import annotation_xml

from src.annotations import parse_body_bbox, parse_declared_size


def test_parses_real_corpus_shape_with_bom():
    bbox = parse_body_bbox(annotation_xml())
    assert (bbox.xmin, bbox.ymin, bbox.xmax, bbox.ymax) == (79, 39, 500, 332)
    assert bbox.width == 421
    assert bbox.height == 293


def test_parses_without_bom_too():
    assert parse_body_bbox(annotation_xml(with_bom=False)) is not None


def test_ignores_head_box():
    # The head box in the fixture is 83,62,285,326 — parsing must not pick it up.
    bbox = parse_body_bbox(annotation_xml())
    assert bbox.xmin == 79


def test_accepts_float_coordinates():
    xml = annotation_xml().replace(b"<xmin>79</xmin>", b"<xmin>79.0</xmin>")
    assert parse_body_bbox(xml).xmin == 79


def test_malformed_xml_returns_none():
    assert parse_body_bbox(b"<annotation><unclosed>") is None


def test_missing_body_box_returns_none():
    xml = annotation_xml().replace(b"bodybndbox", b"somethingelse")
    assert parse_body_bbox(xml) is None


def test_unreadable_coordinates_return_none():
    xml = annotation_xml().replace(b"<xmin>79</xmin>", b"<xmin>abc</xmin>")
    assert parse_body_bbox(xml) is None


def test_degenerate_box_returns_none():
    # Zero-width box: xmax equal to xmin.
    assert parse_body_bbox(annotation_xml(xmin=100, xmax=100)) is None


def test_inverted_box_returns_none():
    assert parse_body_bbox(annotation_xml(xmin=400, xmax=100)) is None


def test_declared_size_is_read():
    assert parse_declared_size(annotation_xml()) == (500, 332)


def test_declared_size_survives_malformed_input():
    assert parse_declared_size(b"not xml at all") is None
