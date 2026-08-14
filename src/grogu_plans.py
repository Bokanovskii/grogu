"""Plan artifacts and the architect/engineer/tester pipeline contract.

A plan is a file on disk, never a message passed between agents. Agents are
handed a plan id and a role; they read what that role is allowed to read and
nothing else. Passing plans by reference instead of by context is where the
token savings come from, and it is also what makes the separation between the
implementation plan and the testing plan mean anything.

Layout, alongside the task store in the target repository:

* `<repo>/.grogu/plans/<id>/manifest.json` — status, stages, workstreams,
  amendments, defects, steering, access log. Reviewable in a pull request.
* `<repo>/.grogu/plans/<id>/implementation.md` — plaintext. The engineer reads
  this, and it is the artifact a human reviews.
* `<repo>/.grogu/plans/<id>/testing.sealed` — the testing plan, sealed.
* `<repo>/.grogu/plans/<id>/evaluation.sealed` — the evaluation plan, sealed.
* `<repo>/.grogu/plans/steering.json` — repository-wide steering notes that
  outlive any single plan.

Sealing is `zlib` + base64 behind a header line. It is a guard against
accidents, **not a security boundary**: an agent running as the same user can
trivially decode it. What it buys is that an engineer who opens
`testing.sealed`, greps the repository, or reads a directory listing does not
absorb the testing plan by accident, and that deliberately unsealing it is a
recorded, visible act rather than an invisible one.
"""

from __future__ import annotations

import base64
import datetime as dt
import fcntl
import fnmatch
import json
import os
import re
import subprocess
import secrets
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from grogu_tasks import actor, repository_root, session_id

SCHEMA_VERSION = 1
STORE_DIRNAME = ".grogu"
PLANS_DIRNAME = "plans"

DESIGN = "design"
IMPLEMENTATION = "implementation"
TESTING = "testing"
EVALUATION = "evaluation"
STAGES = (DESIGN, IMPLEMENTATION, TESTING, EVALUATION)
SEALED_STAGES = frozenset({TESTING, EVALUATION})

ARCHITECT = "architect"
DESIGNER = "designer"
ENGINEER = "engineer"
TESTER = "tester"
REVIEWER = "reviewer"
ROLES = (ARCHITECT, DESIGNER, ENGINEER, TESTER, REVIEWER)

# The whole point of the split: the engineer must not be able to write to the
# test, because an implementation shaped by its own unit tests only proves the
# tests were satisfiable.
ROLE_READABLE_STAGES = {
    ARCHITECT: frozenset(STAGES),
    DESIGNER: frozenset({DESIGN, IMPLEMENTATION}),
    ENGINEER: frozenset({IMPLEMENTATION, DESIGN}),
    TESTER: frozenset({TESTING, EVALUATION, DESIGN}),
    REVIEWER: frozenset(STAGES),
}

# The design spec is not sealed, and that is not an inconsistency. A test is a
# *proxy* for correctness, so showing it to the implementer corrupts the proxy;
# a design spec *is* the requirement, so withholding it just makes the work
# impossible. What stays sealed is how the design will be judged, which the
# architect folds into the testing plan.
STAGE_WRITERS = {
    DESIGN: frozenset({DESIGNER}),
    IMPLEMENTATION: frozenset({ARCHITECT}),
    TESTING: frozenset({ARCHITECT}),
    EVALUATION: frozenset({ARCHITECT}),
}

DRAFT = "draft"
APPROVED = "approved"
AMENDING = "amending"
NEEDS_REVIEW = "needs_review"
SUPERSEDED = "superseded"
COMPLETE = "complete"
PLAN_STATUSES = (DRAFT, APPROVED, AMENDING, NEEDS_REVIEW, SUPERSEDED, COMPLETE)
BLOCKING_STATUSES = frozenset({AMENDING, NEEDS_REVIEW, SUPERSEDED})

PENDING = "pending"
IN_PROGRESS = "in_progress"
FAILED = "failed"
STAGE_STATES = (PENDING, IN_PROGRESS, COMPLETE, FAILED)

ACCEPTED = "accepted"
REJECTED = "rejected"
GUIDED = "guided"
AMENDMENT_OUTCOMES = (ACCEPTED, REJECTED, GUIDED)
RESOLVED = "resolved"

KIND_AMENDMENT = "amendment"
KIND_ESCALATION = "escalation"

ROUTE_IMPLEMENTATION = "implementation"
ROUTE_TEST = "test"
ROUTE_PLAN = "plan"
ROUTE_DESIGN = "design"
DEFECT_ROUTES = (ROUTE_IMPLEMENTATION, ROUTE_TEST, ROUTE_PLAN, ROUTE_DESIGN)

PASS = "pass"
CHANGES = "changes"
REVIEW_RUBBER_DUCK = "rubber-duck"
REVIEW_CODE = "code-review"
REVIEW_SECURITY = "security-review"
REVIEW_KINDS = (REVIEW_RUBBER_DUCK, REVIEW_CODE, REVIEW_SECURITY)
DESIGN_VERDICTS = (PASS, CHANGES)

DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_DEFECT_ROUNDS = 3
SEAL_HEADER = "grogu-sealed:v1"

GATE_IMPLEMENT = "implement"
GATE_TEST = "test"
GATE_EVALUATE = "evaluate"
GATES = (GATE_IMPLEMENT, GATE_TEST, GATE_EVALUATE)


