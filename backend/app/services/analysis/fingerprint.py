"""Finding fingerprints: stable identity across edits that only move code.

fingerprint = sha256(rule id, repository-relative path, enclosing scope, rule-specific discriminator, ordinal)[:40]

The discriminator describes *what* was found (for example the parameter name, the comparison operator, a hash-free structural
description) and never the line number. The ordinal separates several identical constructs inside one scope in source order.
Consequences: inserting or deleting unrelated lines keeps the identity; renaming the file or the enclosing function, or
changing the construct itself, creates a new finding (and resolves the old one); adding an identical construct before an
existing one in the same scope shifts the ordinals of the later ones.
"""
from __future__ import annotations

import hashlib


def make(rule_id: str, rel_path: str, scope: str, discriminator: str, ordinal: int) -> str:
    raw = "\x00".join([rule_id, rel_path, scope, discriminator, str(ordinal)])
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:40]
