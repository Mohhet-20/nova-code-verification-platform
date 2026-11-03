from __future__ import annotations

from app.services.intelligence.language_detection import detect_language


def test_extension_detection_is_separate_from_parser_capability() -> None:
    assert detect_language("a.py").language == "Python"
    assert detect_language("a.TSX").language == "TSX"
    assert detect_language("a.jsx").language == "JSX"
    assert detect_language("main.go").language == "Go"  # detected even though NOVA cannot parse Go yet


def test_filename_and_shebang() -> None:
    assert detect_language("Dockerfile").language == "Dockerfile"
    assert detect_language("run", b"#!/usr/bin/env python3\nprint(1)\n").language == "Python"
    assert detect_language("run", b"#!/bin/bash\n").language == "Shell"


def test_ambiguous_extension_is_low_confidence() -> None:
    g = detect_language("x.h")
    assert g.confidence == "low" and g.alternatives


def test_unknown_extension() -> None:
    assert detect_language("mystery.zzz").language == "Other"
