"""Shared, deterministic safety policy for Brain candidates."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_SOURCE_SCHEMES = {"session", "file", "url", "git", "email", "ticket", "meeting"}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:api[_-]?key|token|password|passwd|secret)\b\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{8,}", re.I),
    re.compile(r"\b(?:sk|xox[baprs])-[A-Za-z0-9_-]{12,}\b", re.I),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b", re.I),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b", re.I),
    re.compile(r"\bAccountKey=|SharedAccessSignature=|sig=[A-Za-z0-9%]{20,}", re.I),
)
PII_PATTERNS = (
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I),
    # IBAN. Le negative lookahead ecarte les identifiants HEXADECIMAUX (run ids, sha tronques :
    # ce05033523d85dd8 satisfaisait 2 lettres + 2 chiffres + 12 alphanum, ce qui faisait refuser
    # des candidats legitimes. Un IBAN reel porte toujours au moins un caractere hors [0-9a-f].
    # IBAN. Le negative lookahead ecarte les identifiants HEXADECIMAUX (run ids, sha tronques :
    # ce05033523d85dd8 satisfaisait 2 lettres + 2 chiffres + 12 alphanum et faisait refuser des
    # candidats legitimes. Un IBAN reel porte toujours au moins un caractere hors [0-9a-f].
    re.compile(r"\b(?![0-9a-f]+\b)[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]){11,30}\b", re.I),
    re.compile(r"\b(?:\+33|0)[1-9](?:[ .-]?\d{2}){4}\b"),
)


def contains_likely_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def contains_likely_pii(text: str) -> bool:
    return any(pattern.search(text) for pattern in PII_PATTERNS)


def valid_source(source: str) -> bool:
    scheme, separator, locator = source.strip().partition(":")
    scheme, locator = scheme.casefold(), locator.strip()
    if separator != ":" or scheme not in ALLOWED_SOURCE_SCHEMES or not locator:
        return False
    if scheme == "session":
        return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{5,127}", locator) is not None
    if scheme == "file":
        return Path(locator).expanduser().is_file()
    if scheme == "url":
        parsed = urlparse(locator)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    if scheme == "git":
        return re.fullmatch(r".+@[0-9a-fA-F]{7,64}", locator) is not None
    if scheme == "email":
        # Opaque provider/message identifier only; addresses are PII and must never be metadata.
        return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", locator) is not None
    if scheme == "ticket":
        return re.fullmatch(r"[A-Z][A-Z0-9]{1,15}-\d{1,12}", locator) is not None
    if scheme == "meeting":
        return re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T /][A-Za-z0-9._:+ -]{1,100})?", locator) is not None
    return False


def scan_candidate(title: str, body: str, source: str, metadata_text: str = "") -> str | None:
    if contains_likely_secret("\n".join((title, body, source, metadata_text))):
        return "likely secret detected"
    if contains_likely_pii("\n".join((title, body, source, metadata_text))):
        return "likely personal data detected"
    return None
