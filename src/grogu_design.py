"""User-scoped design taste: what good looks like to the person Grogu assists.

Separate from `grogu_personal_memory` for the same reason that is separate from
repository intelligence — it answers a different question and is queried on a
different schedule. Design taste is *cross-repository*: a person who likes
restrained, deferential interfaces likes them in the CLI and in the web app.
Repository-specific design constraints (an existing design system, brand rules)
belong in that repository's `.grogu/roles/designer.md` overlay instead.

The write contract is deliberately the same as personal memory's, because the
failure it prevents is the same one. Explicit statements from the user are
recorded with `remember`. Anything *inferred* — "they rejected that layout, so
they must dislike dense toolbars" — goes through `suggest` and stays pending
until the user confirms it. An agent that silently learns a taste the user never
expressed will confidently produce work they never wanted, and will keep doing
it, since nothing in the loop distinguishes an inferred preference from a stated
one after the fact.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import functools
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional

SCHEMA_VERSION = 1

# Scope keeps a rule about touch targets from being applied to a CLI.
SCOPES = ("all", "cli", "web", "ios", "macos", "mobile", "desktop", "api")

CONFIRMED = "confirmed"
PENDING = "pending"


class DesignError(Exception):
    """A design taste operation that cannot be performed."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def _slug(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit] or "principle"


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf8")
    os.replace(temporary, path)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


# Written by Grogu, not copied from anyone's published guidelines: these are
# plain-language statements of the patterns the user asked for, phrased as
# decisions a designer can actually apply.
APPLE_PRINCIPLES = [
    {
        "statement": "Defer to the content. Chrome, borders and decoration recede; the thing the user came for is the most prominent element on screen.",
        "scope": "all",
        "rationale": "Interface that competes with content makes the product feel busy and cheap.",
    },
    {
        "statement": "Establish one clear hierarchy per screen: a single primary action, secondary actions visibly subordinate, destructive actions never adjacent to the primary one.",
        "scope": "all",
        "rationale": "Ambiguous hierarchy is what makes an interface feel like a form rather than a tool.",
    },
    {
        "statement": "Use generous, consistent spacing on a fixed scale rather than ad hoc padding. Whitespace is structure, not waste.",
        "scope": "all",
        "rationale": "Consistent rhythm is most of what reads as 'polished' before anyone notices a single component.",
    },
    {
        "statement": "Prefer the platform's native control for a job over a custom reimplementation of it.",
        "scope": "all",
        "rationale": "Native controls inherit accessibility, keyboard behavior and platform conventions for free, and custom ones almost never re-earn them.",
    },
    {
        "statement": "Motion explains a relationship — where something came from, what it turned into. Animation that only decorates is removed.",
        "scope": "all",
        "rationale": "Purposeful motion aids comprehension; gratuitous motion costs time on every single use.",
    },
    {
        "statement": "Restrict the palette. One accent color carries interaction; everything else is neutral. Color means something specific or it is not used.",
        "scope": "all",
        "rationale": "When several colors compete for meaning, none of them signal anything.",
    },
    {
        "statement": "Type carries hierarchy through weight and size on a small scale, not through many families or arbitrary sizes.",
        "scope": "all",
        "rationale": "A restrained type scale is what makes dense information stay readable.",
    },
    {
        "statement": "Progressive disclosure: show the common path first and keep advanced options one deliberate step away, present but not in the way.",
        "scope": "all",
        "rationale": "Front-loading every option punishes the ninety percent case to serve the ten.",
    },
    {
        "statement": "Empty, loading and error states are designed, not left to the framework's defaults.",
        "scope": "all",
        "rationale": "These states are a large share of real usage and are where unfinished products reveal themselves.",
    },
    {
        "statement": "Accessibility is part of the design, not a later pass: real contrast ratios, full keyboard paths, honoured reduced-motion and dynamic type.",
        "scope": "all",
        "rationale": "Retrofitted accessibility distorts a layout; designed-in accessibility rarely changes it.",
    },
    {
        "statement": "Output is quiet by default. Print what the user asked for, keep progress noise off the success path, and reserve emphasis for things that actually need attention.",
        "scope": "cli",
        "rationale": "A command that shouts on success trains people to ignore it when it matters.",
    },
    {
        "statement": "Errors state what happened, why, and the exact next command that fixes it.",
        "scope": "cli",
        "rationale": "An error that only reports failure hands the diagnosis back to the user.",
    },
    {
        "statement": "Align columns and keep a stable field order so output can be scanned vertically and parsed by a script.",
        "scope": "cli",
        "rationale": "Terminal output is read by eye and by pipe, and both want the same discipline.",
    },
]


