"""Restricted canonical JSON used by Grogu plan documents.

``canonical:v1`` follows RFC 8785 for the subset accepted by Grogu, but it is
deliberately not a general JCS implementation:

* floating-point values, NaN and infinities are forbidden;
* integers are limited to the inclusive range ``[-2**53, 2**53]``;
* duplicate object keys are rejected, including keys that collide after NFC
  normalization;
* every string is NFC-normalized and must contain Unicode scalar values;
* object keys are ordered by UTF-16 code units;
* arrays retain their declared order.  Callers must sort schema-declared
  unordered collections by stable id before canonicalizing them.

These restrictions avoid the number-serialization cases where Python's JSON
encoder differs from JCS while retaining byte-compatible output for values in
the accepted profile.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

PROFILE = "canonical:v1"
MIN_INTEGER = -(2**53)
MAX_INTEGER = 2**53
_ID = re.compile(r"^(?P<prefix>[a-z][a-z0-9_]*)-(?P<number>[0-9]+)$")


class CanonicalError(ValueError):
    """A value cannot be represented by ``canonical:v1``."""


class DuplicateKeyError(CanonicalError):
    """A JSON object repeats a key before or after NFC normalization."""


def _path(parent: str, child: object) -> str:
    if isinstance(child, int):
        return f"{parent}[{child}]"
    return f"{parent}.{child}" if parent != "$" else f"$.{child}"


def normalize_string(value: str, *, path: str = "$") -> str:
    """Return an NFC string and reject lone UTF-16 surrogate code points."""
    if not isinstance(value, str):
        raise CanonicalError(f"{path}: expected a string")
    normalized = unicodedata.normalize("NFC", value)
    if any(0xD800 <= ord(character) <= 0xDFFF for character in normalized):
        raise CanonicalError(f"{path}: strings must contain Unicode scalar values")
    return normalized


def utf16_key(value: str) -> bytes:
    """Return the canonical object-key ordering key."""
    normalized = normalize_string(value, path="$<key>")
    return normalized.encode("utf-16-be")


def normalize(value: Any, *, path: str = "$") -> Any:
    """Normalize and validate a JSON-compatible value for canonical encoding."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value < MIN_INTEGER or value > MAX_INTEGER:
            raise CanonicalError(
                f"{path}: integer {value} is outside [{MIN_INTEGER}, {MAX_INTEGER}]"
            )
        return value
    if isinstance(value, float):
        raise CanonicalError(f"{path}: floating-point values are not allowed")
    if isinstance(value, str):
        return normalize_string(value, path=path)
    if isinstance(value, Mapping):
        normalized_items: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError(f"{path}: object keys must be strings")
            normalized_key = normalize_string(key, path=f"{path}<key>")
            if normalized_key in normalized_items:
                raise DuplicateKeyError(
                    f"{path}: duplicate key {normalized_key!r} after NFC normalization"
                )
            normalized_items[normalized_key] = normalize(
                item, path=_path(path, normalized_key)
            )
        return {
            key: normalized_items[key]
            for key in sorted(normalized_items, key=utf16_key)
        }
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [normalize(item, path=_path(path, index)) for index, item in enumerate(value)]
    raise CanonicalError(
        f"{path}: {type(value).__name__} is not a canonical JSON value"
    )


def dumps(value: Any) -> str:
    """Serialize *value* to canonical UTF-8 JSON text."""
    normalized = normalize(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=False,
    )


def dumpb(value: Any) -> bytes:
    """Serialize *value* to canonical UTF-8 bytes."""
    return dumps(value).encode("utf8")


def _reject_float(token: str) -> None:
    raise CanonicalError(f"$: floating-point value {token!r} is not allowed")


def _reject_constant(token: str) -> None:
    raise CanonicalError(f"$: non-finite value {token!r} is not allowed")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for raw_key, item in pairs:
        key = normalize_string(raw_key, path="$<key>")
        if key in value:
            raise DuplicateKeyError(f"$: duplicate key {key!r}")
        value[key] = item
    return value


def loads(source: str | bytes | bytearray) -> Any:
    """Parse JSON while rejecting duplicate keys and values outside the profile."""
    if isinstance(source, (bytes, bytearray)):
        try:
            source = bytes(source).decode("utf8")
        except UnicodeDecodeError as error:
            raise CanonicalError("$: canonical JSON must be UTF-8") from error
    if not isinstance(source, str):
        raise TypeError("source must be str, bytes, or bytearray")
    try:
        value = json.loads(
            source,
            object_pairs_hook=_object_pairs,
            parse_float=_reject_float,
            parse_constant=_reject_constant,
        )
    except CanonicalError:
        raise
    except (json.JSONDecodeError, UnicodeError) as error:
        raise CanonicalError(f"$: invalid JSON: {error}") from error
    return normalize(value)


def is_canonical(source: str | bytes | bytearray) -> bool:
    """Return whether *source* is already the canonical encoding of its value."""
    try:
        raw = bytes(source) if isinstance(source, (bytes, bytearray)) else source.encode("utf8")
        return dumpb(loads(raw)) == raw
    except (CanonicalError, UnicodeError, AttributeError):
        return False


def digest(value: Any) -> str:
    """Return a prefixed SHA-256 digest of canonical bytes."""
    return "sha256:" + hashlib.sha256(dumpb(value)).hexdigest()


def digest_bytes(payload: bytes) -> str:
    """Return a prefixed SHA-256 digest of already-defined bytes."""
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def id_sort_key(identifier: str) -> tuple[str, int, str]:
    """Sort stable ``prefix-number`` ids numerically, then fall back to text."""
    match = _ID.fullmatch(str(identifier))
    if match is None:
        return str(identifier), -1, str(identifier)
    return match.group("prefix"), int(match.group("number")), str(identifier)


def sort_unordered_by_id(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Normalize an unordered schema collection into stable-id order."""
    normalized = [normalize(value) for value in values]
    if any(not isinstance(value.get("id"), str) for value in normalized):
        raise CanonicalError("$: unordered collection members require string ids")
    return sorted(normalized, key=lambda value: id_sort_key(value["id"]))


canonical_dumps = dumps
canonical_loads = loads
canonical_digest = digest
