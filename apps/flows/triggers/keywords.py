"""Keyword matching — SPEC §10's three modes, and the one distinction that matters.

``exact`` and ``contains`` are what they sound like. ``any_word`` is the one
worth writing down, because it is the only thing that separates it from
``contains`` and it is the thing people complain about: ``"cat"`` in ``any_word``
matches *"my cat sleeps"* and does **not** match *"category"*, where ``contains``
matches both. A test pins that pair.

Case-insensitive and trimmed on both sides, per SPEC. ``casefold()`` rather than
``lower()``: "case-insensitive" is a SPEC word and ``lower()`` gets German ß and
Turkish İ wrong, which is not hypothetical for a product whose contacts are
strangers on the internet.
"""

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

__all__ = ["MAX_MATCH_CHARS", "matches_any", "normalise", "strip_accents"]

#: How much of a message is scanned.
MAX_MATCH_CHARS = 8192

#: ``\w+`` under Unicode, so "café" and "naïve" are single words.
_WORD_RE = re.compile(r"\w+", re.UNICODE)


def strip_accents(s: str) -> str:
    """Decompose Unicode characters and remove combining accent marks (diacritics)."""
    if not isinstance(s, str):
        return ""
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalise(text: Any, *, ignore_accents: bool = False) -> str:
    """Trimmed, casefolded and capped — optionally stripped of accents."""
    if not isinstance(text, str):
        return ""
    result = text.strip()[:MAX_MATCH_CHARS].casefold()
    if ignore_accents:
        result = strip_accents(result)
    return result


def matches_any(text: str, keywords: Iterable[Any], *, ignore_accents: bool = False) -> bool:
    """Whether ``text`` matches any of these ``{"text", "mode"}`` entries.

    Supports configurable accent normalization via ``ignore_accents`` parameter
    or per-keyword ``ignore_accents`` flag.
    Preserves exact, contains, and any_word matching semantics.
    """
    if not text:
        return False

    haystack_raw = normalise(text, ignore_accents=False)
    if not haystack_raw:
        return False

    haystack_no_accents: str | None = None
    words_raw: set[str] | None = None
    words_no_accents: set[str] | None = None

    for keyword in keywords:
        if isinstance(keyword, str):
            kw_text = keyword
            mode = "contains"
            kw_ignore = ignore_accents
        elif isinstance(keyword, dict):
            kw_text = keyword.get("text")
            mode = keyword.get("mode") or "contains"
            kw_ignore = bool(keyword.get("ignore_accents", ignore_accents))
        else:
            continue

        if not kw_text or not isinstance(kw_text, str):
            continue

        needle = normalise(kw_text, ignore_accents=kw_ignore)
        if not needle:
            continue

        if kw_ignore:
            if haystack_no_accents is None:
                haystack_no_accents = strip_accents(haystack_raw)
            haystack = haystack_no_accents
        else:
            haystack = haystack_raw

        if mode == "exact":
            if haystack == needle:
                return True
        elif mode == "contains":
            if needle in haystack:
                return True
        elif mode == "any_word":
            if kw_ignore:
                if words_no_accents is None:
                    words_no_accents = set(_WORD_RE.findall(haystack))
                words = words_no_accents
            else:
                if words_raw is None:
                    words_raw = set(_WORD_RE.findall(haystack))
                words = words_raw
            if needle in words:
                return True

    return False