def serialised(method):
    """Run a read-modify-write under the store lock.

    Two agents can be recording taste at the same time, and an unlocked
    read-modify-write loses one of them silently — which is the worst way to
    lose a preference, since nothing looks broken afterwards.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.locked():
            return method(self, *args, **kwargs)

    return wrapper


class DesignStore:
    """Cross-repository design principles, learned deliberately."""

    def __init__(self, home: Optional[Path] = None) -> None:
        self.home = Path(
            home or os.environ.get("GROGU_HOME", Path.home() / ".grogu")
        ).expanduser()
        self.directory = self.home / "design"
        self._lock_depth = 0
        self.principles_path = self.directory / "principles.json"
        self.pending_path = self.directory / "pending.json"

    # -- storage -----------------------------------------------------------

    def _principles(self) -> dict:
        payload = _read_json(self.principles_path)
        payload.setdefault("schema_version", SCHEMA_VERSION)
        payload.setdefault("principles", [])
        return payload

    def _pending(self) -> dict:
        payload = _read_json(self.pending_path)
        payload.setdefault("schema_version", SCHEMA_VERSION)
        payload.setdefault("candidates", [])
        return payload

    @contextmanager
    def locked(self):
        """Serialise mutations; two agents may be recording taste at once.

        Reentrant, because flock is per-descriptor rather than per-process: a
        method that takes the lock and calls another that does the same would
        otherwise wait on itself forever.
        """
        if getattr(self, "_lock_depth", 0):
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = os.open(self.directory / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        self._lock_depth = 1
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            self._lock_depth = 0
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)

    def status(self) -> dict:
        principles = self._principles()["principles"]
        return {
            "directory": str(self.directory),
            "principles": len(principles),
            "pending": len(self._pending()["candidates"]),
            "scopes": sorted({item["scope"] for item in principles}),
        }

    def _unique_id(self, statement: str, existing: List[dict]) -> str:
        base = _slug(statement)
        taken = {item["id"] for item in existing}
        if base not in taken:
            return base
        index = 2
        while f"{base}-{index}" in taken:
            index += 1
        return f"{base}-{index}"

    # -- explicit capture --------------------------------------------------

    @serialised
    def remember(
        self,
        statement: str,
        *,
        scope: str = "all",
        rationale: str = "",
        examples: Optional[List[str]] = None,
        anti_examples: Optional[List[str]] = None,
        source: str = "user",
    ) -> dict:
        if not statement.strip():
            raise DesignError("a design principle needs a statement")
        if scope not in SCOPES:
            raise DesignError(f"unknown scope {scope!r}; expected one of {', '.join(SCOPES)}")
        payload = self._principles()
        principle = {
            "id": self._unique_id(statement, payload["principles"]),
            "at": now(),
            "statement": statement.strip(),
            "scope": scope,
            "rationale": rationale.strip(),
            "examples": list(examples or []),
            "anti_examples": list(anti_examples or []),
            "source": source,
            "status": CONFIRMED,
        }
        payload["principles"].append(principle)
        _write_json(self.principles_path, payload)
        return principle

    @serialised
    def forget(self, principle_id: str) -> bool:
        payload = self._principles()
        remaining = [item for item in payload["principles"] if item["id"] != principle_id]
        if len(remaining) == len(payload["principles"]):
            return False
        payload["principles"] = remaining
        _write_json(self.principles_path, payload)
        return True

    # -- inferred candidates -----------------------------------------------

    @serialised
    def suggest(
        self,
        statement: str,
        *,
        scope: str = "all",
        rationale: str = "",
        evidence: str = "",
        source: str = "observed",
        confidence: float = 0.5,
    ) -> dict:
        """Queue an inferred preference. It does not apply until confirmed.

        This is the path for "the user rejected that layout": a real signal, but
        a guess about *why*. Confirming is the user's to do.
        """
        if not statement.strip():
            raise DesignError("a design candidate needs a statement")
        if scope not in SCOPES:
            raise DesignError(f"unknown scope {scope!r}")
        payload = self._pending()
        candidate = {
            "id": self._unique_id(statement, payload["candidates"]),
            "at": now(),
            "statement": statement.strip(),
            "scope": scope,
            "rationale": rationale.strip(),
            "evidence": evidence.strip(),
            "source": source,
            "confidence": float(confidence),
            "status": PENDING,
        }
        payload["candidates"].append(candidate)
        _write_json(self.pending_path, payload)
        return candidate

    def review(self, limit: int = 50) -> List[dict]:
        return self._pending()["candidates"][:limit]

    @serialised
    def confirm(self, candidate_id: str) -> dict:
        payload = self._pending()
        for candidate in payload["candidates"]:
            if candidate["id"] == candidate_id:
                break
        else:
            raise DesignError(f"no pending design candidate {candidate_id!r}")
        payload["candidates"] = [
            item for item in payload["candidates"] if item["id"] != candidate_id
        ]
        _write_json(self.pending_path, payload)
        return self.remember(
            candidate["statement"],
            scope=candidate["scope"],
            rationale=candidate["rationale"],
            source=f"confirmed:{candidate['source']}",
        )

    @serialised
    def reject(self, candidate_id: str) -> bool:
        payload = self._pending()
        remaining = [item for item in payload["candidates"] if item["id"] != candidate_id]
        if len(remaining) == len(payload["candidates"]):
            return False
        payload["candidates"] = remaining
        _write_json(self.pending_path, payload)
        return True

    # -- reads -------------------------------------------------------------

    def recall(self, *, query: str = "", scope: str = "", limit: int = 20) -> List[dict]:
        """Bounded, relevant principles — never the whole store.

        The designer's prompt is finite. Twenty principles that apply beat
        eighty that mostly do not, and scope is what makes that selection
        honest rather than arbitrary.
        """
        principles = self._principles()["principles"]
        if scope:
            principles = [
                item for item in principles if item["scope"] in (scope, "all")
            ]
        if query:
            terms = [term for term in re.split(r"\W+", query.lower()) if term]

            def score(item: dict) -> int:
                haystack = f"{item['statement']} {item['rationale']}".lower()
                return sum(1 for term in terms if term in haystack)

            principles = sorted(
                (item for item in principles if score(item)),
                key=score,
                reverse=True,
            ) or principles
        return principles[:limit]

    @serialised
    def seed_apple(self) -> List[dict]:
        """Record the Apple-style patterns the user asked for, once."""
        existing = {item["statement"] for item in self._principles()["principles"]}
        added = []
        for principle in APPLE_PRINCIPLES:
            if principle["statement"] in existing:
                continue
            added.append(
                self.remember(
                    principle["statement"],
                    scope=principle["scope"],
                    rationale=principle["rationale"],
                    source="user:apple-patterns",
                )
            )
        return added