class PlanError(Exception):
    """A plan operation the caller asked for cannot be performed."""


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def max_rounds() -> int:
    raw = os.environ.get("GROGU_PLAN_MAX_ROUNDS", "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_MAX_ROUNDS


def max_defect_rounds() -> int:
    raw = os.environ.get("GROGU_PLAN_MAX_DEFECT_ROUNDS", "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_MAX_DEFECT_ROUNDS


def head_commit(root: Optional[Path] = None) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root or Path.cwd()),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def current_role() -> str:
    """Role of the agent making this call, when it declared one."""
    role = os.environ.get("GROGU_ROLE", "").strip().lower()
    return role if role in ROLES else ""


# -- sealing ---------------------------------------------------------------


def seal(text: str) -> str:
    payload = base64.b64encode(zlib.compress(text.encode("utf8"), 9)).decode("ascii")
    wrapped = "\n".join(payload[index : index + 76] for index in range(0, len(payload), 76))
    return (
        f"# {SEAL_HEADER}\n"
        "# Sealed Grogu plan stage. Read it with:\n"
        "#   grogu plan show <id> --stage <stage> --role tester\n"
        "# Roles that may not read this stage are refused, and every read is\n"
        "# recorded. Decoding it by hand to route around that is a contract\n"
        "# violation, not a clever shortcut.\n"
        f"{wrapped}\n"
    )


def unseal(text: str) -> str:
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    payload = "".join(line.strip() for line in lines)
    if not payload:
        return ""
    try:
        return zlib.decompress(base64.b64decode(payload)).decode("utf8")
    except (ValueError, zlib.error) as error:
        raise PlanError(f"sealed plan stage is unreadable: {error}") from error


# -- triage ----------------------------------------------------------------

_DIRECT_PATTERNS = (
    r"\bno plan\b",
    r"\bwithout a plan\b",
    r"\bdon'?t plan\b",
    r"\bskip the plan\b",
    r"\bjust (answer|tell|show|check|look|read|run|explain)\b",
    r"\bquick question\b",
    r"^(what|why|how|where|who|which|when|is|are|does|do|did|can|should|could|will)\b",
    r"\b(explain|summari[sz]e|describe|show me|list|find|search|read|status|remind)\b",
    r"\bfor (my|your) (information|awareness)\b",
    r"\b(steering|heads up|fyi|note that|keep in mind)\b",
)

_PLAN_PATTERNS = (
    r"\b(build|implement|create|add|introduce|design|architect)\b",
    r"\b(refactor|rewrite|migrate|port|redesign|restructure|overhaul)\b",
    r"\b(feature|capability|subsystem|pipeline|integration|end.to.end)\b",
    r"\bacross\b.*\b(files|modules|packages|services|repos)\b",
    r"\b(and then|after that|followed by|as well as)\b",
    r"\bmake (it|them|this) (work|support|handle)\b",
)

_TRIVIAL_PATTERNS = (
    r"\b(typo|rename|comment|docstring|whitespace|lint|format)\b",
    r"\bone[- ]line(r)?\b",
    r"\bbump (the )?version\b",
)


# The pipeline exists to build software in this repository. Grogu is also a
# general assistant — research, email, messages, errands, thinking out loud —
# and none of that wants an architect. "Plan a trip to Japan" and "help me plan
# my week" both contain the word plan and neither is a planning cycle.
_SOFTWARE_PATTERNS = (
    r"\b(code|codebase|repo|repository|branch|commit|pull request|pr)\b",
    r"\b(function|class|module|file|script|package|library|dependency)\b",
    r"\b(api|endpoint|cli|command|database|schema|migration|query|server)\b",
    r"\b(test|tests|testing|lint|build|compile|deploy|ci|pipeline)\b",
    r"\b(bug|crash|stack trace|exception|regression|refactor|rewrite)\b",
    r"\b(feature|implement|implementation|ship|release|harness|agent)\b",
    r"\b(ui|ux|interface|screen|page|component|frontend|backend)\b",
    r"\b(grogu|python|typescript|javascript|rust|go|swift|sql)\b",
    r"\.(py|ts|tsx|js|jsx|go|rs|swift|sh|md|json|ya?ml)\b",
)


# Listing what software work looks like fails in the wrong direction: "a plan
# for the new indexer" contains no vocabulary from any list and is real work.
# The default context is a code repository, so the sound test is the reverse —
# name the domains that are plainly *not* this repository, and require that no
# software signal is present before believing it.
_PERSONAL_PATTERNS = (
    r"\b(trip|flight|flights|hotel|airbnb|itinerary|vacation|holiday)\b",
    r"\b(restaurant|dinner|lunch|recipe|groceries|cook)\b",
    r"\b(email|e-mail|text|texted|imessage|message[ds]?|reply to|draft (?:a|an) (?:note|email|reply))\b",
    r"\b(landlord|dentist|doctor|appointment|insurance|rent|taxes)\b",
    r"\b(gym|workout|sleep|calendar|errand|birthday|gift)\b",
    r"\b(buy|purchase|price|cheapest|under \$?\d+|headphones|mattress)\b",
    r"\bmy (?:week|day|schedule|budget|finances|wife|kid|dog|cat)\b",
)


def is_software_work(prompt: str) -> bool:
    """Whether this is work on the repository, rather than life admin.

    Biased toward yes: Grogu runs inside a repository, so anything without a
    clear personal-domain signal is treated as software work and left to normal
    triage. Getting this wrong toward "personal" would silently disable the
    pipeline on real work, which is far worse than an occasional needless
    planning cycle.
    """
    lowered = " ".join(prompt.split()).lower()
    if any(re.search(pattern, lowered) for pattern in _SOFTWARE_PATTERNS):
        return True
    return not any(re.search(pattern, lowered) for pattern in _PERSONAL_PATTERNS)


_DIRECT_OVERRIDE = (
    r"\b(?:just|please just) do it\b"
    r"|\bno plan\b|\bwithout a plan\b|\bskip (?:the )?plan(?:ning)?\b"
    r"|\bdon'?t plan\b|\bno need (?:for|to) plan\b"
)
_PLAN_OVERRIDE = (
    r"\b(?:make|write|draft|give me|build|create) (?:me )?(?:a|the) plan\b"
    r"|\bplan (?:this|it) out\b|\bplan first\b|\bi want a plan\b"
)

_DESIGN_PATTERNS = (
    r"\b(ui|ux|interface|screen|page|view|layout|design)\b",
    r"\b(button|form|modal|dialog|menu|navigation|nav bar|sidebar)\b",
    r"\b(dashboard|onboarding|settings page|landing page)\b",
    r"\b(looks?|feel|visual|styling|theme|dark mode)\b",
    r"\bcommand output\b",
    r"\buser[- ]facing\b",
)


def triage(prompt: str) -> dict:
    """Decide whether a request warrants a plan, deterministically.

    Spending a model call to decide whether to spend model calls is exactly the
    waste this is meant to avoid, so this is pattern matching and arithmetic.
    It is deliberately biased: an unnecessary plan costs tokens, but skipping a
    plan on real work costs a rewrite, so ties go to planning.
    """
    text = " ".join(prompt.split())
    lowered = text.lower()
    reasons: list[str] = []
    score = 0
    plan_hits = 0

    def note(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if not is_software_work(lowered):
        return {
            "decision": "direct",
            "design": False,
            "software": False,
            "override": False,
            "score": 0,
            "words": len(lowered.split()),
            "reasons": ["not software work in this repository"],
            "explanation": (
                "Answer this directly. The architect/engineer/tester pipeline is "
                "for building software here; research, correspondence, errands "
                "and thinking out loud are not it, whatever the word 'plan' is "
                "doing in the sentence."
            ),
        }

    # Stated intent is not a signal to be weighed against other signals; it is
    # the answer. Scoring "just do it: build ..." on its verbs reaches the
    # opposite conclusion from the one the user just gave in words.
    override = ""
    if re.search(_DIRECT_OVERRIDE, lowered):
        override = "direct"
    elif re.search(_PLAN_OVERRIDE, lowered):
        override = "plan"
    if override:
        return {
            "decision": override,
            "design": override == "plan" and bool(
                [p for p in _DESIGN_PATTERNS if re.search(p, lowered)]
            ),
            "score": 0,
            "software": True,
            "words": len(lowered.split()),
            "reasons": ["the request states its own routing explicitly"],
            "override": True,
            "explanation": (
                "The user asked for a plan; route through the architect and stop "
                "for their review."
                if override == "plan"
                else "The user asked for this directly; do not spend a planning cycle."
            ),
        }

    for pattern in _DIRECT_PATTERNS:
        if re.search(pattern, lowered):
            score -= 2
            note("direct signal")
    for pattern in _PLAN_PATTERNS:
        if re.search(pattern, lowered):
            score += 2
            plan_hits += 1
            note("planning signal")
    for pattern in _TRIVIAL_PATTERNS:
        if re.search(pattern, lowered):
            score -= 2
            note("trivial-change signal")

    words = len(lowered.split())
    if words > 60:
        score += 2
        reasons.append("long request (>60 words)")
    elif words < 12 and not plan_hits:
        # Brevity only argues against planning when nothing was asked to be
        # built. "Refactor the storage layer" is six words and a week of work.
        score -= 1
        reasons.append("short request with no build verb")

    bullets = len(re.findall(r"(?m)^\s*(?:[-*\u2022]|\d+[.)])\s+", text))
    if bullets >= 2:
        score += 2
        reasons.append(f"{bullets} enumerated requirements")

    if lowered.rstrip().endswith("?") and score < 2:
        score -= 2
        reasons.append("phrased as a question")

    if re.search(r"\b(plan|architect)\b", lowered):
        score += 4
        reasons.append("plan requested explicitly")

    decision = "plan" if score >= 2 else "direct"
    _override = False
    design_hits = [
        pattern for pattern in _DESIGN_PATTERNS if re.search(pattern, lowered)
    ]
    if design_hits and decision == "plan":
        reasons.append("user-visible surface: warrants a design stage")
    return {
        "decision": decision,
        "design": bool(design_hits) and decision == "plan",
        "software": True,
        "override": _override,
        "score": score,
        "words": words,
        "reasons": reasons,
        "explanation": (
            "Route through the architect before editing anything."
            if decision == "plan"
            else "Answer or act directly; a planning cycle would be waste."
        ),
    }


# -- glob overlap ----------------------------------------------------------


def _literal_prefix(pattern: str) -> str:
    index = len(pattern)
    for character in "*?[":
        found = pattern.find(character)
        if found != -1:
            index = min(index, found)
    return pattern[:index]


def patterns_overlap(left: str, right: str, root: Optional[Path] = None) -> bool:
    """Whether two path globs can select the same file.

    Resolved against the working tree when possible, because that is exact.
    Falls back to comparing patterns when neither matches anything on disk,
    which is the normal case for a plan written before the files exist.
    """
    if left == right:
        return True
    if root is not None:
        try:
            left_files = {path for path in root.glob(left) if path.is_file()}
            right_files = {path for path in root.glob(right) if path.is_file()}
        except (OSError, ValueError, IndexError):
            left_files = right_files = set()
        if left_files and right_files:
            return bool(left_files & right_files)
    if fnmatch.fnmatch(left, right) or fnmatch.fnmatch(right, left):
        return True
    left_prefix = _literal_prefix(left)
    right_prefix = _literal_prefix(right)
    if not left_prefix or not right_prefix:
        return True
    return left_prefix.startswith(right_prefix) or right_prefix.startswith(left_prefix)


def pending_banner(root: Optional[Path] = None) -> str:
    """Unread steering for the calling agent, as a block to append to any output.

    A running subagent cannot be interrupted from outside: nothing can push text
    into its context except the output of a tool it already ran. So instead of
    asking the model to remember to poll, every `grogu` command carries the
    delivery. The agent is already running `grogu` constantly — heartbeats,
    gates, status, aggregate — and steering rides along with whatever it ran.

    The role comes from GROGU_ROLE when the spawning environment set it, and
    otherwise from the binding `grogu plan brief` recorded for this working
    directory — because a subagent's first act is to fetch its brief, and no
    mechanism here can set an environment variable inside a session it does not
    own. Nothing is delivered when neither exists, so the user's own session
    does not see its own notes echoed back.
    """
    role = current_role()
    plan_id = os.environ.get("GROGU_PLAN", "").strip()
    try:
        store = PlanStore(root)
        if not role:
            bound = store.session_binding()
            role, plan_id = bound.get("role", ""), plan_id or bound.get("plan", "")
        if not role:
            return ""
        pending = store.steering(role=role, plan_id=plan_id, unread=True)
    except (PlanError, OSError):
        return ""  # steering must never be the reason a command fails
    notes = pending.get("repository", []) + pending.get("plan", [])
    if not notes:
        return ""
    lines = [
        "",
        f"── steering for the {role} ─────────────────────────────",
    ]
    for note in notes[-10:]:
        binding = " [requires replan]" if note.get("requires_replan") else ""
        lines.append(f"  #{note['seq']}{binding} {note['text']}")
    lines.append(
        "  Fold this in now, then run "
        f"`grogu plan steering --role {role}"
        + (f" --plan {plan_id}" if plan_id else "")
        + " --ack`."
    )
    if any(note.get("requires_replan") for note in notes):
        lines.append(
            "  This steering blocks the stage gates until the architect revises the plan."
        )
    lines.append("───────────────────────────────────────────────────────")
    return "\n".join(lines)


# -- design spec structure -------------------------------------------------

# The format question has two bad answers. An HTML mockup is an implementation:
# it makes the designer the front-end engineer, encodes a hundred incidental
# decisions the engineer cannot distinguish from deliberate ones, and does not
# survive a move to a native view or a terminal. Free prose is worse — "clean,
# modern, Apple-like" is not implementable, so the engineer decides, which is
# the exact failure this role exists to prevent.
#
# What transfers is structured English carrying concrete values: fixed sections
# so nothing is silently skipped, real numbers and literal strings instead of
# adjectives, and acceptance criteria the tester can check. Consistent
# completeness is most of what a weaker model gets wrong, and it is the part a
# machine can enforce.
REQUIRED_DESIGN_SECTIONS = (
    "surfaces",
    "hierarchy",
    "states",
    "flow",
    "copy",
    "tokens",
    "accessibility",
    "acceptance criteria",
    "left to the engineer",
)

# Adjectives that feel like decisions and are not. Each one is a place where the
# engineer will have to guess, and where the guess will be wrong.
VAGUE_DESIGN_TERMS = (
    "clean",
    "modern",
    "sleek",
    "elegant",
    "polished",
    "beautiful",
    "intuitive",
    "user-friendly",
    "seamless",
    "nice",
    "pretty",
    "apple-like",
    "apple-esque",
    "premium",
    "slick",
)


def _headings(body: str) -> list:
    return [
        line.lstrip("#").strip().lower()
        for line in body.splitlines()
        if line.lstrip().startswith("#")
    ]


def missing_design_sections(body: str) -> list:
    headings = _headings(body)
    return [
        section
        for section in REQUIRED_DESIGN_SECTIONS
        if not any(section in heading for heading in headings)
    ]


def vague_design_terms(body: str) -> set:
    """Adjectives used as if they were specifications.

    Allowed inside fenced blocks, which hold literal copy and sample output —
    the user's own interface may well use the word "clean".
    """
    outside = []
    fenced = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            outside.append(line)
    text = " ".join(outside).lower()
    return {
        term for term in VAGUE_DESIGN_TERMS if re.search(rf"\b{re.escape(term)}\b", text)
    }


# Choosing a dependency from memory is the quiet architecture failure: a stale
# recommendation reads exactly like a current one. This cannot be enforced —
# nothing here can prove a search happened — but a plan that adopts something
# external and cites nothing is worth saying out loud.
_DEPENDENCY_SIGNALS = (
    r"\b(npm|yarn|pnpm) (install|add)\b",
    r"\bpip install\b",
    r"\bgo get\b",
    r"\bcargo add\b",
    r"\bbrew install\b",
    r"\badd (?:a |the )?(?:new )?dependenc(?:y|ies)\b",
    r"\bnew dependency\b",
    r"\b(?:use|adopt|switch to) the \w+ (?:library|package|sdk|service)\b",
)


def uncited_dependencies(body: str) -> list:
    """Dependency adoptions in a plan that cite no source."""
    lowered = body.lower()
    hits = [
        re.search(pattern, lowered).group(0)
        for pattern in _DEPENDENCY_SIGNALS
        if re.search(pattern, lowered)
    ]
    if not hits or re.search(r"https?://", lowered):
        return []
    return hits


DESIGN_TEMPLATE = """# {title} — design

## Surfaces
Every screen, view, command or endpoint this change touches. One line each.

## Hierarchy
For each surface: the single primary action, what is secondary, what is
destructive and where it is kept away from the primary.

## States
Default, empty, loading, error, success — for each surface. Give the literal
copy for empty and error states; a state without written copy is a state the
engineer will invent.

## Flow
The path through the surfaces, including what happens on cancel and on failure.

## Copy
Exact strings. Labels, buttons, headings, errors, confirmations. Voice: what
this product sounds like, with one rewritten example.

## Tokens
Concrete values, not adjectives. Spacing scale, type ramp with sizes and
weights, the accent colour and what it means, corner radii, motion durations.
State the scale once and reference it; do not restate padding per element.

## Accessibility
Contrast ratios, the full keyboard path, focus order, reduced-motion and
dynamic-type behavior, labels for anything non-textual.

## Layout
For a visual surface, a rough ASCII box sketch — arrangement and proportion
only, deliberately low fidelity so nobody treats it as source.

For a terminal or API surface, a fenced block of the exact intended output,
alignment included. That is the highest-fidelity artifact available here and it
is directly testable:

```
$ example command
  id            status     title
  p-20260814-a  approved   Rate limiting
```

## Acceptance criteria
Checkable statements, one per line, that the tester can confirm or deny
without asking anyone what was meant.

## Left to the engineer
What is deliberately not specified, and therefore the engineer's call. Naming
this is what stops a spec from being read as either gospel or a suggestion.
"""


def design_template(title: str = "<change>") -> str:
    return DESIGN_TEMPLATE.format(title=title)


class PlanStore:
    """Plan artifacts, the stage gates, and the review loop for one repository."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root or repository_root()).expanduser().resolve()
        self.store = self.root / STORE_DIRNAME
        self.plans_dir = self.store / PLANS_DIRNAME
        self.state_dir = self.store / "state"
        self.steering_path = self.plans_dir / "steering.json"
        self.friction_path = self.plans_dir / "friction.json"

    # -- storage -----------------------------------------------------------

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        marker = self.state_dir / ".gitignore"
        if not marker.exists():
            marker.write_text("*\n", encoding="utf8")
        handle = os.open(self.state_dir / "plans.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            os.close(handle)

    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf8")
        os.replace(temporary, path)

    def _read_json(self, path: Path) -> dict:
        try:
            value = json.loads(path.read_text(encoding="utf8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def plan_dir(self, plan_id: str) -> Path:
        return self.plans_dir / plan_id

    def manifest_path(self, plan_id: str) -> Path:
        return self.plan_dir(plan_id) / "manifest.json"

    # -- identifiers -------------------------------------------------------

    def _new_id(self) -> str:
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
        while True:
            candidate = f"p-{stamp}-{secrets.token_hex(3)}"
            if not self.plan_dir(candidate).exists():
                return candidate

    def resolve(self, reference: str) -> str:
        if self.manifest_path(reference).exists():
            return reference
        matches = [
            plan["id"] for plan in self.list_plans() if plan["id"].startswith(reference)
        ]
        if not matches:
            by_task = [
                plan["id"]
                for plan in self.list_plans()
                if plan.get("task_id") == reference
                and plan.get("status") != SUPERSEDED
            ]
            matches = by_task
        if not matches:
            raise PlanError(f"no plan matches {reference!r}")
        if len(matches) > 1:
            raise PlanError(
                f"{reference!r} matches {len(matches)} plans: {', '.join(sorted(matches))}"
            )
        return matches[0]

    # -- reads -------------------------------------------------------------

    def load(self, plan_id: str) -> dict:
        manifest = self._read_json(self.manifest_path(plan_id))
        if not manifest:
            raise PlanError(f"plan {plan_id} was not found")
        return manifest

    def list_plans(self) -> list:
        if not self.plans_dir.is_dir():
            return []
        plans = [
            self._read_json(path / "manifest.json")
            for path in sorted(self.plans_dir.iterdir())
            if path.is_dir()
        ]
        return [plan for plan in plans if plan.get("id")]

    # -- creation ----------------------------------------------------------

    def create(
        self,
        title: str,
        *,
        task_id: str = "",
        design: bool = False,
        evaluation: bool = False,
        review_required: bool = False,
        requested_by: str = "",
    ) -> dict:
        with self.locked():
            plan_id = self._new_id()
            stages = (
                ([DESIGN] if design else [])
                + [IMPLEMENTATION, TESTING]
                + ([EVALUATION] if evaluation else [])
            )
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "id": plan_id,
                "task_id": task_id,
                "title": title,
                "status": DRAFT,
                "stages": stages,
                "stage_state": {stage: PENDING for stage in stages},
                "stage_written": {stage: False for stage in stages},
                # A plan the user asked for is a plan the user reviews, and no
                # autopilot heuristic gets to decide otherwise.
                "review_required": bool(review_required),
                "requested_by": requested_by or actor(),
                "created_at": now(),
                "updated_at": now(),
                "created_by": actor(),
                "session_id": session_id(),
                "rounds": 0,
                "max_rounds": max_rounds(),
                "defect_rounds": 0,
                "max_defect_rounds": max_defect_rounds(),
                "escalated": False,
                "workstreams": [],
                "amendments": [],
                "defects": [],
                "steering": [],
                "steering_acked": {role: 0 for role in ROLES},
                "access_log": [],
                "events": [{"at": now(), "actor": actor(), "event": "created"}],
            }
            self._write_json(self.manifest_path(plan_id), manifest)
            for stage in stages:
                path = self.stage_path(plan_id, stage)
                if not path.exists():
                    body = f"# {title} — {stage} plan\n\n_Not written yet._\n"
                    path.write_text(
                        seal(body) if stage in SEALED_STAGES else body, encoding="utf8"
                    )
            return manifest

    def _save(self, manifest: dict, event: str, **details: object) -> dict:
        manifest["updated_at"] = now()
        entry = {"at": now(), "actor": actor(), "event": event}
        entry.update({key: value for key, value in details.items() if value not in ("", None)})
        manifest.setdefault("events", []).append(entry)
        self._write_json(self.manifest_path(manifest["id"]), manifest)
        return manifest

    # -- stage bodies ------------------------------------------------------

    def write_stage(self, plan_id: str, stage: str, body: str, *, role: str = "") -> dict:
        """Stages are written by the role that owns them; others propose."""
        role = role or current_role() or ARCHITECT
        writers = STAGE_WRITERS.get(stage, frozenset({ARCHITECT}))
        if role not in writers:
            raise PlanError(
                f"role {role!r} may not write the {stage} stage "
                f"(owners: {', '.join(sorted(writers))}); propose a change with "
                "`grogu plan amend` and let the owner decide"
            )
        with self.locked():
            manifest = self.load(plan_id)
            if stage not in manifest.get("stages", []):
                raise PlanError(
                    f"plan {plan_id} has no {stage} stage; create it with "
                    "`grogu plan new --design/--eval` when one is warranted"
                )
            if not body.strip():
                raise PlanError("refusing to write an empty plan stage")
            if stage == DESIGN:
                missing = missing_design_sections(body)
                if missing:
                    raise PlanError(
                        "the design spec is missing required sections: "
                        + ", ".join(missing)
                        + ". Run `grogu design template` for the skeleton. A spec "
                        "that skips states, tokens or acceptance criteria hands "
                        "those decisions to the engineer by omission."
                    )
                vague = vague_design_terms(body)
                if vague:
                    raise PlanError(
                        "the design spec leans on adjectives instead of decisions: "
                        + ", ".join(sorted(vague))
                        + ". Replace each with a concrete value — a spacing number, "
                        "a type size, the literal copy string, the exact output."
                    )
            path = self.stage_path(plan_id, stage)
            sealed = path.suffix == ".sealed"
            path.write_text(seal(body) if sealed else body, encoding="utf8")
            manifest.setdefault("stage_written", {})[stage] = True
            for amendment in manifest.get("amendments", []):
                if (
                    amendment.get("status") == ACCEPTED
                    and amendment.get("stage") == stage
                    and not amendment.get("incorporated")
                ):
                    amendment["incorporated"] = True
                    amendment["incorporated_at"] = now()
            manifest.setdefault("design_review", None)
            if stage == DESIGN and manifest.get("design_review"):
                manifest["design_review"] = None
            manifest = self._save(manifest, "stage_written", stage=stage, bytes=len(body))
            manifest["warnings"] = (
                [
                    "this plan adopts something external ("
                    + ", ".join(uncited_dependencies(body))
                    + ") and cites no source. Check the current state of it — "
                    "version, maintenance, licence, cost — and record where you "
                    "checked, so the engineer can tell a researched choice from a "
                    "remembered one."
                ]
                if stage == IMPLEMENTATION and uncited_dependencies(body)
                else []
            )
            return manifest

    def read_stage(self, plan_id: str, stage: str, *, role: str, record: bool = True) -> str:
        if role not in ROLES:
            raise PlanError(f"unknown role {role!r}; expected one of {', '.join(ROLES)}")
        allowed = stage in ROLE_READABLE_STAGES[role]
        if record:
            self._record_access(plan_id, role, stage, allowed)
        if not allowed:
            readable = ", ".join(sorted(ROLE_READABLE_STAGES[role])) or "nothing"
            raise PlanError(
                f"role {role!r} may not read the {stage} plan (may read: {readable}). "
                "The implementation is written to the plan, not to the tests; "
                "an implementation shaped by its own tests proves nothing."
            )
        path = self.stage_path(plan_id, stage)
        if not path.exists():
            raise PlanError(f"plan {plan_id} has no {stage} stage")
        text = path.read_text(encoding="utf8")
        return unseal(text) if path.suffix == ".sealed" else text

    def _record_access(self, plan_id: str, role: str, stage: str, allowed: bool) -> None:
        with self.locked():
            manifest = self.load(plan_id)
            log = manifest.setdefault("access_log", [])
            log.append(
                {
                    "at": now(),
                    "actor": actor(),
                    "role": role,
                    "stage": stage,
                    "allowed": allowed,
                }
            )
            # The log is evidence, not an archive; keep the recent tail.
            del log[:-200]
            self._write_json(self.manifest_path(plan_id), manifest)

    # -- lifecycle ---------------------------------------------------------

    def approve(self, plan_id: str, *, note: str = "") -> dict:
        """Approval is the user's, and a subagent cannot stand in for them.

        Every spawned role declares itself through GROGU_ROLE; the user's own
        session does not. That is a weak signal — an agent could unset it — but
        it turns "autopilot does not waive review" from a sentence in a prompt
        into something that has to be deliberately circumvented rather than
        merely forgotten.
        """
        if current_role():
            raise PlanError(
                f"the {current_role()} may not approve a plan on the user's "
                "behalf; approval is the user's alone. Present the plan and stop"
            )
        with self.locked():
            manifest = self.load(plan_id)
            missing = [
                stage
                for stage in manifest.get("stages", [])
                if not manifest.get("stage_written", {}).get(stage)
            ]
            if missing:
                raise PlanError(
                    f"plan {plan_id} is missing bodies for: {', '.join(missing)}"
                )
            pending = [
                amendment
                for amendment in manifest.get("amendments", [])
                if amendment.get("status") == PENDING
            ]
            if pending:
                raise PlanError(
                    f"plan {plan_id} has {len(pending)} unresolved amendment(s); "
                    "resolve them before approving"
                )
            manifest["status"] = APPROVED
            manifest["approved_at"] = now()
            manifest["approved_by"] = actor()
            return self._save(manifest, "approved", note=note)

    def set_status(self, plan_id: str, status: str, *, note: str = "") -> dict:
        if status not in PLAN_STATUSES:
            raise PlanError(f"unknown plan status {status!r}")
        with self.locked():
            manifest = self.load(plan_id)
            manifest["status"] = status
            return self._save(manifest, "status", status=status, note=note)

    def set_stage_state(self, plan_id: str, stage: str, state: str, *, note: str = "") -> dict:
        if state not in STAGE_STATES:
            raise PlanError(f"unknown stage state {state!r}")
        with self.locked():
            manifest = self.load(plan_id)
            if stage not in manifest.get("stages", []):
                raise PlanError(f"plan {plan_id} has no {stage} stage")
            manifest.setdefault("stage_state", {})[stage] = state
            return self._save(manifest, "stage_state", stage=stage, state=state, note=note)

    # -- steering ----------------------------------------------------------

    def _repo_steering(self) -> dict:
        payload = self._read_json(self.steering_path)
        payload.setdefault("schema_version", SCHEMA_VERSION)
        payload.setdefault("notes", [])
        payload.setdefault("acked", {role: 0 for role in ROLES})
        return payload

    def steer(
        self,
        text: str,
        *,
        plan_id: str = "",
        role: str = "all",
        requires_replan: bool = False,
    ) -> dict:
        """Record steering for whoever picks the work up next.

        Live relays reach only the subagents that happen to be running. A note
        recorded here also reaches the ones spawned an hour from now, which is
        the difference between steering the session and steering the work.
        """
        if role != "all" and role not in ROLES:
            raise PlanError(f"unknown role {role!r}; expected 'all' or one of {', '.join(ROLES)}")
        if not text.strip():
            raise PlanError("refusing to record empty steering")
        with self.locked():
            if plan_id:
                manifest = self.load(plan_id)
                notes = manifest.setdefault("steering", [])
                note = {
                    "seq": len(notes) + 1,
                    "at": now(),
                    "actor": actor(),
                    "role": role,
                    "text": text.strip(),
                    "requires_replan": bool(requires_replan),
                }
                notes.append(note)
                if requires_replan:
                    # Steering that invalidates the plan must stop the pipeline,
                    # not race it.
                    manifest["status"] = NEEDS_REVIEW
                self._save(
                    manifest,
                    "steering",
                    role=role,
                    seq=note["seq"],
                    requires_replan=requires_replan or None,
                )
                return note
            payload = self._repo_steering()
            note = {
                "seq": len(payload["notes"]) + 1,
                "at": now(),
                "actor": actor(),
                "role": role,
                "text": text.strip(),
                "requires_replan": bool(requires_replan),
            }
            payload["notes"].append(note)
            self._write_json(self.steering_path, payload)
            return note

    def steering(self, *, role: str = "all", plan_id: str = "", unread: bool = False) -> dict:
        """Steering visible to `role`, newest last, cheap enough to poll."""
        if role != "all" and role not in ROLES:
            raise PlanError(f"unknown role {role!r}")

        def visible(notes: list, acked: int) -> list:
            selected = [
                note
                for note in notes
                if role == "all" or note.get("role") in ("all", role)
            ]
            if unread:
                selected = [note for note in selected if note.get("seq", 0) > acked]
            return selected

        payload = self._repo_steering()
        repo_acked = payload["acked"].get(role, 0) if role != "all" else 0
        result = {
            "role": role,
            "repository": visible(payload["notes"], repo_acked),
            "plan": [],
            "plan_id": plan_id,
        }
        if plan_id:
            manifest = self.load(plan_id)
            plan_acked = (
                manifest.get("steering_acked", {}).get(role, 0) if role != "all" else 0
            )
            result["plan"] = visible(manifest.get("steering", []), plan_acked)
            result["requires_replan"] = any(
                note.get("requires_replan") for note in result["plan"]
            )
        return result

    def ack_steering(self, *, role: str, plan_id: str = "") -> dict:
        if role not in ROLES:
            raise PlanError(f"unknown role {role!r}")
        with self.locked():
            payload = self._repo_steering()
            repo_high = max(
                [note.get("seq", 0) for note in payload["notes"]] or [0]
            )
            payload["acked"][role] = repo_high
            self._write_json(self.steering_path, payload)
            plan_high = 0
            if plan_id:
                manifest = self.load(plan_id)
                plan_high = max(
                    [note.get("seq", 0) for note in manifest.get("steering", [])] or [0]
                )
                manifest.setdefault("steering_acked", {})[role] = plan_high
                self._save(manifest, "steering_acked", role=role, seq=plan_high)
            return {"role": role, "repository_seq": repo_high, "plan_seq": plan_high}

    # -- amendments (engineer/tester -> architect) --------------------------

    def amend(
        self,
        plan_id: str,
        *,
        claim: str,
        evidence: str = "",
        stage: str = IMPLEMENTATION,
        raised_by: str = "",
        kind: str = KIND_AMENDMENT,
    ) -> dict:
        raised_by = raised_by or current_role() or ENGINEER
        if not claim.strip():
            raise PlanError("an amendment needs a claim")
        with self.locked():
            manifest = self.load(plan_id)
            rounds = manifest.get("rounds", 0)
            cap = manifest.get("max_rounds", DEFAULT_MAX_ROUNDS)
            if kind == KIND_AMENDMENT:
                rounds += 1
                if rounds > cap:
                    # The architect owns the plan, so there is no higher agent to
                    # appeal to. Disagreement that survives this many rounds is a
                    # question about intent, which is the user's to answer.
                    raise PlanError(
                        f"plan {plan_id} has used its {cap} amendment round(s) with "
                        "the architect. Stop and put both positions to the user: "
                        "another round is a budget leak, not progress."
                    )
                manifest["rounds"] = rounds
            amendments = manifest.setdefault("amendments", [])
            amendment = {
                "id": f"a{len(amendments) + 1}",
                "at": now(),
                "kind": kind,
                "raised_by": raised_by,
                "actor": actor(),
                "stage": stage,
                "claim": claim.strip(),
                "evidence": evidence.strip(),
                "status": PENDING,
            }
            amendments.append(amendment)
            manifest["status"] = AMENDING
            if kind == KIND_ESCALATION:
                manifest["escalated"] = True
            self._save(
                manifest,
                "amendment_raised",
                amendment=amendment["id"],
                kind=kind,
                round=rounds if kind == KIND_AMENDMENT else None,
            )
            return amendment

    def resolve_amendment(
        self,
        plan_id: str,
        amendment_id: str,
        *,
        outcome: str,
        reason: str,
        verified: bool,
        role: str = "",
    ) -> dict:
        role = role or current_role() or ARCHITECT
        if role != ARCHITECT:
            raise PlanError(
                f"role {role!r} may not resolve amendments; only the architect owns the plan"
            )
        if outcome not in AMENDMENT_OUTCOMES:
            raise PlanError(
                f"unknown outcome {outcome!r}; expected one of {', '.join(AMENDMENT_OUTCOMES)}"
            )
        if not verified:
            raise PlanError(
                "pass --verified: the architect must confirm the claim against the "
                "code itself. Taking another agent's word for it is how a wrong "
                "plan becomes an agreed plan."
            )
        if not reason.strip():
            raise PlanError("an amendment resolution needs a reason")
        with self.locked():
            manifest = self.load(plan_id)
            for amendment in manifest.get("amendments", []):
                if amendment.get("id") == amendment_id:
                    break
            else:
                raise PlanError(f"plan {plan_id} has no amendment {amendment_id!r}")
            if amendment.get("status") != PENDING:
                raise PlanError(
                    f"amendment {amendment_id} is already {amendment['status']}"
                )
            amendment["status"] = outcome
            amendment["resolved_at"] = now()
            amendment["resolved_by"] = actor()
            amendment["reason"] = reason.strip()
            amendment["verified"] = True
            if outcome == ACCEPTED:
                # An accepted amendment says the plan is wrong. Reopening the
                # gate before the stage is rewritten sends the engineer back at
                # the same known-wrong text with official approval.
                amendment["incorporated"] = False
            still_open = any(
                other.get("status") == PENDING for other in manifest["amendments"]
            )
            if not still_open:
                if manifest.get("status") == AMENDING:
                    manifest["status"] = APPROVED if manifest.get("approved_at") else DRAFT
                if amendment.get("kind") == KIND_ESCALATION:
                    # The deadlock is broken, so the loop budget resets and the
                    # engineer and tester may try again against new guidance.
                    manifest["defect_rounds"] = 0
                    manifest["escalated"] = False
            self._save(
                manifest,
                "amendment_resolved",
                amendment=amendment_id,
                outcome=outcome,
            )
        if outcome == GUIDED:
            # Guidance reaches the agents the same way the user's steering does,
            # so there is one delivery channel and one place to look.
            self.steer(
                f"Architect guidance on {amendment_id}: {reason.strip()}",
                plan_id=plan_id,
                role=ENGINEER,
            )
            self.steer(
                f"Architect guidance on {amendment_id}: {reason.strip()}",
                plan_id=plan_id,
                role=TESTER,
            )
        return amendment

    # -- defects (tester -> engineer) --------------------------------------

    def report_defect(
        self,
        plan_id: str,
        *,
        report: str,
        route: str,
        evidence: str = "",
        raised_by: str = "",
    ) -> dict:
        """Record a failure and, above all, where it belongs.

        Most testing loops fail not because a test failed but because the
        failure was handed to the wrong agent: the engineer patches a broken
        test, or the tester works around a plan that was wrong.
        """
        if route not in DEFECT_ROUTES:
            raise PlanError(
                f"unknown route {route!r}; expected one of {', '.join(DEFECT_ROUTES)}"
            )
        if not report.strip():
            raise PlanError("a defect needs a report")
        raised_by = raised_by or current_role() or TESTER
        escalate = False
        with self.locked():
            manifest = self.load(plan_id)
            defects = manifest.setdefault("defects", [])
            defect = {
                "id": f"d{len(defects) + 1}",
                "at": now(),
                "raised_by": raised_by,
                "actor": actor(),
                "route": route,
                "report": report.strip(),
                "evidence": evidence.strip(),
                "status": PENDING,
                "owner": {
                    ROUTE_IMPLEMENTATION: ENGINEER,
                    ROUTE_TEST: TESTER,
                    ROUTE_PLAN: ARCHITECT,
                    ROUTE_DESIGN: DESIGNER,
                }[route],
            }
            defects.append(defect)
            if route in (ROUTE_IMPLEMENTATION, ROUTE_TEST, ROUTE_DESIGN):
                rounds = manifest.get("defect_rounds", 0) + 1
                manifest["defect_rounds"] = rounds
                cap = manifest.get("max_defect_rounds", DEFAULT_MAX_DEFECT_ROUNDS)
                # An engineer and a tester trading fixes past this point are no
                # longer converging. That is a question about the plan, and the
                # architect owns the plan — so it goes up one level, not out to
                # the user.
                escalate = rounds >= cap and not manifest.get("escalated")
                defect["round"] = rounds
            self._save(manifest, "defect_raised", defect=defect["id"], route=route)

        if route == ROUTE_PLAN:
            # A plan defect is an amendment; the architect must adjudicate it.
            amendment = self.amend(
                plan_id,
                claim=report,
                evidence=evidence,
                stage=IMPLEMENTATION,
                raised_by=raised_by,
            )
            defect["amendment"] = amendment["id"]
        elif escalate:
            amendment = self.amend(
                plan_id,
                claim=(
                    f"Engineer and tester have exchanged {defect['round']} rounds "
                    f"without converging. Latest: {report.strip()}"
                ),
                evidence=evidence,
                stage=IMPLEMENTATION,
                raised_by=raised_by,
                kind=KIND_ESCALATION,
            )
            defect["amendment"] = amendment["id"]
            defect["escalated"] = True

        if defect.get("amendment"):
            with self.locked():
                manifest = self.load(plan_id)
                for stored in manifest.get("defects", []):
                    if stored.get("id") == defect["id"]:
                        stored["amendment"] = defect["amendment"]
                        stored["escalated"] = defect.get("escalated", False)
                self._write_json(self.manifest_path(plan_id), manifest)
        return defect

    def resolve_defect(self, plan_id: str, defect_id: str, *, note: str) -> dict:
        if not note.strip():
            raise PlanError("a defect resolution needs a note")
        with self.locked():
            manifest = self.load(plan_id)
            for defect in manifest.get("defects", []):
                if defect.get("id") == defect_id:
                    break
            else:
                raise PlanError(f"plan {plan_id} has no defect {defect_id!r}")
            if defect.get("status") != PENDING:
                raise PlanError(f"defect {defect_id} is already {defect['status']}")
            defect["status"] = RESOLVED
            defect["resolved_at"] = now()
            defect["resolved_by"] = actor()
            defect["resolution"] = note.strip()
            self._save(manifest, "defect_resolved", defect=defect_id)
            return defect

    # -- workstreams -------------------------------------------------------

    def add_workstream(
        self,
        plan_id: str,
        *,
        name: str,
        paths: list,
        depends_on: Optional[list] = None,
        model: str = "",
        review: str = "",
        brief: str = "",
    ) -> dict:
        """Declare a piece of parallel work, and who should do it how.

        The architect knows things the engineer cannot infer from a file list:
        that one workstream is fiddly enough to want a stronger model, that
        another is subtle enough to want a second pair of eyes before the tester
        sees it. Left in prose, that intent is advisory and gets skipped under
        time pressure. Declared here, it is part of the assignment the engineer
        reads and part of what the gate checks.
        """
        if not name.strip():
            raise PlanError("a workstream needs a name")
        if not paths:
            raise PlanError(
                "a workstream needs the path globs it owns; parallel work is only "
                "safe when the file sets are declared and disjoint"
            )
        with self.locked():
            manifest = self.load(plan_id)
            workstreams = manifest.setdefault("workstreams", [])
            if any(stream["name"] == name for stream in workstreams):
                raise PlanError(f"plan {plan_id} already has a workstream {name!r}")
            unknown = [
                dependency
                for dependency in (depends_on or [])
                if not any(stream["name"] == dependency for stream in workstreams)
            ]
            if unknown:
                raise PlanError(f"unknown workstream dependency: {', '.join(unknown)}")
            if review and review not in REVIEW_KINDS:
                raise PlanError(
                    f"unknown review {review!r}; expected one of "
                    + ", ".join(REVIEW_KINDS)
                )
            workstream = {
                "name": name,
                "paths": list(paths),
                "depends_on": list(depends_on or []),
                "model": model.strip(),
                "review": review,
                "brief": brief.strip(),
                "reviews": [],
            }
            workstreams.append(workstream)
            self._save(manifest, "workstream_added", workstream=name)
            return workstream

    def record_review(
        self,
        plan_id: str,
        workstream: str,
        *,
        verdict: str,
        model: str = "",
        findings: str = "",
        kind: str = "",
    ) -> dict:
        """A review the architect asked for, and what it found."""
        if verdict not in DESIGN_VERDICTS:
            raise PlanError(
                f"unknown verdict {verdict!r}; expected {' or '.join(DESIGN_VERDICTS)}"
            )
        with self.locked():
            manifest = self.load(plan_id)
            for stream in manifest.get("workstreams", []):
                if stream["name"] == workstream:
                    break
            else:
                raise PlanError(f"plan {plan_id} has no workstream {workstream!r}")
            if verdict == PASS and not findings.strip():
                raise PlanError(
                    "a passing review needs to say what was actually examined; "
                    "a bare pass is indistinguishable from a review that did not "
                    "happen"
                )
            record = {
                "at": now(),
                "kind": kind or stream.get("review") or REVIEW_RUBBER_DUCK,
                "verdict": verdict,
                "model": model.strip(),
                "findings": findings.strip(),
            }
            stream.setdefault("reviews", []).append(record)
            self._save(manifest, "workstream_reviewed", workstream=workstream)
            return record

    def workstream_conflicts(self, plan_id: str) -> list:
        """Overlapping file sets between workstreams that could run together."""
        manifest = self.load(plan_id)
        workstreams = manifest.get("workstreams", [])
        by_name = {stream["name"]: stream for stream in workstreams}

        def ordered(first: str, second: str, seen: Optional[set] = None) -> bool:
            seen = seen or set()
            if first in seen:
                return False
            seen.add(first)
            dependencies = by_name.get(first, {}).get("depends_on", [])
            if second in dependencies:
                return True
            return any(ordered(name, second, seen) for name in dependencies)

        conflicts = []
        for index, left in enumerate(workstreams):
            for right in workstreams[index + 1 :]:
                if ordered(left["name"], right["name"]) or ordered(
                    right["name"], left["name"]
                ):
                    continue  # sequenced by a dependency, so never concurrent
                for left_path in left["paths"]:
                    for right_path in right["paths"]:
                        if patterns_overlap(left_path, right_path, self.root):
                            conflicts.append(
                                {
                                    "workstreams": [left["name"], right["name"]],
                                    "paths": [left_path, right_path],
                                }
                            )
        return conflicts

    def parallel_batches(self, plan_id: str) -> list:
        """Workstreams grouped into waves that may run at the same time."""
        manifest = self.load(plan_id)
        remaining = {
            stream["name"]: set(stream.get("depends_on", []))
            for stream in manifest.get("workstreams", [])
        }
        batches = []
        while remaining:
            ready = sorted(
                name for name, dependencies in remaining.items() if not dependencies
            )
            if not ready:
                raise PlanError(
                    f"plan {plan_id} has a dependency cycle between workstreams: "
                    + ", ".join(sorted(remaining))
                )
            batches.append(ready)
            for name in ready:
                remaining.pop(name)
            for dependencies in remaining.values():
                dependencies.difference_update(ready)
        return batches

    # -- gates -------------------------------------------------------------

    def design_review(
        self,
        plan_id: str,
        verdict: str,
        *,
        notes: str = "",
        evidence: Optional[list] = None,
        role: str = "",
    ) -> dict:
        """The designer's verdict on the built interface, not the spec.

        A spec survives contact with an implementation about as well as any
        other plan does. The only way to know whether the result is right is to
        look at it running, which is why this requires evidence — screenshots,
        a recording, captured terminal output — rather than an assurance.
        """
        role = role or current_role() or DESIGNER
        if role != DESIGNER:
            raise PlanError(
                f"role {role!r} may not sign off on the design; spawn the "
                "designer to look at the built interface"
            )
        if verdict not in DESIGN_VERDICTS:
            raise PlanError(
                f"unknown verdict {verdict!r}; expected {' or '.join(DESIGN_VERDICTS)}"
            )
        evidence = [item for item in (evidence or []) if item.strip()]
        if verdict == PASS and not evidence:
            raise PlanError(
                "a design pass needs evidence of the built interface — screenshot "
                "paths, a recording, or captured output. Signing off from the "
                "diff alone checks that the code looks right, not that the "
                "interface does"
            )
        with self.locked():
            manifest = self.load(plan_id)
            if DESIGN not in manifest.get("stages", []):
                raise PlanError(f"plan {plan_id} has no design stage to review")
            if verdict == PASS and manifest.get("stage_state", {}).get(
                IMPLEMENTATION
            ) != COMPLETE:
                raise PlanError(
                    "the implementation is not marked complete, so there is "
                    "nothing settled to sign off on yet"
                )
            history = manifest.setdefault("design_reviews", [])
            review = {
                "at": now(),
                "verdict": verdict,
                "notes": notes.strip(),
                "evidence": evidence,
                "commit": head_commit(self.root),
            }
            history.append(review)
            manifest["design_review"] = review
            return self._save(manifest, "design_reviewed", verdict=verdict)

    def gate(self, plan_id: str, stage_gate: str) -> dict:
        """Whether the pipeline may enter a stage, and why not when it may not.

        A gate is a state check rather than an instruction, because an
        instruction is something an autopilot run can talk itself out of.
        """
        if stage_gate not in GATES:
            raise PlanError(f"unknown gate {stage_gate!r}")
        manifest = self.load(plan_id)
        blockers: list[str] = []
        status = manifest.get("status")

        if status == SUPERSEDED:
            blockers.append("plan is superseded")
        if status == AMENDING:
            blockers.append("plan has an unresolved amendment awaiting the architect")
        if manifest.get("escalated"):
            blockers.append(
                "the engineer/tester loop was escalated to the architect for "
                "guidance; wait for the architect to resolve it"
            )
        if status == NEEDS_REVIEW:
            blockers.append("steering requires the architect to revise the plan")
        if manifest.get("review_required") and not manifest.get("approved_at"):
            blockers.append(
                "the user asked for this plan directly and has not approved it "
                "(`grogu plan approve`); autopilot does not waive review"
            )
        missing = [
            stage
            for stage in manifest.get("stages", [])
            if not manifest.get("stage_written", {}).get(stage)
        ]
        if missing:
            blockers.append(f"plan stages not written: {', '.join(missing)}")

        unincorporated = [
            amendment["id"]
            for amendment in manifest.get("amendments", [])
            if amendment.get("status") == ACCEPTED
            and not amendment.get("incorporated")
        ]
        if unincorporated:
            blockers.append(
                f"amendment(s) {', '.join(unincorporated)} were accepted but the "
                "affected stage has not been rewritten; the plan still says the "
                "thing the architect agreed was wrong"
            )

        open_defects = [
            defect
            for defect in manifest.get("defects", [])
            if defect.get("status") == PENDING
        ]
        blocking_defects = [
            defect
            for defect in open_defects
            if defect.get("route") in (ROUTE_IMPLEMENTATION, ROUTE_DESIGN)
        ]
        if stage_gate in (GATE_TEST, GATE_EVALUATE) and blocking_defects:
            blockers.append(
                f"{len(blocking_defects)} defect(s) are still open "
                f"({', '.join(defect['id'] for defect in blocking_defects)}); "
                "retesting around a known failure buries it"
            )
        if stage_gate == GATE_TEST:
            unreviewed = [
                stream["name"]
                for stream in manifest.get("workstreams", [])
                if stream.get("review")
                and not any(
                    review.get("verdict") == PASS
                    for review in stream.get("reviews", [])
                )
            ]
            if unreviewed:
                blockers.append(
                    "the architect asked for a review of: "
                    + ", ".join(unreviewed)
                    + "; run it and record `grogu plan review`"
                )
            if manifest.get("stage_state", {}).get(IMPLEMENTATION) != COMPLETE:
                blockers.append("implementation stage is not complete")
            if DESIGN in manifest.get("stages", []):
                review = manifest.get("design_review") or {}
                if review.get("verdict") != PASS:
                    detail = (
                        f" Last verdict: {review['notes']}"
                        if review.get("notes")
                        else ""
                    )
                    blockers.append(
                        "the designer has not signed off on the built interface; "
                        "have the designer look at it running (screenshots or the "
                        "live surface) and record `grogu plan design-review`."
                        + detail
                    )
                else:
                    # A sign-off is about a specific build. Code moved since means
                    # nobody has looked at what is actually about to be tested.
                    current = head_commit(self.root)
                    if current and review.get("commit") and current != review["commit"]:
                        blockers.append(
                            "the code has changed since the designer signed off "
                            f"({review['commit'][:8]} -> {current[:8]}); the review "
                            "was of a different build"
                        )
        if stage_gate == GATE_EVALUATE:
            if EVALUATION not in manifest.get("stages", []):
                blockers.append("this plan has no evaluation stage")
            if manifest.get("stage_state", {}).get(TESTING) != COMPLETE:
                blockers.append("testing stage is not complete")
        if stage_gate == GATE_IMPLEMENT:
            routed = [
                defect for defect in open_defects if defect.get("route") == ROUTE_PLAN
            ]
            if routed:
                blockers.append(
                    f"{len(routed)} defect(s) routed to the plan are unresolved"
                )

        # Repository-wide binding steering outlives any one plan, so a plan
        # created before it was recorded must still answer for it.
        unread = self.steering(role="all", plan_id=plan_id, unread=False)
        binding_repo = [
            note
            for note in unread.get("repository", [])
            if note.get("requires_replan")
            and note.get("at", "") > manifest.get("planned_at", manifest.get("at", ""))
        ]
        if binding_repo:
            blockers.append(
                f"{len(binding_repo)} binding repository steering note(s) postdate "
                "this plan; the architect must fold them in and re-approve"
            )
        if unread.get("requires_replan") and status != APPROVED:
            blockers.append("binding steering has not been folded into the plan")

        return {
            "plan": plan_id,
            "gate": stage_gate,
            "status": status,
            "allowed": not blockers,
            "blockers": blockers,
            "open_defects": [defect["id"] for defect in open_defects],
        }

    # -- summaries and briefs ----------------------------------------------

    def summary(self, plan_id: str) -> dict:
        """Everything an agent needs to orient, and no plan prose at all."""
        manifest = self.load(plan_id)
        return {
            "id": manifest["id"],
            "title": manifest.get("title", ""),
            "task_id": manifest.get("task_id", ""),
            "status": manifest.get("status"),
            "review_required": manifest.get("review_required", False),
            "stages": manifest.get("stages", []),
            "stage_state": manifest.get("stage_state", {}),
            "rounds": f"{manifest.get('rounds', 0)}/{manifest.get('max_rounds', DEFAULT_MAX_ROUNDS)}",
            "defect_rounds": f"{manifest.get('defect_rounds', 0)}/{manifest.get('max_defect_rounds', DEFAULT_MAX_DEFECT_ROUNDS)}",
            "escalated": manifest.get("escalated", False),
            "workstreams": [
                {
                    "name": stream["name"],
                    "paths": stream["paths"],
                    "depends_on": stream.get("depends_on", []),
                }
                for stream in manifest.get("workstreams", [])
            ],
            "open_amendments": [
                {
                    "id": item["id"],
                    "kind": item.get("kind", KIND_AMENDMENT),
                    "raised_by": item.get("raised_by"),
                    "claim": item["claim"],
                }
                for item in manifest.get("amendments", [])
                if item.get("status") == PENDING
            ],
            "open_defects": [
                {"id": item["id"], "route": item["route"], "owner": item.get("owner"), "report": item["report"]}
                for item in manifest.get("defects", [])
                if item.get("status") == PENDING
            ],
            "steering_pending": {
                role: len(
                    self.steering(role=role, plan_id=plan_id, unread=True)["plan"]
                )
                + len(
                    self.steering(role=role, plan_id=plan_id, unread=True)["repository"]
                )
                for role in ROLES
            },
        }

    def finalize(self, plan_id: str, *, note: str = "", force: bool = False) -> dict:
        """Unseal every stage so the finished plan ships in the pull request.

        Sealing exists to keep the engineer from writing to the test while the
        work is in flight. Once the work is done that reason is gone, and the
        reviewer wants all three plans in plain Markdown next to the diff they
        justify.
        """
        with self.locked():
            manifest = self.load(plan_id)
            blockers = []
            open_defects = [
                defect["id"]
                for defect in manifest.get("defects", [])
                if defect.get("status") == PENDING
            ]
            if open_defects:
                blockers.append(f"open defect(s): {', '.join(open_defects)}")
            open_amendments = [
                amendment["id"]
                for amendment in manifest.get("amendments", [])
                if amendment.get("status") == PENDING
            ]
            if open_amendments:
                blockers.append(
                    f"unresolved amendment(s): {', '.join(open_amendments)}"
                )
            unfinished = [
                stage
                for stage in manifest.get("stages", [])
                if stage in (IMPLEMENTATION, TESTING, EVALUATION)
                and manifest.get("stage_state", {}).get(stage) != COMPLETE
            ]
            if unfinished:
                blockers.append(f"stage(s) not complete: {', '.join(unfinished)}")
            if blockers and not force:
                raise PlanError(
                    "refusing to finalize "
                    + plan_id
                    + ": "
                    + "; ".join(blockers)
                    + ". Finalizing writes the plan into the pull request as the "
                    "account of what was done; pass --force only when you mean to "
                    "ship it knowingly incomplete."
                )
            emitted = []
            for stage in manifest.get("stages", []):
                sealed_path = self.plan_dir(plan_id) / f"{stage}.sealed"
                if not sealed_path.exists():
                    continue
                body = unseal(sealed_path.read_text(encoding="utf8"))
                plain = self.plan_dir(plan_id) / f"{stage}.md"
                plain.write_text(body, encoding="utf8")
                sealed_path.unlink()
                emitted.append(str(plain.relative_to(self.root)))
            manifest["status"] = COMPLETE
            manifest["finalized_at"] = now()
            manifest["sealed"] = False
            manifest["finalized_incomplete"] = blockers if force else []
            self._save(manifest, "finalized", note=note, forced=bool(blockers and force))
            return {
                "plan": plan_id,
                "emitted": emitted,
                "status": COMPLETE,
                "shipped_incomplete": blockers if force else [],
            }

    def stage_path(self, plan_id: str, stage: str) -> Path:
        if stage not in STAGES:
            raise PlanError(f"unknown stage {stage!r}")
        sealed = self.plan_dir(plan_id) / f"{stage}.sealed"
        if sealed.exists():
            return sealed
        plain = self.plan_dir(plan_id) / f"{stage}.md"
        if plain.exists():
            return plain
        return sealed if stage in SEALED_STAGES else plain

    # -- improvement --------------------------------------------------------

    def retro(self, plan_id: str) -> dict:
        """What this plan cost beyond the work itself, and who should change.

        Every signal here is already in the manifest, so a retrospective is
        arithmetic rather than an agent re-reading a transcript. That matters:
        an improvement loop that is expensive to run does not get run.
        """
        manifest = self.load(plan_id)
        amendments = manifest.get("amendments", [])
        defects = manifest.get("defects", [])
        steering = manifest.get("steering", [])
        accepted = [item for item in amendments if item.get("status") == ACCEPTED]
        escalations = [item for item in amendments if item.get("kind") == KIND_ESCALATION]
        by_route: dict = {}
        for defect in defects:
            by_route[defect["route"]] = by_route.get(defect["route"], 0) + 1

        findings = []
        # An accepted amendment means the plan was wrong and somebody else found
        # it first. That is a planning gap, and planning gaps repeat.
        if accepted:
            findings.append(
                {
                    "signal": "accepted_amendments",
                    "count": len(accepted),
                    "target": "architect_overlay",
                    "detail": (
                        "the plan was changed after work started; the architect "
                        "lacked repository knowledge it could have had up front"
                    ),
                    "examples": [item["claim"] for item in accepted[:3]],
                }
            )
        if escalations:
            findings.append(
                {
                    "signal": "escalations",
                    "count": len(escalations),
                    "target": "architect_overlay",
                    "detail": (
                        "the engineer and tester could not converge, which means "
                        "the plan left something ambiguous"
                    ),
                    "examples": [item["claim"] for item in escalations[:3]],
                }
            )
        if by_route.get(ROUTE_TEST):
            findings.append(
                {
                    "signal": "test_defects",
                    "count": by_route[ROUTE_TEST],
                    "target": "test_infrastructure",
                    "detail": "the test harness itself failed, not the code under test",
                }
            )
        if by_route.get(ROUTE_DESIGN):
            findings.append(
                {
                    "signal": "design_defects",
                    "count": by_route[ROUTE_DESIGN],
                    "target": "design_taste",
                    "detail": (
                        "the built result did not match the design intent; the "
                        "principles the designer worked from may be incomplete"
                    ),
                }
            )
        if by_route.get(ROUTE_PLAN):
            findings.append(
                {
                    "signal": "plan_defects",
                    "count": by_route[ROUTE_PLAN],
                    "target": "architect_overlay",
                    "detail": "the tester could not verify the plan as written",
                }
            )
        if by_route.get(ROUTE_IMPLEMENTATION, 0) > 1:
            findings.append(
                {
                    "signal": "repeated_implementation_defects",
                    "count": by_route[ROUTE_IMPLEMENTATION],
                    "target": "engineer_overlay",
                    "detail": (
                        "the engineer needed several passes; the conventions or "
                        "constraints it needed may not be written down"
                    ),
                }
            )
        # The user correcting the plan mid-flight is the strongest signal there
        # is: something they consider obvious was not in the overlay.
        binding = [note for note in steering if note.get("requires_replan")]
        if binding:
            findings.append(
                {
                    "signal": "user_replanned",
                    "count": len(binding),
                    "target": "architect_overlay",
                    "detail": (
                        "the user had to redirect the plan; capture what they "
                        "corrected so the next plan starts there"
                    ),
                    "examples": [note["text"] for note in binding[:3]],
                }
            )

        return {
            "plan": plan_id,
            "title": manifest.get("title", ""),
            "status": manifest.get("status"),
            "amendment_rounds": manifest.get("rounds", 0),
            "defect_rounds": manifest.get("defect_rounds", 0),
            "escalated": bool(escalations),
            "defects_by_route": by_route,
            "steering_notes": len(steering),
            "findings": findings,
            "clean": not findings,
        }

    def note_friction(self, note: str, *, plan_id: str = "", role: str = "") -> dict:
        """Record friction an agent hit, for review rather than for nobody."""
        if not note.strip():
            raise PlanError("friction needs a note")
        role = role or current_role() or "unknown"
        with self.locked():
            payload = self._read_json(self.friction_path)
            payload.setdefault("schema_version", SCHEMA_VERSION)
            entries = payload.setdefault("entries", [])
            entry = {
                "seq": len(entries) + 1,
                "at": now(),
                "actor": actor(),
                "role": role,
                "plan": plan_id,
                "note": note.strip(),
                "status": PENDING,
            }
            entries.append(entry)
            self._write_json(self.friction_path, payload)
            return entry

    def friction(self, *, include_resolved: bool = False) -> dict:
        """Recorded friction plus retro findings, aggregated across plans.

        One bad plan is noise. The same finding across several plans is a
        change to make to the harness or to a role overlay, and that is the
        distinction this report exists to draw.
        """
        payload = self._read_json(self.friction_path)
        entries = payload.get("entries", [])
        if not include_resolved:
            entries = [entry for entry in entries if entry.get("status") == PENDING]

        totals: dict = {}
        examples: dict = {}
        for plan in self.list_plans():
            for finding in self.retro(plan["id"]).get("findings", []):
                signal = finding["signal"]
                bucket = totals.setdefault(
                    signal, {"signal": signal, "target": finding["target"], "plans": 0, "count": 0}
                )
                bucket["plans"] += 1
                bucket["count"] += finding["count"]
                examples.setdefault(signal, []).extend(finding.get("examples", []))

        recurring = sorted(
            (bucket for bucket in totals.values() if bucket["plans"] > 1),
            key=lambda bucket: (-bucket["plans"], -bucket["count"]),
        )
        for bucket in totals.values():
            bucket["examples"] = examples.get(bucket["signal"], [])[:5]
        return {
            "notes": entries,
            "signals": sorted(
                totals.values(), key=lambda bucket: (-bucket["plans"], -bucket["count"])
            ),
            "recurring": recurring,
            "verdict": (
                "recurring signals: worth a harness or overlay change"
                if recurring
                else "no signal has repeated across plans yet; keep collecting"
            ),
        }

    def resolve_friction(self, seq: int, *, note: str) -> dict:
        with self.locked():
            payload = self._read_json(self.friction_path)
            for entry in payload.get("entries", []):
                if entry.get("seq") == seq:
                    break
            else:
                raise PlanError(f"no friction note #{seq}")
            entry["status"] = RESOLVED
            entry["resolved_at"] = now()
            entry["resolution"] = note.strip()
            self._write_json(self.friction_path, payload)
            return entry

    def overlay_path(self, role: str) -> Path:
        """Where a target repository customises a role's prompt."""
        return self.store / "roles" / f"{role}.md"

    def bind_session(self, role: str, plan_id: str = "") -> None:
        """Record which role is working in this directory.

        Steering has to reach an agent whose environment we cannot set. A
        subagent's first command is its brief, so the brief is where the role
        becomes discoverable; every later `grogu` call in that directory can
        then carry steering without the model being asked to poll for it.
        """
        path = self.state_dir / "session-roles.json"
        payload = self._read_json(path) if path.exists() else {}
        payload[str(Path.cwd().resolve())] = {
            "role": role,
            "plan": plan_id,
            "at": now(),
        }
        self._write_json(path, payload)

    def session_binding(self) -> dict:
        path = self.state_dir / "session-roles.json"
        if not path.exists():
            return {}
        payload = self._read_json(path)
        return payload.get(str(Path.cwd().resolve()), {})

    def brief(self, role: str, *, plan_id: str = "", base_dir: Optional[Path] = None) -> dict:
        """Assemble a role's prompt: shared contract + repository overlay.

        Modularity lives here. Every repository gets the same pipeline contract
        and adds its own architecture, invariants and validation commands in
        `.grogu/roles/<role>.md`, instead of the harness carrying per-repository
        knowledge it cannot possibly keep current.
        """
        if role not in ROLES:
            raise PlanError(f"unknown role {role!r}")
        base = ""
        if base_dir is not None:
            candidate = Path(base_dir) / f"{role}.md"
            if candidate.is_file():
                base = candidate.read_text(encoding="utf8")
        overlay_path = self.overlay_path(role)
        overlay = overlay_path.read_text(encoding="utf8") if overlay_path.is_file() else ""
        try:
            self.bind_session(role, plan_id)
        except OSError:
            pass
        steering = self.steering(role=role, plan_id=plan_id, unread=False)
        principles: list = []
        if role == DESIGNER:
            try:
                import grogu_design

                principles = grogu_design.DesignStore().recall(limit=25)
            except Exception:
                principles = []
        return {
            "role": role,
            "plan": plan_id,
            "base": base,
            "overlay": overlay,
            "overlay_path": str(overlay_path),
            "has_overlay": bool(overlay),
            "steering": steering,
            "design_principles": principles,
            "summary": self.summary(plan_id) if plan_id else {},
        }
