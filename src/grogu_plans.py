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
recorded, visible act rather than an invisible one. Once an agent has an
identity, its first role claim is also persisted and conflicting later claims
are refused, including after a fresh shell loses the role environment.
"""

from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import uuid
import fnmatch
import json
import os
import re
import shutil
import sys
import subprocess
import secrets
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

import grogu_platform
import grogu_privacy
import grogu_markdown
import grogu_agentevents
import grogu_controlroom
import grogu_plandoc
import grogu_plandoc_anchor
import grogu_plandoc_canon
import grogu_plandoc_compile
import grogu_plandoc_patch
import grogu_plandoc_revision
import grogu_plandoc_schema
from grogu_tasks import actor, primary_worktree, repository_root, session_id
import grogu_worktrees

SCHEMA_VERSION = 1
STORE_DIRNAME = ".grogu"
PLANS_DIRNAME = "plans"

DESIGN = "design"
IMPLEMENTATION = "implementation"
TESTING = "testing"
EVALUATION = "evaluation"
STAGES = (DESIGN, IMPLEMENTATION, TESTING, EVALUATION)
SEALED_STAGES = frozenset({TESTING, EVALUATION})


def _private_design_statements() -> list:
    """The user's own design words, which must not ship in a plan.

    Imported lazily and failing open: the taste store is optional, and a
    missing one must not stop a plan being finished.
    """
    try:
        import grogu_design

        return grogu_design.DesignStore().private_statements()
    except Exception:
        return []

ARCHITECT = "architect"
DESIGNER = "designer"
ENGINEER = "engineer"
TESTER = "tester"
REVIEWER = "reviewer"
# The supervisor coordinates the others: it spawns them, carries the user's
# steering into them, harvests what they report and fixes the harness itself.
# It is deliberately not a stage writer and not a stage completer -- it does
# not do the work -- and because `approve` refuses every declared role, calling
# yourself the supervisor gives up the ability to approve a plan on the user's
# behalf. That is the point of naming it rather than acting as a bare shell.
SUPERVISOR = "supervisor"
ROLES = (ARCHITECT, DESIGNER, ENGINEER, TESTER, REVIEWER, SUPERVISOR)

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
# Who does the work a stage describes, as opposed to who writes it. The
# architect writes the testing plan; the tester is the one it is addressed to.
STAGE_OWNERS = {
    DESIGN: DESIGNER,
    IMPLEMENTATION: ENGINEER,
    TESTING: TESTER,
    EVALUATION: TESTER,
}

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
REVIEW_CLEARABLE_STATUSES = frozenset({DRAFT, NEEDS_REVIEW})

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

# Who may declare a stage finished. Writing a stage and completing it are
# different acts: the architect writes the test plan, the tester is the one who
# can say it has been carried out.
STAGE_COMPLETERS = {
    DESIGN: DESIGNER,
    IMPLEMENTATION: ENGINEER,
    TESTING: TESTER,
    EVALUATION: TESTER,
}

# Completing a stage closes the defects that were waiting on that stage's owner.
_STAGE_DEFECT_ROUTES = {
    IMPLEMENTATION: (ROUTE_IMPLEMENTATION,),
    TESTING: (ROUTE_TEST,),
    DESIGN: (ROUTE_DESIGN,),
}

HARNESS_FRICTION_THRESHOLD = 3
FRICTION_STALE_DAYS = 30

TARGET_REPO = "repo"
HOLLOW_SECTION_CHARS = 80
TARGET_HARNESS = "harness"
# A note that quotes a grogu command, or names the harness or one of its
# roles' commands, is about the harness no matter which bucket the caller
# aimed at.
_HARNESS_NOTE_PATTERN = re.compile(
    r"(?:^|[\s`'\"(])grogu[\s`'\"),.:]|\bgrogu\b.*\b(?:command|flag|CLI|--\w)"
    r"|\bthe harness\b",
    re.IGNORECASE,
)
# "repo-only" is "repo, and I mean it" -- it opts out of the content-based
# reroute below for the rare note that quotes a grogu command while being
# genuinely about this project.
TARGET_REPO_ONLY = "repo-only"
FRICTION_TARGETS = (TARGET_REPO, TARGET_HARNESS, TARGET_REPO_ONLY)

PASS = "pass"
CHANGES = "changes"
REVIEW_RUBBER_DUCK = "rubber-duck"
REVIEW_CODE = "code-review"
REVIEW_SECURITY = "security-review"
REVIEW_KINDS = (REVIEW_RUBBER_DUCK, REVIEW_CODE, REVIEW_SECURITY)
DESIGN_VERDICTS = (PASS, CHANGES)

DEFAULT_MAX_ROUNDS = 3
DEFAULT_MAX_DEFECT_ROUNDS = 3
# A backstop for the stall that produces no bounces: failures piling up on one
# route that nobody is resolving. Twice the round cap, on the reasoning that
# six open failures on a single route is past the point where the next one
# tells anybody anything new.
DEFAULT_MAX_PENDING_DEFECTS = 6
SEAL_HEADER = "grogu-sealed:v1"

GATE_IMPLEMENT = "implement"
GATE_TEST = "test"
GATE_EVALUATE = "evaluate"
GATES = (GATE_IMPLEMENT, GATE_TEST, GATE_EVALUATE)


class PlanError(Exception):
    """A plan operation the caller asked for cannot be performed."""


def now() -> str:
    """UTC, to the microsecond.

    The precision is load-bearing rather than decorative: the gates decide
    whether steering predates a plan by comparing these strings, and a plan
    created and steered in the same second would otherwise compare equal and
    the note would be silently treated as already answered.
    """
    return dt.datetime.now(dt.timezone.utc).isoformat()


def max_rounds() -> int:
    raw = os.environ.get("GROGU_PLAN_MAX_ROUNDS", "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_MAX_ROUNDS


def max_defect_rounds() -> int:
    raw = os.environ.get("GROGU_PLAN_MAX_DEFECT_ROUNDS", "")
    return int(raw) if raw.isdigit() and int(raw) > 0 else DEFAULT_MAX_DEFECT_ROUNDS


def max_pending_defects() -> int:
    raw = os.environ.get("GROGU_PLAN_MAX_PENDING_DEFECTS", "")
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return DEFAULT_MAX_PENDING_DEFECTS


def harness_friction_path() -> Path:
    home = os.environ.get("GROGU_HOME", "").strip()
    return (Path(home) if home else Path.home() / ".grogu") / "friction.json"


@contextmanager
def harness_friction_lock() -> Iterator[None]:
    """Serialise writes to the one file every repository's agents share.

    This store is pooled across every repository precisely so that a complaint
    hit in four places adds up, which also means it is the one file several
    unrelated agents write at the same moment. Unlocked read-modify-write there
    loses exactly the repetition that makes a cluster ripe.
    """
    path = harness_friction_path().with_name("friction.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with grogu_platform.exclusive_lock(handle):
            yield
    finally:
        os.close(handle)


def _write_harness_friction(payload: dict) -> None:
    path = harness_friction_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf8")
    os.replace(temporary, path)


def harness_friction(*, include_resolved: bool = False) -> list:
    """Friction with Grogu itself, gathered from every repository."""
    path = harness_friction_path()
    if not path.exists():
        return []
    try:
        entries = json.loads(path.read_text(encoding="utf8")).get("entries", [])
    except (OSError, ValueError):
        return []
    if include_resolved:
        return entries
    return [entry for entry in entries if entry.get("status") == PENDING]


_FRICTION_STOPWORDS = frozenset(
    """a an the and or but to of in on for with by is are was were be been it
    this that these those i we you it's its no not have has had do does did so
    should would could there here when then than as at from up out if too very
    just really quite thing things way ways time times""".split()
)


def _friction_tokens(note: str) -> set:
    words = re.findall(r"[a-z][a-z0-9_-]{2,}", note.lower())
    return {word for word in words if word not in _FRICTION_STOPWORDS}


def cluster_harness_friction(entries: Optional[list] = None) -> list:
    """Group friction notes that are plainly the same complaint.

    Counting notes is the wrong measure: five people describing one missing
    command is one change, and five unrelated papercuts are five. Ripeness is a
    property of the cluster, so the clustering has to happen before the
    judgement does.

    Greedy overlap rather than anything clever — the corpus is tens of short
    sentences written by the same handful of agents, and a wrong grouping costs
    a glance, not a mistake.
    """
    entries = harness_friction() if entries is None else entries
    clusters: list = []
    for entry in sorted(entries, key=lambda item: item.get("seq", 0)):
        tokens = _friction_tokens(entry.get("note", ""))
        for cluster in clusters:
            shared = tokens & cluster["tokens"]
            union = tokens | cluster["tokens"]
            if union and len(shared) / len(union) >= 0.34:
                cluster["entries"].append(entry)
                cluster["tokens"] = union
                break
        else:
            clusters.append({"tokens": tokens, "entries": [entry]})

    report = []
    for index, cluster in enumerate(clusters, start=1):
        members = cluster["entries"]
        repositories = sorted(
            {member.get("repository", "?") for member in members if member.get("repository")}
        )
        roles = sorted({member.get("role", "?") for member in members})
        first = min(member.get("at", "") for member in members)
        age = _days_since(first)
        claimed = any(member.get("claim") for member in members)
        # Repeated across repositories is the strongest signal available: it
        # cannot be explained by one project's quirks. Repetition within one
        # repository counts too, just later.
        ripe = (len(repositories) > 1 or len(members) >= 3) and not claimed
        report.append(
            {
                "id": f"f{index}",
                "title": members[0].get("note", "")[:72],
                "count": len(members),
                "repositories": repositories,
                "roles": roles,
                "first_seen": first,
                "age_days": age,
                "ripe": ripe,
                "stale": age >= FRICTION_STALE_DAYS and not claimed,
                "claim": next(
                    (member["claim"] for member in members if member.get("claim")), ""
                ),
                "seqs": [member.get("seq") for member in members],
                "notes": [member.get("note", "") for member in members],
                "reason": (
                    f"hit in {len(repositories)} repositories"
                    if len(repositories) > 1
                    else f"hit {len(members)} times"
                    if len(members) >= 3
                    else f"open {age} days"
                    if age >= FRICTION_STALE_DAYS
                    else "not yet repeated"
                ),
            }
        )
    return sorted(
        report,
        key=lambda cluster: (not cluster["ripe"], -cluster["count"], -cluster["age_days"]),
    )


def _days_since(stamp: str) -> int:
    try:
        when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, (dt.datetime.now(dt.timezone.utc) - when).days)


def claim_harness_friction(cluster_id: str, *, reference: str) -> dict:
    """Mark a cluster as being worked on, so it stops being proposed."""
    if not reference.strip():
        raise PlanError("claiming friction needs a reference: a PR, branch or issue")
    clusters = {cluster["id"]: cluster for cluster in cluster_harness_friction()}
    if cluster_id not in clusters:
        raise PlanError(f"no friction cluster {cluster_id!r}")
    cluster = clusters[cluster_id]
    with harness_friction_lock():
        payload = json.loads(harness_friction_path().read_text(encoding="utf8"))
        for entry in payload.get("entries", []):
            if entry.get("seq") in cluster["seqs"]:
                entry["claim"] = reference.strip()
                entry["claimed_at"] = now()
        _write_harness_friction(payload)
    return {"cluster": cluster_id, "claim": reference.strip(), "notes": cluster["seqs"]}


def resolve_harness_friction(seq: int, *, resolution: str) -> bool:
    path = harness_friction_path()
    if not path.exists():
        return False
    with harness_friction_lock():
        try:
            payload = json.loads(path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return False
        for entry in payload.get("entries", []):
            if entry.get("seq") == seq and entry.get("status") == PENDING:
                entry["status"] = "resolved"
                entry["resolution"] = resolution.strip()
                entry["resolved_at"] = now()
                break
        else:
            return False
        _write_harness_friction(payload)
    return True


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


def working_head() -> str:
    """HEAD of the checkout the caller is actually working in.

    Not `self.root`: plan state deliberately lives in the primary checkout so
    parallel workstreams share one lock, but the code a designer signed off on
    is the code in *their* worktree. Reading the primary HEAD meant a sign-off
    recorded the wrong commit entirely, and the gate then compared the wrong
    commit to itself and passed — approving a build nobody had looked at.
    """
    return head_commit(Path.cwd())


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
    r"\bfor (my|your) (information|awareness)\b",
    r"\b(steering|heads up|fyi|note that|keep in mind)\b",
)

# Retrieval verbs only mean "retrieval" when they lead the request. Matched
# anywhere they also fire on "build a search index", "add a status page" and
# "implement the read replica failover" — where the word is a noun in the thing
# being built — and cancelled the build verb, so the largest requests in the
# corpus were the ones routed away from the architect.
_RETRIEVAL_PATTERN = (
    r"^(?:\W*(?:please|can you|could you|would you|just|hey|grogu|and)\s+)*"
    r"(explain|summari[sz]e|describe|show|list|find|search|read|remind|tell)\b"
)

# Requests for different behaviour, phrased without a build verb. This is how
# most substantial work actually arrives: nobody says "implement correct
# rounding", they say "make the exporter round properly".
_CHANGE_PATTERNS = (
    r"\bmake\b[^.]{0,60}\b(work|support|handle|cope|correct|correctly|"
    r"consistent|reliable|idempotent|faster|safe)\b",
    r"\bget\b[^.]{0,60}\bto (stop|start|handle|support|use|cope)\b",
    # "we need proper X" is a request for work; "we need to know X" is a
    # question wearing the same opening words.
    r"\bneeds? to\b(?!\s+(know|see|check|understand|find))",
    r"\b(we|i) need\b(?!\s+to\s+(know|see|check|understand|find))",
    r"\bshould (be able to|support|handle|stop|expire|use|never|always)\b",
    r"\b(never|always)\b[^.]{0,40}\band (it|they|we) should\b",
    r"\bso (that )?it (invalidates|expires|retries|handles|supports)\b",
    r"\bstop\b[^.]{0,40}\b(drift|double|leaking|racing|duplicating)\b",
)

# Joining two things that were not joined before. These arrive phrased as if
# they were plumbing -- "wire the notifier up to SMTP" -- and they are never
# plumbing: they are a new failure surface, usually credentials, usually
# somebody else's uptime.
_INTEGRATION_PATTERNS = (
    r"\b(wire|hook|plug)\b[^.]{0,40}\b(up|in|into|to|through)\b",
    r"\bintegrate\b",
    r"\b(connect|point)\b[^.]{0,40}\b(to|at|against)\b[^.]{0,40}"
    r"\b(api|service|provider|endpoint|queue|bucket|database|db|smtp|webhook)\b",
    r"\b(switch|move|migrate|cut) (us |it |them |everything )?(over )?to\b",
    r"\breplace\b[^.]{0,40}\bwith\b",
)

# Domains where "it is a small change" is reliably wrong. Naming them is not a
# heuristic about wording but about engineering: these are the places where the
# second and third cases are the whole job.
_SUBTLE_PATTERNS = (
    r"\b(round(ing)?|precision|float(ing)?|decimal|currenc(y|ies)|minor unit)\b",
    r"\b(time ?zone|dst|daylight|utc|leap)\b",
    r"\b(unicode|encoding|utf-?8|collation|normali[sz]ation)\b",
    r"\b(concurren(t|cy)|race|deadlock|lock(ing)?|thread|parallel)\b",
    r"\b(idempoten|retry|retries|backoff|exactly.once|double.fir)\w*\b",
    r"\b(pagination|backfill|migration|schema change)\b",
    r"\b(cache invalidat|invalidates|stale)\w*\b",
    r"\b(auth|permission|token|session|expiry|expire)\w*\b",
)

_PLAN_PATTERNS = (
    r"\b(implement|create|add|introduce|architect)\b",
    # "build" and "design" are nouns as often as verbs in this repository --
    # "the build fails", "the design doc" -- and reading those as a request to
    # construct something routed bug reports to the architect.
    r"(?<!\bthe )(?<!\ba )(?<!\bour )(?<!\bthis )(?<!\bthat )\b(build|design)\b",
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
    # Communication verbs are the dangerous ones: "message the architect",
    # "email service", "text field" and "reply to the review comment" are all
    # software, so these require a personal correspondent or an actual inbox
    # rather than firing on the verb alone.
    r"\bimessage\b",
    r"\b(?:email|message|text|call|remind)\s+(?:my|his|her|their|mom|dad|wife|"
    r"husband|partner|landlord|doctor|dentist|him|her|them)\b",
    r"\b(?:check|read|search|go through)\s+(?:my\s+)?(?:email|inbox|messages|texts|mail)\b",
    r"\breply to (?:my|his|her|their|the last)\s+(?:email|message|text)\b",
    r"\bdraft (?:a|an) (?:note|email|reply)\b",
    r"\b(landlord|dentist|doctor|appointment|insurance|rent|taxes)\b",
    r"\b(gym|workout|sleep|calendar|errand|birthday|gift)\b",
    # "purchase flow" and "pricing page" are software; deciding what to buy is
    # not, so this needs the shape of a shopping question.
    r"\b(?:should i buy|where (?:can|should) i buy|cheapest|best price on)\b",
    r"\bunder \$\d+\b|\bunder \d+ dollars\b",
    r"\b(?:best|top|recommend(?:ations?)?)\b.{0,40}\b(?:headphones|mattress|laptop|"
    r"phone|chair|desk|monitor|camera|speakers?|tv)\b",
    r"\b(?:buy|purchase)\s+(?:a|an|some|me)\b.*\b(?:headphones|mattress|laptop|"
    r"phone|chair|desk|monitor|car|gift)\b",
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

# A standalone HTML report or guide -- an architecture walkthrough, a set of
# options written up for review, a comparison, a summary -- is a single
# self-contained artifact for reading, not a product surface: there is no
# separate implementation to build against a design spec, because the file
# handed back *is* the spec and the build. Routing "write me an HTML guide to
# X" through the full architect/designer/engineer/tester pipeline burns a
# planning cycle and a designer's creative reasoning re-inventing chrome that
# `grogu design html-template` already holds settled.
_STATIC_HTML_DOCUMENT_PATTERNS = (
    r"\bhtml\b[^.]{0,60}\b(report|guide|file|write.?up|writeup|summary|comparison|"
    r"walkthrough|review|reference doc|options? (doc|page|summary))\b",
    r"\b(report|guide|write.?up|writeup|summary|comparison|walkthrough|review)\b"
    r"[^.]{0,60}\b(html|standalone)\b",
    r"\bstandalone html\b",
)

# Interactivity or product-surface language means the deliverable is not a
# static document, whatever else the request says -- "an html guide with a
# login form" is a product page wearing the word 'guide'.
_INTERACTIVE_SURFACE_PATTERNS = (
    r"\b(button|form|input|toggle|checkbox|dropdown|modal|dialog)\b",
    r"\b(endpoint|api|database|db|server|backend|deploy|auth(entication)?|login)\b",
    r"\b(app|application|dashboard|webapp|web app)\b",
    r"\binteractiv(e|ity)\b",
)


def is_static_html_document(prompt: str) -> bool:
    """Whether this asks for a standalone HTML report or guide, not an app."""
    lowered = " ".join(prompt.split()).lower()
    if any(re.search(pattern, lowered) for pattern in _INTERACTIVE_SURFACE_PATTERNS):
        return False
    return any(re.search(pattern, lowered) for pattern in _STATIC_HTML_DOCUMENT_PATTERNS)


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

    # A standalone HTML report or guide is a document, not a product surface:
    # the file handed back is both the spec and the build, so there is nothing
    # for a designer to draft and an engineer to build separately. Skip that
    # unless the user explicitly asked for a plan above.
    if is_static_html_document(lowered):
        return {
            "decision": "direct",
            "design": False,
            "software": True,
            "override": False,
            "score": 0,
            "words": len(lowered.split()),
            "reasons": ["asks for a standalone HTML report or guide, not a product surface"],
            "explanation": (
                "Answer directly with `grogu design html-template` as the starting "
                "chrome. A static report is one artifact, not a designed surface "
                "with a separate build to test; the architect/designer/engineer/"
                "tester pipeline would be re-deriving settled style for no benefit."
            ),
        }

    for pattern in _DIRECT_PATTERNS:
        if re.search(pattern, lowered):
            score -= 2
            note("direct signal")
    if re.search(_RETRIEVAL_PATTERN, lowered):
        score -= 2
        note("asks to be shown something")
    for pattern in _PLAN_PATTERNS:
        if re.search(pattern, lowered):
            score += 2
            plan_hits += 1
            note("planning signal")
    for pattern in _CHANGE_PATTERNS:
        if re.search(pattern, lowered):
            score += 2
            plan_hits += 1
            note("asks for different behaviour")
            break
    for pattern in _INTEGRATION_PATTERNS:
        if re.search(pattern, lowered):
            score += 2
            plan_hits += 1
            note("joins this system to another one")
            break
    trivial = any(re.search(pattern, lowered) for pattern in _TRIVIAL_PATTERNS)
    for pattern in _SUBTLE_PATTERNS:
        # A subtle domain makes the work bigger than it looks, but not when the
        # request was explicitly a comment or a rename: "add a docstring about
        # timezones" is a docstring, and no amount of timezone is going to make
        # it an architecture question.
        if not trivial and re.search(pattern, lowered):
            score += 2
            note("touches a domain where the edge cases are the work")
            break
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


_DELIVERING: Optional[tuple] = None


def mark_delivered() -> None:
    """Ack the steering the last banner carried, now that it has been printed.

    Called after the text reaches a stream rather than when it is composed, so
    a command that fails before printing does not swallow the note. If the
    output is discarded anyway the note is lost, which is the honest cost of
    not repeating it; the gates, not the banner, are what stop a binding note
    from being ignored.
    """
    global _DELIVERING
    delivering, _DELIVERING = _DELIVERING, None
    if not delivering:
        return
    role, plan_id, root = delivering
    try:
        PlanStore(root).ack_steering(role=role, plan_id=plan_id)
    except (PlanError, OSError):
        pass  # an ack must never be why a command fails


def pending_banner(
    root: Optional[Path] = None, plan_hint: str = "", user_command: bool = False
) -> str:
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
    # A plan named on the command line identifies the plan just as well as the
    # environment variable, and the role prompts tell agents to pass it. Reading
    # only the variable meant an engineer following its own instructions was
    # never handed plan-scoped steering at all.
    plan_id = os.environ.get("GROGU_PLAN", "").strip() or plan_hint.strip()
    if user_command and not role:
        # A user-only command is proof the caller is the user, whatever role
        # last fetched a brief in this directory. Without this, running
        # `plan approve` after reading a tester's brief recorded the tester as
        # present and put it on the watch board -- so the board said an agent
        # was working when it was the user approving, and the undelivered
        # steering count started tracking a tester that had gone home.
        return harness_friction_banner(root)
    try:
        store = PlanStore(root)
        if not role:
            bound = store.session_binding()
            explicit_agent = os.environ.get("GROGU_AGENT", "").strip()
            bound_agent = (bound.get("agent", "") or "").strip()
            # A named agent sharing this checkout is not the unnamed or
            # differently named agent that last fetched a brief here. Let its
            # command establish its own role instead of delivering and
            # recording steering under somebody else's identity.
            if not explicit_agent or explicit_agent == bound_agent:
                role, plan_id = (
                    bound.get("role", ""),
                    plan_id or bound.get("plan", ""),
                )
        if not role:
            return harness_friction_banner(root)
        if plan_id:
            # Every command an agent runs is proof it exists. Without this the
            # only agents the plan knew about were the ones that had already
            # read something, so "which of my two engineers has not seen this
            # correction" was unanswerable until one of them answered it.
            if not store.note_agent_presence(plan_id, role):
                return harness_friction_banner(root)
        pending = store.steering(role=role, plan_id=plan_id, unread=True)
    except (PlanError, OSError):
        return ""  # steering must never be the reason a command fails
    notes = pending.get("repository", []) + pending.get("plan", [])
    if not notes:
        return ""
    # Remember what this banner is about to hand over. The caller acks it once
    # the text is actually on a stream, so a note enters an agent's context
    # exactly once instead of riding along with every command until the model
    # remembers to run --ack. Repetition was never the enforcement anyway:
    # binding notes hold the gate in state.
    global _DELIVERING
    _DELIVERING = (role, plan_id, root)
    lines = [
        "",
        f"── steering for the {role} ─────────────────────────────",
    ]
    for note in notes[-10:]:
        binding = " [requires replan]" if note.get("requires_replan") else ""
        lines.append(f"  #{note['seq']}{binding} {note['text']}")
    lines.append(
        "  Fold this in now. You are shown each note once, so act on it here "
        "rather than planning to come back to it."
    )
    if any(note.get("requires_replan") for note in notes):
        lines.append(
            "  This steering blocks the stage gates until the architect revises the plan."
        )
    lines.append("───────────────────────────────────────────────────────")
    return "\n".join(lines)


def harness_friction_banner(root: Optional[Path] = None) -> str:
    """Remind the user's own session of unreviewed friction with Grogu itself.

    Friction that only surfaces when somebody remembers to ask for it is
    friction nobody acts on. The user's session is where the harness actually
    gets fixed, so the reminder belongs there — rate-limited to once a day,
    because a nag on every command is itself friction.
    """
    entries = harness_friction(include_resolved=False)
    if len(entries) < HARNESS_FRICTION_THRESHOLD:
        return ""
    clusters = cluster_harness_friction(entries)
    actionable = [cluster for cluster in clusters if cluster["ripe"] or cluster["stale"]]
    if not actionable:
        return ""
    stamp_path = harness_friction_path().with_name(".friction-reminded")
    today = now()[:10]
    try:
        if stamp_path.exists() and stamp_path.read_text(encoding="utf8").strip() == today:
            return ""
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text(today, encoding="utf8")
    except OSError:
        return ""
    in_harness = is_harness_repo(root)
    lines = [
        "",
        f"── {len(actionable)} friction cluster(s) ready to fix in Grogu ──",
    ]
    for cluster in actionable[:5]:
        mark = "ripe" if cluster["ripe"] else "stale"
        lines.append(f"  {cluster['id']} [{mark}: {cluster['reason']}] {cluster['title']}")
    if in_harness:
        lines.append(
            "  You are in the Grogu repository: this is where these get fixed. "
            "Propose the work, or `grogu plan friction --ripe` for the detail."
        )
    else:
        lines.append(
            "  These are fixed in the Grogu repository, not here. "
            "`grogu plan friction --ripe` for the detail."
        )
    lines.append("──────────────────────────────────────────────────────")
    return "\n".join(lines)


def is_harness_repo(root: Optional[Path] = None) -> bool:
    """Whether the working tree is Grogu's own, where friction gets fixed."""
    try:
        base = Path(root or repository_root()).expanduser().resolve()
    except (OSError, ValueError):
        return False
    return (base / "src" / "grogu_cli.py").is_file()


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
    "layout",
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


#: Below this a body is too short to judge for padding: a three-line testing
#: plan may be terse and correct.
PADDING_FLOOR_CHARS = 600


def padded_body(body: str) -> str:
    """Why this text is padding rather than a plan, or "" if it is a plan.

    Stage bodies were checked only for being non-empty. That is enough for the
    stages somebody reads in review, and not enough for the sealed ones: the
    testing and evaluation plans are withheld from the engineer on purpose, so
    the tester is the only role that ever sees them, and a testing plan that
    says nothing produces a tester who tests nothing and reports success.

    The first tester run in this pipeline's life was handed a testing plan that
    was one sentence repeated ninety times. It coped -- it derived scope from
    the user's steering instead and filed friction -- but the next one might
    not, and neither the architect who wrote it nor the gate that let it
    through noticed anything wrong. A length threshold cannot catch this,
    because repetition is the cheapest way to reach any length.
    """
    stripped = body.strip()
    if len(stripped) < PADDING_FLOOR_CHARS:
        return ""
    sentences = [
        " ".join(piece.split()).lower()
        for piece in re.split(r"(?<=[.!?])\s+|\n", stripped)
        if len(piece.strip()) > 20
    ]
    if len(sentences) < 5:
        return ""
    unique = set(sentences)
    if len(unique) / len(sentences) >= 0.3:
        return ""
    # Character share, not just the count: a long plan that happens to repeat a
    # short boilerplate line many times is fine, and the thing being caught is
    # a body that is *mostly* the same text over and over.
    repeated = sum(
        len(sentence)
        for sentence in sentences
        if sentences.count(sentence) > 1
    )
    if repeated / max(sum(len(one) for one in sentences), 1) < 0.6:
        return ""
    commonest = max(unique, key=sentences.count)
    return (
        f"{len(sentences)} sentences, {len(unique)} of them distinct, and most "
        "of the text is the same line repeated -- this is padding, not a plan. "
        f'The commonest line appears {sentences.count(commonest)} times: '
        f'"{commonest[:70]}..."'
    )


def missing_design_sections(body: str) -> list:
    headings = _headings(body)
    return [
        section
        for section in REQUIRED_DESIGN_SECTIONS
        if not any(section in heading for heading in headings)
    ]


def _sections(body: str) -> dict:
    """Section heading -> its lines, for comparing a spec against the skeleton."""
    sections: dict = {}
    current = ""
    for line in body.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current:
            sections[current].append(line.rstrip())
    return sections


def hollow_design_sections(body: str) -> list:
    """Sections that say nothing while avoiding every banned word.

    The first designer ever run was asked to try to get a bad spec past the
    validator and did it on the second attempt, at exit 0: all eleven
    headings, one sentence under each -- "It works." "It is accessible."
    "No tokens are needed here." -- 463 bytes, no fenced block anywhere on a
    terminal surface. The adjective ban is a vocabulary filter, so vagueness
    that avoids the wordlist sails through. These are the three checks it
    said would have caught it, in its own words.
    """
    sections = _sections(body)
    hollow = []
    for heading, lines in sections.items():
        text = " ".join(line.strip() for line in lines).strip()
        if not text:
            continue
        fenced = any(line.lstrip().startswith("```") for line in lines)
        if heading.lower().startswith("layout"):
            # The one section whose whole purpose is a literal artifact.
            if not fenced:
                hollow.append(f"{heading} (no fenced block: what does it look like?)")
            continue
        if heading.lower().startswith("acceptance"):
            criteria = [
                line for line in lines if line.strip() and not line.startswith("#")
            ]
            if len(criteria) < 2:
                hollow.append(f"{heading} (fewer than two checkable statements)")
            continue
        if not fenced and len(text) < HOLLOW_SECTION_CHARS:
            hollow.append(f"{heading} ({len(text)} characters)")
    return hollow


def unfilled_design_sections(body: str) -> list:
    """Sections still holding the template's instructions instead of a decision.

    `grogu design template | grogu plan write <id> design` was accepted: the
    skeleton has every required heading and uses no adjectives, so the one
    artifact guaranteed to pass the validator was the empty one. Since a plan
    can be approved once its design stage is *written*, that was a single pipe
    between an unwritten spec and a user-approved plan.
    """
    skeleton = _sections(design_template())
    submitted = _sections(body)
    unfilled: list = []
    empty: list = []
    for heading, template_lines in skeleton.items():
        lines = submitted.get(heading)
        if lines is None:
            continue
        template_text = {line.strip() for line in template_lines if line.strip()}
        written = {line.strip() for line in lines if line.strip()}
        if not written:
            empty.append(heading)
        elif template_text and written <= template_text:
            unfilled.append(heading)
    return unfilled + [f"{heading} (empty)" for heading in empty]


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


# A standalone, single-file HTML report or guide — an architecture walkthrough,
# a set of options laid out for review, a written-up comparison — is a
# different deliverable from a designed product surface: there is no separate
# implementation to build against a spec, because the file *is* the artifact.
# Routing that through the full design/build/test pipeline burns a planning
# cycle and a designer's creative reasoning re-inventing chrome — the CSS
# variables, the sidebar table of contents, the light/dark toggle, the card and
# callout styles — that was already settled the first time somebody liked the
# result. This template is that settled chrome, held once so it is copied
# rather than re-derived: everything below `__CONTENT__` is what actually
# varies between reports.
HTML_REPORT_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light dark">
  <title>__TITLE__</title>
  <style>
    :root {
      --bg: #f4f7fb;
      --surface: rgba(255, 255, 255, 0.92);
      --surface-solid: #ffffff;
      --surface-soft: #edf3fb;
      --ink: #182337;
      --muted: #637087;
      --line: #d9e2ef;
      --brand: #3457d5;
      --brand-2: #00a88f;
      --brand-3: #7c4dff;
      --amber: #b86a00;
      --danger: #b42318;
      --success: #067647;
      --shadow: 0 18px 55px rgba(28, 45, 78, 0.12);
      --sidebar: #10182a;
      --sidebar-ink: #dfe8fb;
      --code: #101827;
      --code-ink: #dbeafe;
      --radius: 18px;
    }
    [data-theme="dark"] {
      --bg: #0b1020;
      --surface: rgba(19, 28, 48, 0.92);
      --surface-solid: #131c30;
      --surface-soft: #19243b;
      --ink: #edf4ff;
      --muted: #a8b5ca;
      --line: #2b3852;
      --brand: #84a2ff;
      --brand-2: #49d7c0;
      --brand-3: #b69cff;
      --amber: #ffbd66;
      --danger: #ff8f87;
      --success: #66d9a7;
      --shadow: 0 22px 65px rgba(0, 0, 0, 0.35);
      --sidebar: #070b14;
      --sidebar-ink: #dfe8fb;
      --code: #050914;
      --code-ink: #dbeafe;
    }
    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      background:
        radial-gradient(circle at 84% 8%, rgba(52, 87, 213, 0.12), transparent 28rem),
        radial-gradient(circle at 35% 90%, rgba(0, 168, 143, 0.09), transparent 32rem),
        var(--bg);
      color: var(--ink);
      font: 16px/1.58 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    button, input { font: inherit; }
    a { color: var(--brand); }
    code {
      border: 1px solid var(--line);
      border-radius: 7px;
      background: var(--surface-soft);
      padding: 0.08rem 0.35rem;
      font: 0.88em/1.4 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    pre {
      overflow: auto;
      border-radius: 14px;
      background: var(--code);
      color: var(--code-ink);
      padding: 1rem 1.15rem;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,0.06);
    }
    pre code { border: 0; background: none; padding: 0; color: inherit; }
    .app { display: grid; grid-template-columns: 294px minmax(0, 1fr); min-height: 100vh; }
    .sidebar {
      position: sticky; top: 0; height: 100vh; overflow-y: auto;
      background: linear-gradient(180deg, rgba(66, 93, 189, 0.2), transparent 32%), var(--sidebar);
      color: var(--sidebar-ink);
      padding: 1.6rem 1.15rem;
      border-right: 1px solid rgba(255,255,255,0.08);
    }
    .brand-lockup { display: flex; align-items: center; gap: 0.8rem; margin-bottom: 1.5rem; padding: 0 0.35rem; }
    .logo {
      display: grid; place-items: center; width: 46px; height: 46px; border-radius: 14px;
      background: linear-gradient(145deg, #6f8cff, #1cc9ab);
      color: #07101f; font-weight: 900; letter-spacing: -0.06em;
      box-shadow: 0 12px 30px rgba(83, 122, 255, 0.32);
    }
    .brand-lockup strong { display: block; font-size: 1rem; }
    .brand-lockup span { display: block; color: #91a1bc; font-size: 0.78rem; }
    .nav-label {
      margin: 1.2rem 0.6rem 0.4rem; color: #7788a6; font-size: 0.7rem;
      font-weight: 800; letter-spacing: 0.12em; text-transform: uppercase;
    }
    .nav a {
      display: flex; align-items: center; gap: 0.7rem; margin: 0.2rem 0; border-radius: 10px;
      color: #b9c5da; padding: 0.62rem 0.75rem; text-decoration: none; transition: 160ms ease;
    }
    .nav a:hover, .nav a.active { color: white; background: rgba(255,255,255,0.09); transform: translateX(2px); }
    .nav-num {
      display: grid; place-items: center; width: 24px; height: 24px; flex: 0 0 auto;
      border-radius: 8px; background: rgba(255,255,255,0.08); font-size: 0.72rem; font-weight: 800;
    }
    .sidebar-foot { margin-top: 1.5rem; border-top: 1px solid rgba(255,255,255,0.1); padding: 1rem 0.55rem 0; color: #8393ae; font-size: 0.76rem; }
    main { width: min(1320px, 100%); padding: 2rem clamp(1.1rem, 3vw, 3.4rem) 5rem; margin: 0 auto; }
    .mobile-nav { display: none; margin-bottom: 1rem; }
    .mobile-nav select { width: 100%; padding: 0.6rem 0.8rem; border-radius: 10px; border: 1px solid var(--line); background: var(--surface-solid); color: var(--ink); }
    @media (max-width: 880px) {
      .app { grid-template-columns: 1fr; }
      .sidebar { display: none; }
      .mobile-nav { display: block; }
    }
    .toolbar { position: sticky; top: 0.75rem; z-index: 20; display: flex; justify-content: flex-end; gap: 0.55rem; pointer-events: none; }
    .toolbar button {
      pointer-events: auto; cursor: pointer; border: 1px solid var(--line); border-radius: 999px;
      background: var(--surface); color: var(--ink); padding: 0.55rem 0.8rem;
      box-shadow: 0 8px 28px rgba(22, 34, 54, 0.1); backdrop-filter: blur(12px);
    }
    .toolbar button:hover { border-color: var(--brand); }
    .hero {
      position: relative; overflow: hidden; min-height: 320px; display: grid; align-items: center;
      margin: -2.8rem -1rem 2rem; border-radius: 0 0 30px 30px; padding: 5.6rem clamp(1.4rem, 5vw, 4.4rem) 3.7rem;
      background: linear-gradient(135deg, rgba(8, 20, 48, 0.97), rgba(30, 57, 125, 0.95) 52%, rgba(0, 113, 104, 0.92)), #102040;
      color: white; box-shadow: var(--shadow);
    }
    .hero::before, .hero::after { content: ""; position: absolute; border-radius: 50%; border: 1px solid rgba(255,255,255,0.15); }
    .hero::before { width: 420px; height: 420px; right: -90px; top: -140px; }
    .hero::after { width: 260px; height: 260px; right: 90px; bottom: -170px; }
    .eyebrow {
      display: inline-flex; width: fit-content; align-items: center; gap: 0.5rem; border: 1px solid rgba(255,255,255,0.2);
      border-radius: 999px; background: rgba(255,255,255,0.08); padding: 0.35rem 0.72rem; color: #dfe8ff;
      font-size: 0.75rem; font-weight: 800; letter-spacing: 0.08em; text-transform: uppercase;
    }
    .hero h1 { max-width: 900px; margin: 1rem 0 0.8rem; font-size: clamp(2.5rem, 6vw, 5.4rem); line-height: 0.98; letter-spacing: -0.055em; }
    .hero p { max-width: 780px; margin: 0; color: #cedaf2; font-size: clamp(1rem, 1.8vw, 1.25rem); }
    .hero-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 0.75rem; margin-top: 2rem; max-width: 920px; }
    .hero-stat { border: 1px solid rgba(255,255,255,0.14); border-radius: 14px; background: rgba(255,255,255,0.07); padding: 0.8rem 0.95rem; backdrop-filter: blur(9px); }
    .hero-stat strong { display: block; font-size: 1.35rem; }
    .hero-stat span { color: #aebcda; font-size: 0.75rem; }
    section { scroll-margin-top: 1.5rem; margin: 2rem 0; }
    .section-head { display: flex; align-items: flex-end; justify-content: space-between; gap: 1rem; margin: 3.2rem 0 1.2rem; }
    .section-head h2 { margin: 0; font-size: clamp(1.8rem, 3vw, 2.7rem); line-height: 1.1; letter-spacing: -0.035em; }
    .section-head p { max-width: 660px; margin: 0.5rem 0 0; color: var(--muted); }
    .kicker { color: var(--brand); font-size: 0.73rem; font-weight: 850; letter-spacing: 0.11em; text-transform: uppercase; }
    .card { border: 1px solid var(--line); border-radius: var(--radius); background: var(--surface); padding: 1.25rem; box-shadow: var(--shadow); backdrop-filter: blur(10px); }
    .card h3 { margin: 0 0 0.55rem; }
    .card p:last-child { margin-bottom: 0; }
    .grid-2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 1rem; }
    .grid-3 { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 1rem; }
    .grid-4 { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 1rem; }
    @media (max-width: 880px) { .grid-2, .grid-3, .grid-4 { grid-template-columns: 1fr; } }
    .callout {
      display: grid; grid-template-columns: 44px 1fr; gap: 0.8rem; align-items: start;
      border: 1px solid color-mix(in srgb, var(--brand) 30%, var(--line));
      border-radius: 16px; background: color-mix(in srgb, var(--brand) 7%, var(--surface)); padding: 1rem 1.1rem;
    }
    .callout.warn { border-color: color-mix(in srgb, var(--amber) 38%, var(--line)); background: color-mix(in srgb, var(--amber) 8%, var(--surface)); }
    .callout-icon { display: grid; place-items: center; width: 38px; height: 38px; border-radius: 12px; background: var(--brand); color: white; font-weight: 900; }
    .callout.warn .callout-icon { background: var(--amber); }
    .callout strong { display: block; margin-bottom: 0.15rem; }
    .callout p { margin: 0; color: var(--muted); }
    .footer { margin-top: 4rem; padding-top: 1.5rem; border-top: 1px solid var(--line); color: var(--muted); font-size: 0.85rem; }
    @media print { .toolbar, .mobile-nav { display: none !important; } .sidebar { display: none; } .app { display: block; } }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="brand-lockup">
        <div class="logo">__LOGO__</div>
        <div>
          <strong>__TITLE__</strong>
          <span>__SUBTITLE__</span>
        </div>
      </div>
      <nav class="nav" aria-label="Report navigation">
__NAV_ITEMS__
      </nav>
      <div class="sidebar-foot">
        __FOOTNOTE__
      </div>
    </aside>

    <main>
      <div class="mobile-nav">
        <select aria-label="Jump to section" onchange="location.hash=this.value">
__NAV_OPTIONS__
        </select>
      </div>

      <div class="toolbar">
        <button class="print-button" onclick="window.print()" title="Print or save as PDF">Print</button>
        <button id="themeToggle" title="Toggle color theme">Theme</button>
      </div>

      <header class="hero" id="__FIRST_ANCHOR__">
        <div>
          <span class="eyebrow">__EYEBROW__</span>
          <h1>__HEADLINE__</h1>
          <p>__DEK__</p>
        </div>
      </header>

      __CONTENT__

      <footer class="footer">
        <strong>__TITLE__</strong><br>
        Self-contained HTML artifact. No external JavaScript, fonts, images, or network requests are required to view it.
      </footer>
    </main>
  </div>

  <script>
    (() => {
      const root = document.documentElement;
      const toggle = document.getElementById("themeToggle");
      const key = "__STORAGE_KEY__";
      const saved = localStorage.getItem(key);
      if (saved) {
        root.dataset.theme = saved;
      } else if (window.matchMedia("(prefers-color-scheme: dark)").matches) {
        root.dataset.theme = "dark";
      }
      toggle.addEventListener("click", () => {
        const next = root.dataset.theme === "dark" ? "light" : "dark";
        root.dataset.theme = next;
        localStorage.setItem(key, next);
      });

      const links = [...document.querySelectorAll(".nav a")];
      const sections = links
        .map((link) => document.querySelector(link.getAttribute("href")))
        .filter(Boolean);
      if (sections.length) {
        const observer = new IntersectionObserver((entries) => {
          const visible = entries.filter((entry) => entry.isIntersecting);
          if (!visible.length) return;
          const current = visible[0].target.id;
          links.forEach((link) => link.classList.toggle("active", link.getAttribute("href") === `#${current}`));
        }, { rootMargin: "-20% 0px -70% 0px" });
        sections.forEach((section) => observer.observe(section));
      }
    })();
  </script>
</body>
</html>
"""


def _slugify(text: str, fallback: str = "section") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or fallback


def html_report_template(
    title: str = "<report title>",
    *,
    subtitle: str = "Review guide",
    eyebrow: str = "",
    headline: str = "",
    dek: str = "",
    logo: str = "",
    footnote: str = "",
    sections: Optional[List[str]] = None,
) -> str:
    """The standing chrome for a standalone HTML report or guide.

    This is the settled style: CSS variables for a light/dark theme, a sticky
    sidebar table of contents with a mobile fallback, a print button, a hero
    header, and card/callout/grid primitives. `sections` names the reader's
    table of contents — supply the section titles in order and get back
    numbered nav links, matching `<section id="...">` anchors, and a mobile
    `<select>`, all wired to the same anchors; anything you write inside those
    `<section>` tags is the actual content, which is the only part that should
    take real design or engineering effort.
    """
    sections = list(sections) if sections else ["Overview"]
    used_ids: List[str] = []
    nav_items = []
    nav_options = []
    for index, name in enumerate(sections, start=1):
        anchor = _slugify(name, fallback=f"section-{index}")
        base_anchor = anchor
        suffix = 2
        while anchor in used_ids:
            anchor = f"{base_anchor}-{suffix}"
            suffix += 1
        used_ids.append(anchor)
        number = f"{index:02d}"
        nav_items.append(f'        <a href="#{anchor}"><span class="nav-num">{number}</span> {name}</a>')
        nav_options.append(f'          <option value="#{anchor}">{number} - {name}</option>')

    content = "\n\n".join(
        f'      <section id="{anchor}">\n        <div class="section-head">\n          <h2>{name}</h2>\n        </div>\n        <!-- content for "{name}" goes here -->\n      </section>'
        for anchor, name in zip(used_ids, sections)
    )

    storage_key = f"{_slugify(title, fallback='report')}-theme"
    replacements = {
        "__TITLE__": title,
        "__SUBTITLE__": subtitle,
        "__EYEBROW__": eyebrow or subtitle,
        "__HEADLINE__": headline or title,
        "__DEK__": dek,
        "__LOGO__": logo or (title[:3].upper() if title else "RPT"),
        "__FOOTNOTE__": footnote,
        "__NAV_ITEMS__": "\n".join(nav_items),
        "__NAV_OPTIONS__": "\n".join(nav_options),
        "__FIRST_ANCHOR__": used_ids[0],
        "__STORAGE_KEY__": storage_key,
        "__CONTENT__": content,
    }
    rendered = HTML_REPORT_TEMPLATE
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    return rendered


class PlanStore:
    """Plan artifacts, the stage gates, and the review loop for one repository."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = primary_worktree(Path(root or repository_root()).expanduser().resolve())
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
        self._protect_working_state()
        handle = os.open(self.state_dir / "plans.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            with grogu_platform.exclusive_lock(handle):
                yield
        finally:
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

    # Working state, not the account. `finalize` stages the plan Markdown and
    # deliberately leaves these behind -- the manifest carries session ids,
    # actor strings, the full text of every amendment and every steering note,
    # and the revisions directory holds superseded drafts. That was enforced by
    # a comment in one function, which a user or an agent running `git add -A`
    # walks straight past, in a repository that is public.
    #
    # `review.json` carries the user's unfiltered words about the plan (the
    # review workspace's threads and covering notes), so it is local-only for
    # the same reason and by the same enforcement as the manifest.
    _LOCAL_ONLY = (
        "manifest.json\n"
        "revisions/\n"
        "*.sealed\n"
        "review.json\n"
        "HEAD\n"
        "graph/\n"
        "log/\n"
        "snapshots/\n"
        "proposals/\n"
        "projections/\n"
        "revision-meta/\n"
        "legacy/\n"
        "recovery/\n"
        "registrations/\n"
    )

    def _protect_working_state(self) -> None:
        try:
            self.plans_dir.mkdir(parents=True, exist_ok=True)
            marker = self.plans_dir / ".gitignore"
            if not marker.exists():
                marker.write_text(self._LOCAL_ONLY, encoding="utf8")
                return
            # A marker already written by an older Grogu never gained
            # `review.json`, and that gap is a privacy failure, not a cosmetic
            # one: a `git add -A` would stage the user's review comments in a
            # public repository. Append whatever entry is missing, preserving
            # every line already there.
            existing = marker.read_text(encoding="utf8")
            lines = existing.splitlines()
            present = {line.strip() for line in lines}
            missing = [
                entry
                for entry in self._LOCAL_ONLY.splitlines()
                if entry and entry not in present
            ]
            if missing:
                suffix = "" if existing.endswith("\n") or not existing else "\n"
                marker.write_text(
                    existing + suffix + "\n".join(missing) + "\n", encoding="utf8"
                )
        except OSError:
            return

    def plan_dir(self, plan_id: str) -> Path:
        legacy = self.plans_dir / plan_id
        package = self.plans_dir / f"{plan_id}.plan"
        if legacy.exists() and package.exists():
            raise PlanError(
                f"plan {plan_id} exists in both layouts: {legacy} and {package}. "
                "Remove the one you do not want, then retry; Grogu refuses a "
                "dual-path plan because neither copy can be treated as the "
                "single writable truth."
            )
        return package if package.exists() else legacy

    def legacy_plan_dir(self, plan_id: str) -> Path:
        return self.plans_dir / plan_id

    def document_plan_dir(self, plan_id: str) -> Path:
        return self.plans_dir / f"{plan_id}.plan"

    def is_document_plan(self, plan_id: str) -> bool:
        return self.plan_dir(plan_id) == self.document_plan_dir(plan_id)

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
        reference = (reference or "").strip()
        if not reference:
            # An empty reference would prefix-match every plan, so the
            # ambient plan is the only sane reading of "the plan".
            reference = os.environ.get("GROGU_PLAN", "").strip()
        if not reference:
            raise PlanError(
                "no plan given and $GROGU_PLAN is not set; pass a plan id"
            )
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
        plan_ids = set()
        for path in self.plans_dir.iterdir():
            if not path.is_dir():
                continue
            name = path.name
            plan_ids.add(name[:-5] if name.endswith(".plan") else name)
        plans = [
            self._read_json(self.plan_dir(plan_id) / "manifest.json")
            for plan_id in sorted(plan_ids)
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

    # -- stage shape -------------------------------------------------------

    def add_stage(self, plan_id: str, stage: str, *, role: str = "") -> dict:
        """Add a design or evaluation stage the architect judges warranted.

        Stages used to be fixed at creation, which meant the architect --
        the only role that reads the whole request -- could not add the one
        stage they alone are qualified to call for.
        """
        role = role or current_role() or ARCHITECT
        self.claim_agent_role(plan_id, role)
        if role != ARCHITECT:
            raise PlanError(f"role {role!r} may not change the shape of a plan")
        if stage not in (DESIGN, EVALUATION):
            raise PlanError(
                f"{stage} is not optional; every plan has implementation and testing"
            )
        if self.is_document_plan(plan_id):
            return PlanDocumentStore(self, plan_id).add_stage(stage, role=role)
        with self.locked():
            manifest = self.load(plan_id)
            if manifest.get("status") in (COMPLETE, SUPERSEDED):
                raise PlanError(f"plan {plan_id} is {manifest['status']}")
            if stage in manifest.get("stages", []):
                raise PlanError(f"plan {plan_id} already has a {stage} stage")
            stages = [item for item in STAGES if item in manifest["stages"] or item == stage]
            manifest["stages"] = stages
            manifest.setdefault("stage_state", {})[stage] = PENDING
            manifest.setdefault("stage_written", {})[stage] = False
            manifest.get("declined_stages", {}).pop(stage, None)
            path = self.stage_path(plan_id, stage)
            if not path.exists():
                body = f"# {manifest.get('title', plan_id)} — {stage} plan\n\n_Not written yet._\n"
                path.write_text(
                    seal(body) if stage in SEALED_STAGES else body, encoding="utf8"
                )
            return self._save(manifest, "stage_added", stage=stage)

    def reset_stage(self, plan_id: str, stage: str, *, role: str = "") -> dict:
        """Un-write a stage, because there was no way back from a bad write.

        An architect probing the design validator wrote the template stub into
        a plan, could not withdraw it, and repaired the state by hand-editing
        manifest.json. An agent editing harness internals to undo a supported
        command is worse than any bug it was chasing.
        """
        role = role or current_role() or ARCHITECT
        self.claim_agent_role(plan_id, role)
        if role != ARCHITECT:
            raise PlanError(f"role {role!r} may not reset a stage; that is the architect's")
        if stage not in STAGES:
            raise PlanError(f"unknown stage {stage!r}")
        if self.is_document_plan(plan_id):
            return PlanDocumentStore(self, plan_id).reset_stage(stage, role=role)
        with self.locked():
            manifest = self.load(plan_id)
            if stage not in manifest.get("stages", []):
                raise PlanError(f"plan {plan_id} has no {stage} stage")
            if manifest.get("status") == COMPLETE:
                raise PlanError(f"plan {plan_id} is finalized; supersede it instead")
            for path in (self.plan_dir(plan_id) / f"{stage}.md",
                         self.plan_dir(plan_id) / f"{stage}.sealed"):
                if path.exists():
                    path.unlink()
            body = f"# {manifest.get('title', plan_id)} — {stage} plan\n\n_Not written yet._\n"
            target = self.stage_path(plan_id, stage)
            target.write_text(
                seal(body) if stage in SEALED_STAGES else body, encoding="utf8"
            )
            manifest.setdefault("stage_written", {})[stage] = False
            manifest.setdefault("stage_state", {})[stage] = PENDING
            return self._save(manifest, "stage_reset", stage=stage)

    def decline_stage(self, plan_id: str, stage: str, why: str, *, role: str = "") -> dict:
        """Record that a stage was considered and judged unnecessary.

        Without this, a missing evaluation stage reads identically whether
        the architect ruled it out or never thought about it.
        """
        role = role or current_role() or ARCHITECT
        self.claim_agent_role(plan_id, role)
        if role != ARCHITECT:
            raise PlanError(f"role {role!r} may not change the shape of a plan")
        if stage not in (DESIGN, EVALUATION):
            raise PlanError(f"{stage} is not optional and cannot be declined")
        if not why.strip():
            raise PlanError("declining a stage needs a reason the next reader can weigh")
        with self.locked():
            manifest = self.load(plan_id)
            if stage in manifest.get("stages", []):
                raise PlanError(
                    f"plan {plan_id} already has a {stage} stage; supersede the plan instead"
                )
            manifest.setdefault("declined_stages", {})[stage] = {
                "why": why.strip(),
                "at": now(),
                "by": actor(),
            }
            return self._save(manifest, "stage_declined", stage=stage)

    def require_review(self, plan_id: str, *, role: str = "") -> dict:
        """Hold work until the user approves.

        The architect is usually spawned onto a plan someone else created,
        so the flag at creation time is not enough: the role that discovers
        the request wants review needs to be able to say so.
        """
        role = role or current_role() or ARCHITECT
        self.claim_agent_role(plan_id, role)
        if role != ARCHITECT:
            raise PlanError(f"role {role!r} may not put a plan up for review")
        with self.locked():
            manifest = self.load(plan_id)
            if manifest.get("approved_at"):
                raise PlanError(f"plan {plan_id} was already approved")
            if manifest.get("review_required"):
                return manifest
            manifest["review_required"] = True
            # Holding a plan for review after work has already landed does
            # not un-land it. Say so, rather than letting the architect
            # believe it has closed a gate the work already walked through.
            done = [
                stage
                for stage in manifest.get("stages", [])
                if manifest.get("stage_state", {}).get(stage) == COMPLETE
            ]
            manifest = self._save(manifest, "review_required")
            if done:
                manifest["warnings"] = [
                    f"{', '.join(done)} already completed before review was required; "
                    "the hold applies to remaining stages only"
                ]
            return manifest

    def clear_review_requirement(
        self,
        plan_id: str,
        reason: str,
        *,
        role: str = "",
        as_user: bool = False,
    ) -> dict:
        """Remove a mistaken hold as a declared architect or explicit user."""
        role = role or current_role()
        if role and role != ARCHITECT:
            raise PlanError(
                f"role {role!r} may not clear a plan's review requirement; "
                "that is the architect's"
            )
        if as_user and current_role():
            raise PlanError(
                f"--as-user is for the user; this session is running as the "
                f"{current_role()}"
            )
        if role and not as_user:
            self.claim_agent_role(plan_id, role)
        if not role and not as_user:
            raise PlanError(
                "clearing a review requirement needs a role: export "
                "GROGU_ROLE=architect, pass --role architect, or pass --as-user "
                "if you are the user"
            )
        reason = reason.strip()
        if not reason:
            raise PlanError(
                "clearing a review requirement needs a reason the next reader can audit"
            )
        with self.locked():
            manifest = self.load(plan_id)
            if manifest.get("approved_at") or manifest.get("status") == APPROVED:
                raise PlanError(
                    f"plan {plan_id} was already approved; its review requirement "
                    "cannot be cleared"
                )
            status = manifest.get("status")
            if status not in REVIEW_CLEARABLE_STATUSES:
                raise PlanError(
                    f"plan {plan_id} is {status or 'in an unknown state'}; "
                    "only draft or needs_review plans may clear an unapproved "
                    "review requirement"
                )
            if not manifest.get("review_required"):
                raise PlanError(f"plan {plan_id} has no review requirement to clear")
            manifest["review_required"] = False
            return self._save(
                manifest,
                "review_cleared",
                reason=reason,
                role=role or "user",
                as_user=bool(as_user),
            )

    def commission(
        self, plan_id: str, role: str, brief: str, *, by: str = "", replace: bool = False
    ) -> dict:
        """Let the architect say what it wants from a role, in its own voice.

        An architect could open a design stage and then had no way to tell the
        designer what the work was: the design stage is written by the designer
        alone, `workstream --brief` is engineer-facing, and `plan brief --role
        designer` carried the taste principles and the plan status but not one
        word about the job. The only channel that reached the designer was
        `plan steer --role designer`, which arrives attributed to the user --
        so an architect following its own contract had to put its statement of
        work into the user's mouth, to the one role whose whole job is
        weighting the user's taste above its own inference.
        """
        by = by or current_role() or ARCHITECT
        self.claim_agent_role(plan_id, by)
        if by != ARCHITECT:
            raise PlanError(f"role {by!r} may not commission work; that is the architect's")
        if role not in ROLES:
            raise PlanError(f"unknown role {role!r}")
        if role == ARCHITECT:
            raise PlanError("the architect does not commission itself")
        if not brief.strip():
            raise PlanError("a commission needs a brief saying what the work is")
        with self.locked():
            manifest = self.load(plan_id)
            existing = (manifest.get("commissions") or {}).get(role)
            if existing and not replace:
                raise PlanError(
                    f"the {role} is already commissioned on {plan_id}; pass "
                    "--replace to overwrite it (the current brief starts: "
                    + existing["brief"][:60].replace("\n", " ")
                    + "...)"
                )
            manifest.setdefault("commissions", {})[role] = {
                "brief": brief.strip(),
                "at": now(),
                "by": by,
                "replaced": existing.get("brief") if existing else "",
            }
            # A commission is read once, at the start. Replacing it after the
            # role has already delivered against the old one left a finished
            # stage attached to a statement of work nobody had ever seen --
            # the same failure `write_stage` already refuses for plan bodies.
            # Reopening is the honest state: the work may be fine, but nothing
            # has checked it against what the architect now says it wants.
            warnings = []
            written = (manifest.get("stage_written") or {})
            unchanged = bool(existing) and existing.get("brief", "") == brief.strip()
            for stage, writers in STAGE_WRITERS.items():
                if unchanged:
                    break
                if role not in writers or not written.get(stage):
                    continue
                if manifest.get("stage_state", {}).get(stage) == COMPLETE:
                    manifest["stage_state"][stage] = PENDING
                    warnings.append(
                        f"the {stage} stage was complete against the brief you "
                        f"just replaced, so it is pending again. Tell the "
                        f"{role} what changed -- it read the commission once, "
                        "at the start, and will not read it again by itself."
                    )
                else:
                    warnings.append(
                        f"the {role} has already written the {stage} stage "
                        "against the brief you just replaced. It will not "
                        "re-read the commission on its own; relay the change."
                    )
            saved = self._save(manifest, "commissioned", stage=role)
            saved["warnings"] = warnings
            return saved

    # -- stage bodies ------------------------------------------------------

    def write_stage(
        self,
        plan_id: str,
        stage: str,
        body: str,
        *,
        role: str = "",
        replace: bool = False,
    ) -> dict:
        """Stages are written by the role that owns them; others propose.

        Every write keeps the text it replaced. An architect probing this
        command replaced thirteen kilobytes of a *completed* implementation
        plan with a fifty-five byte test string, exit 0, no confirmation, and
        got it back only because it had thought to copy the file out of
        `.grogu` first. One wrong command was the only place in the pipeline
        where work was lost that no other role could recover.
        """
        role = role or current_role() or ARCHITECT
        self.claim_agent_role(plan_id, role)
        writers = STAGE_WRITERS.get(stage, frozenset({ARCHITECT}))
        if role not in writers:
            raise PlanError(
                f"role {role!r} may not write the {stage} stage "
                f"(owners: {', '.join(sorted(writers))}); propose a change with "
                "`grogu plan amend` and let the owner decide"
            )
        if self.is_document_plan(plan_id):
            return PlanDocumentStore(self, plan_id).replace_stage(
                stage, body, role=role, replace=replace
            )
        with self.locked():
            manifest = self.load(plan_id)
            if stage not in manifest.get("stages", []):
                raise PlanError(
                    f"plan {plan_id} has no {stage} stage; add one with "
                    f"`grogu plan shape {plan_id} --add {stage}` when it is warranted"
                )
            if not body.strip():
                raise PlanError("refusing to write an empty plan stage")
            padding = padded_body(body)
            if padding:
                raise PlanError(
                    f"refusing to write the {stage} stage: {padding}\n"
                    "The testing and evaluation stages are sealed from the "
                    "engineer, so nobody else reads them before the tester "
                    "acts on them -- a stage that says nothing here produces a "
                    "tester that tests nothing and reports that it passed."
                )
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
                unfilled = unfilled_design_sections(body)
                if unfilled:
                    raise PlanError(
                        "these design sections still hold the template's own "
                        "instructions: "
                        + ", ".join(unfilled)
                        + ". The skeleton passes every other check by "
                        "construction, so writing it back unchanged is how an "
                        "unwritten spec reaches the user for approval."
                    )
                hollow = hollow_design_sections(body)
                if hollow:
                    raise PlanError(
                        "these design sections say nothing a tester could "
                        "check: "
                        + ", ".join(hollow)
                        + ". Every one of them is a decision the engineer will "
                        "otherwise make by default."
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
            previous = path.read_text(encoding="utf8") if path.exists() else ""
            state = manifest.get("stage_state", {}).get(stage)
            payload = seal(body) if sealed else body
            changed = previous != payload
            # Every byte count below must be the plan as a person reads it. A
            # sealed stage is stored base64+zlib, so `was`/`now` and the
            # truncation warning were reporting compressed sizes while the CLI
            # reported plaintext -- the same write announced 13178 bytes and
            # "replaced 379 bytes". Worse, the "did this plan just lose half
            # itself" check was comparing compression ratios, so it fired on
            # honest edits and stayed quiet on real truncation.
            previous_text = (unseal(previous) if sealed and previous else previous)
            if changed and previous and state == COMPLETE and not replace:
                raise PlanError(
                    f"the {stage} stage is complete and this rewrites it "
                    f"({len(previous_text)} bytes -> {len(body)} bytes). That "
                    "reopens the stage and invalidates the work done against "
                    "it. Pass --replace if you mean it; the text you replace "
                    "is kept either way."
                )
            revision = 0
            warnings: list = []
            if changed and previous:
                revision = self._keep_revision(
                    plan_id, stage, previous, manifest, plain_bytes=len(previous_text)
                )
                # A plan that loses most of itself in one write is almost
                # always a mistake -- a probe, a truncated pipe, a --body where
                # a --file was meant. Say so on the spot, while the person who
                # did it is still looking.
                if len(body) * 2 < len(previous_text):
                    warnings.append(
                        f"the {stage} plan went from {len(previous_text)} bytes to "
                        f"{len(body)}. If that was not deliberate, "
                        f"`grogu plan show {plan_id} --stage {stage} "
                        f"--revision {revision}` is the text you just replaced."
                    )
            path.write_text(payload, encoding="utf8")
            manifest["last_write"] = {
                "stage": stage,
                "by": role,
                "at": now(),
                "was": len(previous_text),
                "now": len(body),
                "revision": revision,
            }
            manifest.setdefault("stage_written", {})[stage] = True
            if changed and manifest.get("stage_state", {}).get(stage) == COMPLETE:
                # A completed stage is a claim that the work matches the plan.
                # Rewriting the plan under it leaves that claim attached to
                # text nobody can read any more, and the only person who knows
                # it is stale is the one who just made it so.
                manifest["stage_state"][stage] = PENDING
                manifest.setdefault("reopened", []).append(
                    {"stage": stage, "at": now(), "why": "the plan was rewritten"}
                )
                # The workstreams have to come back with it. An architect that
                # accepted an amendment and rewrote the plan found both
                # workstreams still recorded complete against text that no
                # longer existed, and `workstream --replace` refused to touch
                # them precisely because they were complete -- a state only the
                # rewrite could have created and nothing could leave.
                if stage == IMPLEMENTATION:
                    done = manifest.setdefault("workstream_state", {})
                    for name in list(done):
                        if done[name] == COMPLETE:
                            done[name] = PENDING
                notes = manifest.setdefault("steering", [])
                notes.append(
                    {
                        "seq": len(notes) + 1,
                        "at": now(),
                        "actor": actor(),
                        "role": STAGE_OWNERS.get(stage, ENGINEER),
                        "text": (
                            f"the {stage} plan changed after you completed that "
                            "stage; re-read it and complete it again"
                        ),
                        # Not the user's words. Attributing the harness's own
                        # bookkeeping to the user is the same mistake that put
                        # an architect's brief in the user's mouth.
                        "automatic": True,
                        "requires_replan": False,
                    }
                )
            for amendment in manifest.get("amendments", []):
                # An accepted amendment is answered by the plan actually
                # changing. Marking it incorporated on any write meant a
                # byte-identical no-op re-write flipped the gate from blocked
                # to allowed without a word of the plan being different.
                if (
                    changed
                    and amendment.get("status") == ACCEPTED
                    and amendment.get("stage") == stage
                    and not amendment.get("incorporated")
                ):
                    amendment["incorporated"] = True
                    amendment["incorporated_at"] = now()
            manifest.setdefault("design_review", None)
            if stage == DESIGN and manifest.get("design_review"):
                manifest["design_review"] = None
            if stage == DESIGN:
                # Rewriting the design *is* the answer to a design defect. The
                # only other closing edge was `stage design complete`, which
                # nothing in the normal path runs and no prompt asks for, so a
                # single design defect shut the test gate for the rest of the
                # plan's life.
                self._close_defects_for(
                    manifest,
                    routes=(ROUTE_DESIGN,),
                    reason="the design was revised",
                )
            if role == ARCHITECT:
                # The architect rewriting a stage *is* the act of folding
                # steering in. Without this, `--requires-replan` leaves the plan
                # in needs_review until a human approves it — which the docs
                # never claimed and which strands every autopilot run.
                manifest["steering_folded_at"] = now()
                manifest["steering_folded_seq"] = max(
                    [note.get("seq", 0) for note in manifest.get("steering", [])] or [0]
                )
                if manifest.get("status") == NEEDS_REVIEW:
                    if manifest.get("review_required"):
                        # The user wanted to see this plan. They steered it, so
                        # they see it again.
                        manifest["approved_at"] = ""
                        manifest["status"] = DRAFT
                    else:
                        manifest["status"] = (
                            APPROVED if manifest.get("approved_at") else DRAFT
                        )
            manifest = self._save(manifest, "stage_written", stage=stage, bytes=len(body))
            manifest["warnings"] = warnings + (
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
        try:
            self.claim_agent_role(plan_id, role)
        except PlanError:
            if record:
                self._record_access(plan_id, role, stage, False)
            raise
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
                    "agent": self._ack_key(role),
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
                "behalf; approval is the user's alone, and --as-user does not "
                "lift that. Present the plan and stop"
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

    def set_stage_state(
        self,
        plan_id: str,
        stage: str,
        state: str,
        *,
        note: str = "",
        role: str = "",
        as_user: bool = False,
        workstream: str = "",
    ) -> dict:
        if state not in STAGE_STATES:
            raise PlanError(f"unknown stage state {state!r}")
        workstream = (workstream or os.environ.get("GROGU_WORKSTREAM", "")).strip()
        role = role or current_role()
        owner = STAGE_COMPLETERS.get(stage)
        # Marking the test plan complete is the tester's judgement to make. An
        # engineer who can make it can then finalize the plan and read the
        # sealed assertions, which turns the seal into a formality.
        if role and owner and role not in (owner, ARCHITECT):
            raise PlanError(
                f"the {role} may not change the {stage} stage state; that belongs "
                f"to the {owner}"
            )
        if as_user and current_role():
            raise PlanError(
                f"--as-user is for the user; this session is running as the "
                f"{current_role()}"
            )
        if role and not as_user:
            self.claim_agent_role(plan_id, role)
        if not role and stage in SEALED_STAGES and state == COMPLETE and not as_user:
            # Default-deny, because the check above was only ever as strong as
            # the caller's willingness to declare itself. An engineer that
            # simply never exported GROGU_ROLE could complete the test stage
            # and then finalize, which is the whole bypass the seal exists to
            # prevent. Saying who you are is cheap; the seal is not.
            raise PlanError(
                f"completing the sealed {stage} stage needs a role: export "
                f"GROGU_ROLE={STAGE_COMPLETERS.get(stage, TESTER)} or pass "
                f"--as-user if you are the user"
            )
        with self.locked():
            manifest = self.load(plan_id)
            if stage not in manifest.get("stages", []):
                raise PlanError(f"plan {plan_id} has no {stage} stage")
            if (
                manifest.get("review_required")
                and not manifest.get("approved_at")
                and not as_user
                # The design spec is part of what the user is being asked to
                # review, so it has to be finishable before approval. Holding
                # it dead-ended the designer: it could write the spec and then
                # not close its own stage, and the refusal pointed it at a
                # user-only command it had itself just unblocked.
                and stage != DESIGN
            ):
                # `plan gate` said this correctly and an engineer walked
                # straight past it, because nothing made it ask. A hold the
                # held party can decline to notice is not a hold.
                raise PlanError(
                    f"plan {plan_id} is waiting for the user to review it; "
                    "nothing moves until `grogu plan approve` "
                    "(autopilot does not waive review)"
                )
            if state == COMPLETE and not manifest.get("stage_written", {}).get(stage):
                # `plan status` printed "testing=complete (unwritten)" and let
                # it stand: a recorded claim that the tests passed against a
                # plan that was never written. The most likely way to get here
                # is a rejected write -- the design spec is refused for missing
                # sections, the role does not notice, and completes anyway --
                # so the failure mode is a green stage produced by an error
                # message nobody read.
                raise PlanError(
                    f"the {stage} stage has no body, so there is nothing to "
                    f"have completed. Write it first (`grogu plan write "
                    f"{plan_id} {stage} --file ...`); if a write was refused, "
                    "the refusal is the thing to fix"
                )
            if stage in SEALED_STAGES and state == COMPLETE:
                # The dual of the late-defect hole. Reopening testing when a
                # defect arrives late only covers defects filed *after* a pass;
                # a pass recorded while a defect was already open needed no
                # reopening, and the defect then auto-closed when the engineer
                # re-completed implementation, leaving a complete test stage and
                # no open defects. A verdict of "the tests pass" is not
                # available while something is known to be broken.
                open_defects = [
                    defect.get("id", "?")
                    for defect in manifest.get("defects", [])
                    if defect.get("status") == PENDING
                ]
                if open_defects:
                    raise PlanError(
                        f"cannot complete {stage} while defect(s) "
                        f"{', '.join(open_defects)} are open: a pass recorded "
                        "over a known failure is not a pass. Resolve or route "
                        "them, then run the tests again."
                    )
            streams = [stream["name"] for stream in manifest.get("workstreams", [])]
            if len(streams) > 1 and stage == IMPLEMENTATION:
                # Completion used to be plan-wide, so the first of four
                # engineers to finish opened the test gate for work the other
                # three had not started. Fanning out is the point of
                # workstreams; a completion that means "all of it" when it
                # meant "my file" is the one thing that makes fanning out
                # unsafe.
                if not workstream:
                    raise PlanError(
                        f"plan {plan_id} is split across {len(streams)} "
                        f"workstreams ({', '.join(streams)}), so say which one "
                        "you finished: --workstream <name>, or export "
                        "GROGU_WORKSTREAM"
                    )
                if workstream not in streams:
                    raise PlanError(
                        f"plan {plan_id} has no workstream {workstream!r} "
                        f"(declared: {', '.join(streams)})"
                    )
                done = manifest.setdefault("workstream_state", {})
                done[workstream] = state
                outstanding = [name for name in streams if done.get(name) != COMPLETE]
                if state == COMPLETE and outstanding:
                    manifest["stage_state"][stage] = PENDING
                    return self._save(
                        manifest,
                        "workstream_state",
                        stage=stage,
                        state=state,
                        workstream=workstream,
                        outstanding=", ".join(outstanding),
                    )
            manifest.setdefault("stage_state", {})[stage] = state
            resolved: list = []
            if state == COMPLETE:
                resolved = (
                    []
                    if manifest.get("escalated")
                    # While the architect is adjudicating, the defects are the
                    # evidence. Closing them because a stage was re-completed
                    # leaves the escalation with nothing to look at.
                    else self._close_defects_for(
                        manifest,
                        routes=_STAGE_DEFECT_ROUTES.get(stage, ()),
                        reason=f"{stage} was completed again after the fix",
                    )
                )
                if stage == TESTING:
                    # The loop converged: tests were run to completion rather
                    # than bouncing. Counting rounds past this point would
                    # escalate healthy work that simply had several bugs.
                    manifest["defect_rounds"] = 0
                    # But a green pass that only exists because the last one was
                    # invalidated is itself a bounce, one level up. Without this
                    # an endless "one more bug after green" could run forever:
                    # every cycle reaches green, so every cycle resets the count
                    # and the architect is never asked whether the plan is the
                    # problem.
                    if manifest.pop("retest_pending", False):
                        manifest["retest_cycles"] = (
                            manifest.get("retest_cycles", 0) + 1
                        )
                pending_now = [
                    other
                    for other in manifest.get("defects", [])
                    if other.get("status") == PENDING
                ]
                if len(pending_now) < max_pending_defects():
                    # The pile the architect was called in about has been dealt
                    # with, so the stall trigger re-arms.
                    manifest.pop("stall_held", None)
            return self._save(
                manifest,
                "stage_state",
                stage=stage,
                state=state,
                note=note,
                resolved_defects=", ".join(resolved) or None,
            )

    @staticmethod
    def _invalidate_verification(manifest: dict) -> list:
        """Reopen any stage whose "complete" is a claim about older code.

        Returns the stages reset, for the log. Testing and evaluation are the
        stages that assert something about a build rather than produce one, so
        they are the ones a new defect falsifies.
        """
        reopened = []
        state = manifest.setdefault("stage_state", {})
        for stage in (TESTING, EVALUATION):
            if stage in manifest.get("stages", []) and state.get(stage) == COMPLETE:
                state[stage] = PENDING
                reopened.append(stage)
        if reopened:
            manifest["verification_invalidated_at"] = now()
            # The next green pass is a retest rather than a first pass, and
            # counting those is what catches a plan that keeps producing one
            # more bug after every clean run.
            manifest["retest_pending"] = True
        return reopened

    @staticmethod
    def _close_defects_for(manifest: dict, *, routes: tuple, reason: str) -> list:
        """Resolve open defects on `routes`, returning the ids closed.

        A defect has to be able to close, or the first real test failure shuts
        the gate for good and the pipeline dies exactly when it is working. The
        fix landing is the signal, and the tester re-running is the check: a
        defect closed by an engineer who did not actually fix it comes straight
        back as a new one.
        """
        closed = []
        for defect in manifest.get("defects", []):
            if defect.get("status") == PENDING and defect.get("route") in routes:
                defect["status"] = RESOLVED
                defect["resolved_at"] = now()
                defect["resolved_by"] = actor()
                defect["resolution"] = reason
                defect["auto_resolved"] = True
                closed.append(defect.get("id", "?"))
        return closed

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
        relayed: bool = False,
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
        # Steering is the user's channel, but agents record notes on it too.
        # A reader that cannot tell whose note it is reading cannot weigh it,
        # which matters most for the designer.
        author = current_role() or "user"
        # The supervisor's whole job on this channel is carrying the user's
        # words into agents that cannot be interrupted from outside. A reader
        # weighing "whose taste is this" must be able to tell a relayed note
        # from the supervisor's own opinion, so relaying is explicit and the
        # default is the supervisor's own voice. Nothing here can stop a
        # supervisor claiming a relay it invented; what it does is put the
        # claim in the record, where the user reads it in the finished plan.
        relayed_by = ""
        if relayed:
            if author != SUPERVISOR:
                raise PlanError(
                    "only the supervisor relays steering; the user's own notes "
                    "are already the user's"
                )
            author, relayed_by = "user", SUPERVISOR
        with self.locked():
            if plan_id:
                manifest = self.load(plan_id)
                notes = manifest.setdefault("steering", [])
                note = {
                    "seq": len(notes) + 1,
                    "at": now(),
                    "actor": actor(),
                    "role": role,
                    "from": author,
                    "relayed_by": relayed_by,
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
                "from": author,
                "relayed_by": relayed_by,
                "text": text.strip(),
                "requires_replan": bool(requires_replan),
            }
            payload["notes"].append(note)
            self._write_json(self.steering_path, payload)
            return note

    def retract_steering(self, seq: int, *, plan_id: str = "") -> dict:
        """Take back a note, because append-only means noise only grows.

        An architect probing the harness left a note queued for the tester and
        found the only remedy was a second note contradicting the first --
        both permanent, both delivered. Retracting hides an undelivered note
        entirely; a delivered one is marked retracted and says so, because
        someone has already read it and pretending otherwise is worse.
        """
        with self.locked():
            if plan_id:
                manifest = self.load(plan_id)
                notes = manifest.get("steering", [])
                note = next((item for item in notes if item.get("seq") == seq), None)
                if note is None:
                    raise PlanError(f"plan {plan_id} has no steering note #{seq}")
                delivered = self._note_was_delivered(manifest, note)
                note["retracted"] = True
                note["retracted_at"] = now()
                self._save(manifest, "steering_retracted", seq=seq)
                return {"seq": seq, "delivered": delivered}
            payload = self._repo_steering()
            note = next(
                (item for item in payload["notes"] if item.get("seq") == seq), None
            )
            if note is None:
                raise PlanError(f"there is no repository steering note #{seq}")
            note["retracted"] = True
            note["retracted_at"] = now()
            self._write_json(self.steering_path, payload)
            return {"seq": seq, "delivered": False}

    def _note_was_delivered(self, manifest: dict, note: dict) -> bool:
        acked = manifest.get("steering_acked", {}) or {}
        return any(
            value >= note.get("seq", 0)
            for key, value in acked.items()
            if note.get("role") in ("all", key.split("@")[0])
        )

    def _feedback_acknowledged(self, manifest: dict, note: dict) -> bool:
        acked = manifest.get("steering_acked", {}) or {}
        seq = int(note.get("seq", 0) or 0)
        target_role = str(note.get("role", "all"))
        target_agent = str(note.get("target_agent", ""))
        if target_agent:
            return int(
                acked.get(f"{target_role}@{target_agent}", 0) or 0
            ) >= seq
        matching = [
            int(value or 0)
            for key, value in acked.items()
            if target_role == "all"
            or key == target_role
            or key.startswith(target_role + "@")
        ]
        return bool(matching) and max(matching) >= seq

    def steering(self, *, role: str = "all", plan_id: str = "", unread: bool = False) -> dict:
        """Steering visible to `role`, newest last, cheap enough to poll."""
        if role != "all" and role not in ROLES:
            raise PlanError(f"unknown role {role!r}")

        caller_agent = self._identified_agent()[0]

        def visible(notes: list, acked: int) -> list:
            selected = [
                note
                for note in notes
                # The architect owns the plan, so a correction aimed at the
                # engineer is still the architect's problem: it is how a plan
                # goes stale. Steering the plan's owner cannot see is the one
                # kind that silently invalidates everything downstream of it.
                if (role in ("all", ARCHITECT) or note.get("role") in ("all", role))
                and (
                    role in ("all", ARCHITECT)
                    or not note.get("target_agent")
                    or note.get("target_agent") == caller_agent
                )
                and not note.get("retracted")
                and not note.get("withdrawn")
            ]
            if unread:
                selected = [note for note in selected if note.get("seq", 0) > acked]
            return selected

        payload = self._repo_steering()
        repo_acked = self._acked_seq(payload["acked"], role) if role != "all" else 0
        result = {
            "role": role,
            "repository": visible(payload["notes"], repo_acked),
            "plan": [],
            "plan_id": plan_id,
        }
        if plan_id:
            manifest = self.load(plan_id)
            plan_acked = (
                self._acked_seq(manifest.get("steering_acked", {}), role)
                if role != "all"
                else 0
            )
            result["plan"] = visible(manifest.get("steering", []), plan_acked)
            folded = manifest.get("steering_folded_seq", 0)
            result["requires_replan"] = any(
                note.get("requires_replan") and note.get("seq", 0) > folded
                for note in result["plan"]
            )
        return result

    def audit_note(self, seq: int, plan_id: str = "") -> dict:
        """Who has actually read one specific note.

        The relay path asks an agent to take somebody else's word that a note
        was delivered and acked on its behalf. A designer that was told this
        had no way to check it -- `plan steering` cannot address a single note
        and `plan status` shows only an aggregate -- so it re-polled anyway,
        which is exactly the round trip the relay exists to save.
        """
        repository = self._repo_steering()
        sources = [("repository", repository["notes"], repository["acked"])]
        if plan_id:
            manifest = self.load(plan_id)
            sources.append(
                ("plan", manifest.get("steering", []), manifest.get("steering_acked", {}))
            )
        for source, notes, acked in reversed(sources):
            note = next((item for item in notes if item.get("seq") == seq), None)
            if note is None:
                continue
            target = note.get("role", "all")
            roles = set(ROLES) if target == "all" else {target}
            read, unread = [], []
            for key, value in acked.items():
                if "@" not in key or key.split("@", 1)[0] not in roles:
                    continue
                (read if int(value or 0) >= seq else unread).append(key)
            # An audit that lists every agent that ever touched this plan
            # reads like a scoping bug: an engineer checking one note saw two
            # agents from an hour-old probe listed as not having read it, had
            # no way to tell whether they were relevant, and trusted the
            # answer less than before it asked. Say when each was last seen.
            seen = (self.load(plan_id).get("agents_seen", {}) if plan_id else {})
            return {
                "seq": seq,
                "source": source,
                "role": target,
                "text": note.get("text", ""),
                "retracted": bool(note.get("retracted")),
                "read_by": sorted(read),
                "unread_by": sorted(unread),
                "last_seen": {key: seen.get(key, "") for key in read + unread},
            }
        raise PlanError(
            f"no steering note #{seq}"
            + (f" on {plan_id}" if plan_id else "")
            + "; `grogu plan steering --all` lists them with their numbers"
        )

    def _ack_key(self, role: str, agent: str = "") -> str:
        """Who, specifically, has read a steering note.

        Acking per role alone was wrong the moment two engineers could run at
        once: the first to poll marked the note read for the whole role, and
        its peers in other worktrees never saw the user's correction at all.
        Steering that silently reaches one of three agents is worse than
        steering that reaches none, because it looks delivered.

        Identity is the agent's working directory, since parallel workstreams
        each get their own worktree, overridable by `GROGU_AGENT` for layouts
        that share one. The failure mode of getting this wrong is a note shown
        twice, which is the right direction to fail in.
        """
        agent = (agent or os.environ.get("GROGU_AGENT", "")).strip()
        if not agent:
            # Fall back to the name this working directory was bound under
            # before falling back to the directory itself. Without it the same
            # agent answers to two names depending on whether the variable
            # happened to be exported in that particular shell.
            try:
                agent = (self.session_binding().get("agent", "") or "").strip()
            except (PlanError, OSError):
                agent = ""
        if not agent:
            try:
                agent = str(Path.cwd().resolve())
            except OSError:
                agent = "unknown"
        return f"{role}@{agent}"

    @staticmethod
    def _binding_time(entry: dict) -> dt.datetime:
        try:
            stamp = dt.datetime.fromisoformat(
                str(entry.get("at") or "").replace("Z", "+00:00")
            )
        except ValueError:
            return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt.timezone.utc)

    def _identified_agent(self, *, include_worktree: bool = False) -> tuple[str, str]:
        """Return the stable agent name and any role already bound to it."""
        explicit = os.environ.get("GROGU_AGENT", "").strip()
        if explicit:
            matching = [
                entry
                for entry in self.session_bindings().values()
                if isinstance(entry, dict)
                and (entry.get("agent", "") or "").strip() == explicit
            ]
            binding = max(matching, key=self._binding_time, default={})
            return explicit, (binding.get("role", "") or "").strip()

        binding = self.binding_covering()
        agent = (binding.get("agent", "") or "").strip()
        if agent:
            return agent, (binding.get("role", "") or "").strip()
        if include_worktree:
            try:
                return (
                    str(Path.cwd().resolve()),
                    (binding.get("role", "") or "").strip(),
                )
            except OSError:
                return "", ""
        return "", ""

    def claim_agent_role(
        self,
        plan_id: str,
        role: str,
        *,
        identify: bool = False,
        record_presence: bool = True,
    ) -> str:
        """Bind an identified agent to one pipeline role.

        `GROGU_AGENT` is the explicit identity. A persisted session binding
        recovers it when a later command runs in a fresh shell, and CLI entry
        points may opt into the working-directory fallback used by steering.
        Anonymous library callers retain the old trust-on-assert behavior.
        """
        role = (role or "").strip().lower()
        if role not in ROLES:
            raise PlanError(f"unknown role {role!r}; expected one of {', '.join(ROLES)}")
        declared = current_role()
        if declared and declared != role:
            raise PlanError(
                f"this session is the {declared}; it cannot act as the {role}. "
                f"If the {role} should do this, spawn one with a distinct "
                "GROGU_AGENT"
            )

        agent, bound_role = self._identified_agent(include_worktree=identify)
        if not agent:
            return ""

        with self.locked():
            manifest = self.load(plan_id) if plan_id else {}
            roles = {
                key.split("@", 1)[0]
                for key in manifest.get("agents_seen", {})
                if "@" in key and key.split("@", 1)[1] == agent
            }
            if bound_role in ROLES:
                roles.add(bound_role)
            conflicts = sorted(existing for existing in roles if existing != role)
            if conflicts:
                previous = ", ".join(conflicts)
                raise PlanError(
                    f"this agent is already bound to the {previous} role"
                    + (f" on {plan_id}" if plan_id else "")
                    + f"; it cannot act as the {role}. Spawn a distinct {role} "
                    "agent with its own GROGU_AGENT"
                )

            if plan_id and record_presence:
                seen = manifest.setdefault("agents_seen", {})
                key = f"{role}@{agent}"
                stamp = now()[:16]
                if seen.get(key) != stamp:
                    seen[key] = stamp
                    self._write_json(self.manifest_path(plan_id), manifest)
            self._remember_session_role(role, plan_id, agent)
        return agent

    def _acked_seq(self, acked: dict, role: str) -> int:
        """This agent's high-water mark, falling back to the role-wide one.

        The fallback keeps acks written before per-agent keys existed from
        replaying every old note at once on upgrade.
        """
        return int(acked.get(self._ack_key(role), acked.get(role, 0)) or 0)

    def ack_steering(self, *, role: str, plan_id: str = "", agent: str = "") -> dict:
        """Mark steering read, optionally on another agent's behalf.

        The relay path needs the second form. When the session that spawned an
        engineer pushes a note into it with `write_agent`, the note has arrived
        — but the harness has no way to know that, so the next `grogu` command
        that engineer runs would hand it the same text a second time. Acking
        for the agent you just relayed to is what keeps one delivery to one
        context.
        """
        if role not in ROLES:
            raise PlanError(f"unknown role {role!r}")
        key = self._ack_key(role, agent)
        with self.locked():
            payload = self._repo_steering()
            repo_high = max(
                [note.get("seq", 0) for note in payload["notes"]] or [0]
            )
            payload["acked"][key] = repo_high
            self._write_json(self.steering_path, payload)
            plan_high = 0
            if plan_id:
                manifest = self.load(plan_id)
                plan_high = max(
                    [note.get("seq", 0) for note in manifest.get("steering", [])] or [0]
                )
                manifest.setdefault("steering_acked", {})[key] = plan_high
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
        self.claim_agent_role(plan_id, raised_by)
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
        self.claim_agent_role(plan_id, role)
        if role != ARCHITECT:
            raise PlanError(
                f"role {role!r} may not resolve amendments; only the architect owns the plan"
            )
        if outcome not in AMENDMENT_OUTCOMES:
            raise PlanError(
                f"unknown outcome {outcome!r}; expected one of {', '.join(AMENDMENT_OUTCOMES)}"
            )
        if not reason.strip():
            raise PlanError("an amendment resolution needs a reason")
        manifest = self.load(plan_id)
        for amendment in manifest.get("amendments", []):
            if amendment.get("id") == amendment_id:
                break
        else:
            # Asking for --verified before saying the amendment does not exist
            # sends the architect off to verify a claim nobody made.
            raise PlanError(f"plan {plan_id} has no amendment {amendment_id!r}")
        if not verified:
            raise PlanError(
                "pass --verified: the architect must confirm the claim against the "
                "code itself. Taking another agent's word for it is how a wrong "
                "plan becomes an agreed plan."
            )
        self._require_having_read(manifest, amendment)
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
            # The defect that raised this amendment has now had its answer,
            # whichever way it went. Leaving it open blocks the implement gate
            # on a question the architect has already settled.
            for defect in manifest.get("defects", []):
                if (
                    defect.get("amendment") == amendment_id
                    and defect.get("status") == PENDING
                ):
                    defect["status"] = RESOLVED
                    defect["resolved_at"] = now()
                    defect["resolved_by"] = actor()
                    defect["resolution"] = f"amendment {amendment_id} {outcome}: {reason.strip()}"
                    defect["auto_resolved"] = True
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
                    manifest["retest_cycles"] = 0
                    manifest["escalated"] = False
                    if any(
                        other.get("status") == PENDING
                        for other in manifest.get("defects", [])
                    ):
                        # The architect has seen this pile. Hold the stall
                        # trigger until it has been cleared, so the next filing
                        # does not immediately call it back for the same thing.
                        manifest["stall_held"] = True
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
        self.claim_agent_role(plan_id, raised_by)
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
            # A defect found after the tests were marked complete makes that
            # completion stale: it describes a build that no longer exists once
            # the fix lands. Leaving it standing was a hole — the defect
            # auto-closed when the engineer re-completed implementation, and
            # finalize then saw no open defects and a complete test stage, so
            # the plan shipped without anything having been re-run. Verification
            # is a claim about a specific state of the code, and it expires when
            # that state changes.
            invalidated = self._invalidate_verification(manifest)
            if route in (ROUTE_IMPLEMENTATION, ROUTE_TEST, ROUTE_DESIGN):
                # A round is a *bounce*, not a bug. Counting filings meant a
                # perfectly healthy first test pass that found three real
                # problems escalated to the architect before the engineer had
                # been given a chance to fix any of them. What actually signals
                # non-convergence is a new failure arriving on a route that was
                # already fixed once, so a round opens only when the previous
                # wave on that route has been resolved and this is the first
                # defect of the next one.
                siblings = [
                    other
                    for other in defects[:-1]
                    if other.get("route") == route
                ]
                new_wave = bool(siblings) and not any(
                    other.get("status") == PENDING for other in siblings
                )
                rounds = manifest.get("defect_rounds", 0) + (1 if new_wave else 0)
                manifest["defect_rounds"] = rounds
                cap = manifest.get("max_defect_rounds", DEFAULT_MAX_DEFECT_ROUNDS)
                # An engineer and a tester trading fixes past this point are no
                # longer converging. That is a question about the plan, and the
                # architect owns the plan — so it goes up one level, not out to
                # the user.
                # Counting bounces alone has a blind spot the old rule did
                # not: an engineer who never fixes anything never produces a
                # bounce, so a tester could file failure after failure on the
                # same route and the round count would sit at zero forever.
                # That is a stall rather than a loop, but it needs the same
                # person — nothing else in the pipeline will notice a plan
                # that has simply stopped moving.
                # Counted across every route, not just this one. Per route it
                # was still evadable: five open implementation failures and
                # five open test failures is a plan that has plainly stopped,
                # and neither route reaches the cap alone.
                unresolved = [
                    other for other in defects if other.get("status") == PENDING
                ]
                stalled = (
                    len(unresolved) >= max_pending_defects()
                    # Cleared once the pile drops back under the cap. Without
                    # it, a tester filing one more before the engineer sweeps
                    # the previous six re-escalates immediately, and the
                    # architect is pulled back in for a pile it has already
                    # seen.
                    and not manifest.get("stall_held")
                )
                churning = manifest.get("retest_cycles", 0) >= cap
                escalate = (
                    rounds >= cap or stalled or churning
                ) and not manifest.get("escalated")
                defect["round"] = rounds
                defect["stalled"] = stalled
                defect["churning"] = churning and not stalled and rounds < cap
            self._save(
                manifest,
                "defect_raised",
                defect=defect["id"],
                route=route,
                invalidated=", ".join(invalidated) or None,
            )

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
                    (
                        f"{len(unresolved)} failures are open with none "
                        f"resolved; the loop has stalled rather than bounced. "
                        f"Latest: {report.strip()}"
                    )
                    if defect.get("stalled")
                    else (
                        f"The tests have gone green and come back "
                        f"{manifest.get('retest_cycles', 0)} times and this is "
                        f"another failure; the question is the plan, not the "
                        f"fix. Latest: {report.strip()}"
                    )
                    if defect.get("churning")
                    else f"Engineer and tester have exchanged {defect['round']} rounds "
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
        model: Optional[str] = None,
        review: Optional[str] = None,
        brief: Optional[str] = None,
        replace: bool = False,
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
            existing = next(
                (stream for stream in workstreams if stream["name"] == name), None
            )
            if existing is not None:
                if not replace:
                    raise PlanError(
                        f"plan {plan_id} already has a workstream {name!r}; pass "
                        "--replace to correct it. Getting a path wrong used to "
                        "mean superseding the whole plan"
                    )
                if manifest.get("workstream_state", {}).get(name) == COMPLETE:
                    raise PlanError(
                        f"workstream {name!r} is already complete; redefining it "
                        "would silently reopen finished work"
                    )
                workstreams.remove(existing)
            # --replace was built as delete-then-create, so correcting a path
            # glob silently blanked the model, the review requirement, the brief
            # and the reviews already recorded against the workstream. The
            # architect narrowed one glob and lost the "this one wants a second
            # pair of eyes" instruction without being told. An omitted flag now
            # means "leave it alone"; `--model ""` still clears it.
            carried = existing if (existing is not None and replace) else {}
            unknown = [
                dependency
                for dependency in (depends_on or [])
                if not any(stream["name"] == dependency for stream in workstreams)
            ]
            if unknown:
                raise PlanError(f"unknown workstream dependency: {', '.join(unknown)}")
            if review is not None and review and review not in REVIEW_KINDS:
                raise PlanError(
                    f"unknown review {review!r}; expected one of "
                    + ", ".join(REVIEW_KINDS)
                )
            workstream = {
                "name": name,
                "paths": list(paths),
                "depends_on": list(depends_on or []),
                "model": (carried.get("model", "") if model is None else model).strip(),
                "review": carried.get("review", "") if review is None else review,
                "brief": (carried.get("brief", "") if brief is None else brief).strip(),
                "reviews": list(carried.get("reviews", [])),
            }
            workstreams.append(workstream)
            self._save(manifest, "workstream_added", workstream=name)
            return workstream

    def drop_workstream(self, plan_id: str, name: str) -> dict:
        """Withdraw a workstream the plan no longer wants.

        A replan that merges two workstreams into one left the third declared
        forever: `--replace` could redefine it and nothing could remove it, so
        the only exit was superseding the whole plan. The wave calculation kept
        scheduling work nobody intended to do.
        """
        with self.locked():
            manifest = self.load(plan_id)
            workstreams = manifest.setdefault("workstreams", [])
            existing = next(
                (stream for stream in workstreams if stream["name"] == name), None
            )
            if existing is None:
                known = ", ".join(stream["name"] for stream in workstreams) or "none"
                raise PlanError(
                    f"plan {plan_id} has no workstream {name!r}; it has: {known}"
                )
            if manifest.get("workstream_state", {}).get(name) == COMPLETE:
                raise PlanError(
                    f"workstream {name!r} is complete; dropping it would erase "
                    "work that was already done and reviewed"
                )
            dependents = [
                stream["name"]
                for stream in workstreams
                if name in stream.get("depends_on", [])
            ]
            if dependents:
                raise PlanError(
                    f"{', '.join(dependents)} depend(s) on {name!r}; drop or "
                    "redeclare those first, or the waves lose their ordering"
                )
            workstreams.remove(existing)
            manifest.get("workstream_state", {}).pop(name, None)
            self._save(manifest, "workstream_dropped", workstream=name)
            return existing

    # -- workstream worktrees ------------------------------------------

    def workstream_worktree(
        self, plan_id: str, name: str, *, base: Optional[str] = None
    ) -> dict:
        """Get or create the dedicated git worktree for one declared workstream.

        This is the harness-level fix for friction #40: declared parallel
        workstreams used to run against one shared checkout by convention,
        and a workstream's scratch files could land anywhere in it despite
        the declared path globs being disjoint -- the disjoint-paths
        guarantee had nothing actually enforcing it. Every caller asking
        about the same workstream gets back the same worktree, because the
        path and branch are a deterministic function of the plan id and the
        workstream name (`grogu_worktrees.workstream_worktree_path`), never a
        freshly chosen name -- so the engineer that owns the workstream and
        the supervisor that spawned it agree on where it lives without a
        message ever passing between them.
        """
        manifest = self.load(plan_id)
        workstreams = manifest.get("workstreams", [])
        if not any(stream["name"] == name for stream in workstreams):
            known = ", ".join(stream["name"] for stream in workstreams) or "none"
            raise PlanError(
                f"plan {plan_id} has no workstream {name!r}; it has: {known}"
            )
        try:
            result = grogu_worktrees.ensure_workstream_worktree(
                self.root, plan_id, name, base=base or None
            )
        except grogu_worktrees.WorktreeError as error:
            raise PlanError(str(error)) from error
        return {
            "name": name,
            "path": str(result.path),
            "branch": result.branch,
            "created": result.created,
        }

    def list_workstream_worktrees(self, plan_id: str) -> list:
        """Every dedicated workstream worktree already created for this plan.

        Read-only: it reports what `workstream_worktree` already made, and
        never creates one itself, so listing a plan's state is never the
        thing that scatters a new worktree onto disk.

        `workstream_branch` slugifies a name (spaces and other unsafe
        characters become `-`) to build a Git ref, which is lossy: recovering
        "api gateway" from its branch by splitting on `/` alone would give
        back "api-gateway", not the raw declared name, so it would never
        match a declared workstream with a multi-word name and would always
        be reported as no longer declared. Matching each worktree's branch
        against the branch that every *currently* declared name would
        produce (`workstream_branch(plan_id, declared_name)`) recovers the
        exact raw name instead; the lossy split is only a fallback for a
        worktree whose branch matches no currently declared workstream at
        all (dropped, or never declared), where there is no raw name left to
        recover.
        """
        declared_names = [
            stream["name"] for stream in self.load(plan_id).get("workstreams", [])
        ]
        branch_to_name = {
            grogu_worktrees.workstream_branch(plan_id, declared_name): declared_name
            for declared_name in declared_names
        }
        entries = []
        for entry in grogu_worktrees.list_workstream_worktrees(self.root, plan_id):
            declared_name = branch_to_name.get(entry.branch)
            name = declared_name if declared_name is not None else entry.branch.rsplit("/", 1)[-1]
            entries.append(
                {
                    "name": name,
                    "path": str(entry.path),
                    "branch": entry.branch,
                    "declared": declared_name is not None,
                }
            )
        return entries

    def remove_workstream_worktree(
        self,
        plan_id: str,
        name: str,
        *,
        force: bool = False,
        delete_branch: bool = False,
        base: Optional[str] = None,
    ) -> dict:
        """Clean up one workstream's dedicated worktree once it is no longer needed.

        Deliberately does not require the workstream still be declared: a
        finished workstream is often dropped from the plan (`drop_workstream`)
        before anyone remembers to clean up the worktree it left behind, and
        cleanup has to reach it anyway. Removing a workstream nothing ever
        created for is not an error either -- it is reported as nothing to do.
        """
        try:
            removed = grogu_worktrees.remove_workstream_worktree(
                self.root,
                plan_id,
                name,
                force=force,
                delete_branch=delete_branch,
                base=base or None,
            )
        except grogu_worktrees.WorktreeError as error:
            raise PlanError(str(error)) from error
        if removed is None:
            return {
                "name": name,
                "removed": "",
                "branch_deleted": False,
                "branch_kept_reason": "",
            }
        return {
            "name": name,
            "removed": str(removed.path),
            "branch_deleted": removed.branch_deleted,
            "branch_kept_reason": removed.branch_kept_reason,
        }

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
            required = stream.get("review") or ""
            if verdict == PASS and required and not kind.strip():
                # Defaulting the kind to whatever was required meant a
                # security review was a label you got for free: record a bare
                # pass and the gate saw the kind it asked for. The reviewer has
                # to name what they actually did.
                raise PlanError(
                    f"this workstream requires a {required}; say which review "
                    f"you ran with --kind, because defaulting it to the one "
                    f"that was required is the same as not checking"
                )
            record = {
                "at": now(),
                "kind": kind.strip() or required or REVIEW_RUBBER_DUCK,
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
        self.claim_agent_role(plan_id, role)
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
        if verdict == PASS:
            # An unchecked path is an assurance with a filename attached. The
            # whole point of this gate is that somebody looked, so the artifact
            # they looked at has to exist and have something in it.
            missing = []
            for item in evidence:
                candidate = Path(item.strip()).expanduser()
                if not candidate.is_absolute():
                    candidate = self.root / candidate
                try:
                    if not candidate.is_file() or candidate.stat().st_size == 0:
                        missing.append(item)
                except OSError:
                    missing.append(item)
            if missing:
                raise PlanError(
                    "design evidence not found or empty: "
                    + ", ".join(missing)
                    + ". Capture the interface first (the `browser-validate` "
                    "skill for a visual surface, redirected output for a "
                    "terminal one) and point at the file you actually produced."
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
                "commit": working_head(),
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

        pending_feedback = [
            note
            for note in manifest.get("steering", [])
            if note.get("binding_feedback")
            and not note.get("withdrawn")
            and stage_gate in note.get("gates", [])
            and not self._feedback_acknowledged(manifest, note)
        ]
        caller_role = current_role()
        caller_agent = self._identified_agent()[0]
        for note in pending_feedback:
            can_read_feedback = (
                not caller_role
                or caller_role == ARCHITECT
                or (
                    note.get("role") in {"all", caller_role}
                    and (
                        not note.get("target_agent")
                        or note.get("target_agent") == caller_agent
                    )
                )
            )
            summary = (
                str(note.get("text", "")).replace("\n", " ")
                if can_read_feedback
                else "feedback addressed to another role"
            )
            if len(summary) > 120:
                summary = summary[:117] + "..."
            blockers.append(
                "awaiting binding feedback acknowledgement: "
                f"{note.get('feedback_id', 'feedback')} {summary!r}"
            )

        if stage_gate == GATE_TEST:
            checks = [
                item
                for item in manifest.get("attachments", [])
                if item.get("verifier")
            ]
            verification = manifest.get("verification") or {}
            if checks and not verification:
                blockers.append(
                    "a verifier is attached to this plan and has never been "
                    "run: `grogu plan verify` (an artifact nobody runs is a "
                    "comment)"
                )
            elif checks and not verification.get("passed"):
                failed = [
                    item["name"]
                    for item in verification.get("results", [])
                    if not item.get("passed")
                ]
                blockers.append(
                    f"attached verifier(s) failed: {', '.join(failed)}"
                )
            elif checks and verification.get("commit") != head_commit(self.root):
                blockers.append(
                    "the attached verifier last passed at "
                    f"{str(verification.get('commit'))[:8]} and the tree has "
                    "moved since; `grogu plan verify` again"
                )

        if stage_gate == GATE_IMPLEMENT:
            # `workstreams --check` reported overlaps and then nothing acted on
            # them, so two engineers could be sent at the same file with a
            # warning nobody had to read. The conflict is exactly the condition
            # under which starting is unsafe.
            conflicts = self.workstream_conflicts(plan_id)
            for conflict in conflicts:
                blockers.append(
                    "workstreams {} both claim {}; they cannot run at the same "
                    "time. Narrow the paths or add --depends-on".format(
                        " and ".join(conflict["workstreams"]),
                        " / ".join(conflict["paths"]),
                    )
                )

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
                f"{stream['name']} ({stream['review']})"
                for stream in manifest.get("workstreams", [])
                if stream.get("review")
                and not any(
                    review.get("verdict") == PASS
                    # The kind has to match. A rubber-duck pass does not
                    # discharge a required security review; accepting any pass
                    # turns a specific instruction into a formality.
                    and review.get("kind") == stream["review"]
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
                    current = working_head()
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
        # created before it was recorded must still answer for it. The
        # comparison is against the later of "when this plan was written" and
        # "when the architect last folded steering in" — a note the architect
        # has already answered must stop blocking, or one repository-wide note
        # freezes every plan in the repository forever.
        watermark = max(
            manifest.get("created_at", ""), manifest.get("steering_folded_at", "")
        )
        unread = self.steering(role="all", plan_id=plan_id, unread=False)
        binding_repo = [
            note
            for note in unread.get("repository", [])
            if note.get("requires_replan") and note.get("at", "") > watermark
        ]
        if binding_repo:
            # Quote them. A banner is shown once, and this is the moment the
            # note actually bites, so an agent that has lost it from context
            # gets the text back exactly when it needs it rather than a count
            # and an instruction to go looking.
            quoted = "; ".join(note["text"] for note in binding_repo[-3:])
            blockers.append(
                f"{len(binding_repo)} binding repository steering note(s) postdate "
                f"this plan; the architect must fold them in and re-approve: {quoted}"
            )
        if unread.get("requires_replan") and status != APPROVED:
            binding_plan = "; ".join(
                note["text"]
                for note in unread.get("plan", [])
                if note.get("requires_replan")
            )
            blockers.append(
                "binding steering has not been folded into the plan"
                + (f": {binding_plan}" if binding_plan else "")
            )

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
            "stage_written": manifest.get("stage_written", {}),
            "declined_stages": manifest.get("declined_stages", {}),
            "rounds": f"{manifest.get('rounds', 0)}/{manifest.get('max_rounds', DEFAULT_MAX_ROUNDS)}",
            "defect_rounds": f"{manifest.get('defect_rounds', 0)}/{manifest.get('max_defect_rounds', DEFAULT_MAX_DEFECT_ROUNDS)}",
            "escalated": manifest.get("escalated", False),
            "workstreams": [
                {
                    "name": stream["name"],
                    "paths": stream["paths"],
                    "depends_on": stream.get("depends_on", []),
                    "brief": stream.get("brief", ""),
                    "model": stream.get("model", ""),
                    "review": stream.get("review", ""),
                    "state": manifest.get("workstream_state", {}).get(
                        stream["name"], PENDING
                    ),
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
            "steering_pending": self._steering_pending(manifest),
            "steering_undelivered": self._steering_undelivered(manifest),
        }

    def note_agent_presence(self, plan_id: str, role: str) -> bool:
        """Record that an agent of this role is alive on this plan."""
        try:
            self.claim_agent_role(plan_id, role, identify=True)
        except (PlanError, OSError):
            return False  # presence is a convenience; it must never fail a command
        return True

    def _steering_undelivered(self, manifest: dict) -> list:
        """Notes no agent of the target role has read yet.

        `steering_pending` answers "do I have unread notes", which is the
        question an agent has. The user has the opposite question -- "did it
        reach them" -- and reading the per-caller count to answer it says yes
        as soon as the user's own shell has looked at the note, which is the
        most misleading possible answer.
        """
        repository = self._repo_steering()
        undelivered = []
        for source, notes, acked in (
            ("repository", repository["notes"], repository["acked"]),
            ("plan", manifest.get("steering", []), manifest.get("steering_acked", {})),
        ):
            agent_keys = {key: int(seq or 0) for key, seq in acked.items() if "@" in key}
            for key in manifest.get("agents_seen", {}):
                agent_keys.setdefault(key, 0)
            # Only roles that have actually shown up can be behind on
            # anything. Reporting a note as unread by a designer who was
            # never spawned would make the line noise, and a line that is
            # always there is a line nobody reads.
            present = {key.split("@", 1)[0] for key in agent_keys}
            for note in notes:
                if note.get("retracted"):
                    # Retracting stopped delivery but left the text quoted in
                    # `plan status` with a counter that could never reach zero,
                    # which is the opposite of taking a note back.
                    continue
                target = note.get("role", "all")
                roles = [role for role in (ROLES if target == "all" else (target,))]
                seq = note.get("seq", 0)
                unread = sorted(
                    key
                    for key, value in agent_keys.items()
                    if key.split("@", 1)[0] in roles and value < seq
                )
                if not present:
                    # Nothing has run yet, so we know nothing about who is
                    # behind beyond who the note was aimed at.
                    unread = [target]
                if unread:
                    undelivered.append(
                        {
                            "source": source,
                            "seq": seq,
                            "role": target,
                            "unread_by": sorted(unread),
                            "text": note.get("text", ""),
                        }
                    )
        return undelivered

    def _steering_pending(self, manifest: dict) -> dict:
        """Unread steering per role, from two file reads rather than twenty.

        `summary()` is the orientation call every agent makes, often more than
        once, so doing it by calling `steering()` twice per role re-read the
        same two files ten times over for a handful of integers.
        """
        repository = self._repo_steering()
        repo_notes = repository["notes"]
        repo_acked = repository["acked"]
        plan_notes = manifest.get("steering", [])
        plan_acked = manifest.get("steering_acked", {})

        def unread(notes: list, role: str, acked: int) -> int:
            notes = [note for note in notes if not note.get("retracted")]
            return sum(
                1
                for note in notes
                if note.get("role") in ("all", role) and note.get("seq", 0) > acked
            )

        return {
            role: unread(repo_notes, role, self._acked_seq(repo_acked, role))
            + unread(plan_notes, role, self._acked_seq(plan_acked, role))
            for role in ROLES
        }

    def finalize(
        self,
        plan_id: str,
        *,
        note: str = "",
        force: bool = False,
        role: str = "",
        as_user: bool = False,
    ) -> dict:
        """Unseal every stage so the finished plan ships in the pull request.

        Sealing exists to keep the engineer from writing to the test while the
        work is in flight. Once the work is done that reason is gone, and the
        reviewer wants all three plans in plain Markdown next to the diff they
        justify.

        Only the architect or the user may do it. Finalizing is the one
        operation that turns the sealed stages into plaintext on disk, so an
        engineer who can call it can read the assertions it was meant not to
        see — which would make the seal a formality rather than a boundary.

        An undeclared caller is refused rather than assumed to be the user:
        otherwise the role check is opt-in, and the one role it exists to stop
        is the one with a reason to opt out.
        """
        role = role or current_role()
        if role and role != ARCHITECT:
            raise PlanError(
                f"the {role} may not finalize a plan: finalizing unseals the "
                "testing and evaluation stages. Ask the architect."
            )
        if as_user and current_role():
            raise PlanError(
                f"--as-user is for the user; this session is running as the "
                f"{current_role()}"
            )
        if role and not as_user:
            self.claim_agent_role(plan_id, role)
        if not role and not as_user:
            raise PlanError(
                "finalizing unseals the testing and evaluation stages, so say "
                "who is asking: export GROGU_ROLE=architect, or pass --as-user "
                "if you are the user"
            )
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
            private_taste = _private_design_statements()
            for stage in manifest.get("stages", []):
                sealed_path = self.plan_dir(plan_id) / f"{stage}.sealed"
                plain = self.plan_dir(plan_id) / f"{stage}.md"
                if sealed_path.exists():
                    body = unseal(sealed_path.read_text(encoding="utf8"))
                elif plain.exists():
                    body = plain.read_text(encoding="utf8")
                else:
                    continue
                # Finalizing is the moment a plan stops being a local working
                # file and becomes pull request content. Anything pasted into
                # it along the way — a token from a failing run, a customer
                # address from a bug report — publishes here.
                #
                # This used to scan only the sealed stages, because the loop
                # skipped anything without a `.sealed` file. Only testing and
                # evaluation are sealed, so design and implementation — the two
                # a human pastes context into most — shipped unread.
                leaks = grogu_privacy.blocking(
                    grogu_privacy.scan(body, path=f"{stage}"),
                    destination=grogu_privacy.PUBLISHED,
                )
                if leaks:
                    raise PlanError(
                        f"refusing to finalize {plan_id}: the {stage} plan "
                        "contains data that must not be published.\n"
                        + grogu_privacy.report(leaks)
                        + "\nEdit the stage, then finalize again."
                    )
                quoted = [line for line in private_taste if line in body]
                if quoted:
                    raise PlanError(
                        f"refusing to finalize {plan_id}: the {stage} plan "
                        "quotes the user's own design principles verbatim, and "
                        "a finalized plan is committed in plaintext:\n  "
                        + "\n  ".join(f"{line[:70]}…" for line in quoted)
                        + "\nState the decision the principle produced, not the "
                        "principle. Then finalize again."
                    )
                if sealed_path.exists():
                    plain.write_text(body, encoding="utf8")
                    sealed_path.unlink()
                    emitted.append(str(plain.relative_to(self.root)))
            record = self._plan_record(manifest)
            leaks = grogu_privacy.blocking(
                grogu_privacy.scan(record, path="record"),
                destination=grogu_privacy.PUBLISHED,
            )
            if leaks:
                raise PlanError(
                    f"refusing to finalize {plan_id}: the plan record contains "
                    "data that must not be published.\n"
                    + grogu_privacy.report(leaks)
                )
            record_path = self.plan_dir(plan_id) / "record.md"
            record_path.write_text(record, encoding="utf8")
            manifest["status"] = COMPLETE
            manifest["finalized_at"] = now()
            manifest["sealed"] = False
            manifest["finalized_incomplete"] = blockers if force else []
            self._save(manifest, "finalized", note=note, forced=bool(blockers and force))
            # Everything readable in the plan directory ships, not only the
            # stages this call happened to unseal: implementation.md was
            # never sealed and is the one a reviewer reads first.
            shippable = sorted(
                str(path.relative_to(self.root))
                for path in self.plan_dir(plan_id).glob("*.md")
            ) + sorted(
                str(path.relative_to(self.root))
                for path in (self.plan_dir(plan_id) / "attachments").glob("*")
                if path.is_file()
            )
            staged = self._stage_for_review(shippable)
            return {
                "plan": plan_id,
                "emitted": emitted,
                "staged": staged,
                "status": COMPLETE,
                "shipped_incomplete": blockers if force else [],
            }

    def _require_having_read(self, manifest: dict, amendment: dict) -> None:
        """`--verified` was an honour system, and it did not survive contact.

        An architect probing this typed `--verified --reason "I did not read
        this amendment"` and the harness accepted it, from an agent that had
        read nothing at all. The manifest already logs every stage read with
        the agent that made it, so the claim is checkable rather than merely
        asserted: the architect must have opened the stage the amendment is
        about, after the amendment was raised.
        """
        if amendment.get("kind") != KIND_AMENDMENT:
            # An escalation is the harness reporting that a loop ran out of
            # room. There is no claim about the plan's text to check.
            return
        stage = amendment.get("stage") or IMPLEMENTATION
        raised = str(amendment.get("at", ""))
        agent = self._ack_key(ARCHITECT)
        for entry in manifest.get("access_log", []):
            if (
                entry.get("allowed")
                and entry.get("stage") == stage
                and entry.get("agent") == agent
                and str(entry.get("at", "")) >= raised
            ):
                return
        raise PlanError(
            f"nothing records you reading the {stage} stage since "
            f"{amendment.get('id')} was raised, so --verified is an assertion "
            f"rather than a check. Run `grogu plan show {manifest.get('id')} "
            f"--stage {stage}` and read it."
        )

    def verifiers(self, plan_id: str) -> list:
        return [
            item for item in self.attachments(plan_id) if item.get("verifier")
        ]

    def run_verifiers(self, plan_id: str, *, role: str = "") -> dict:
        """Run the checks a role attached, and record what they said.

        An architect pointed out that the harness carried the designer's
        verification script -- the one that re-derives every example in the
        spec from the spec's own rules -- and never ran it, never asked anyone
        whether it still passed, and opened the test gate regardless. An
        artifact nobody runs is a comment.
        """
        plan_id = self.resolve(plan_id)
        acting_role = role or current_role()
        if acting_role in ROLES:
            self.claim_agent_role(plan_id, acting_role)
        checks = self.verifiers(plan_id)
        if not checks:
            raise PlanError(
                f"plan {plan_id} has no verifiers; attach one with "
                "`grogu plan attach <id> --file <path> --verifier`"
            )
        directory = self.plan_dir(plan_id) / "attachments"
        results = []
        for check in checks:
            path = directory / check["name"]
            completed = subprocess.run(
                [sys.executable, str(path)],
                cwd=self.root,
                capture_output=True,
                text=True,
            )
            results.append(
                {
                    "name": check["name"],
                    "passed": completed.returncode == 0,
                    "code": completed.returncode,
                    "output": (completed.stdout + completed.stderr)[-2000:],
                }
            )
        with self.locked():
            manifest = self.load(plan_id)
            manifest["verification"] = {
                "at": now(),
                "by": role or current_role() or actor(),
                "commit": head_commit(self.root),
                "results": results,
                "passed": all(item["passed"] for item in results),
            }
            self._save(
                manifest,
                "verifiers_run",
                note=f"{sum(1 for r in results if r['passed'])}/{len(results)} passed",
            )
        return manifest["verification"]

    def _keep_revision(
        self,
        plan_id: str,
        stage: str,
        previous: str,
        manifest: dict,
        plain_bytes: Optional[int] = None,
    ) -> int:
        """Park the text a write is about to replace, and say which slot."""
        directory = self.plan_dir(plan_id) / "revisions"
        directory.mkdir(parents=True, exist_ok=True)
        history = manifest.setdefault("revisions", [])
        number = len([item for item in history if item.get("stage") == stage]) + 1
        name = f"{stage}.{number}.txt"
        (directory / name).write_text(previous, encoding="utf8")
        history.append(
            {
                "stage": stage,
                "revision": number,
                "file": name,
                "bytes": len(previous) if plain_bytes is None else plain_bytes,
                "at": now(),
                "by": actor(),
            }
        )
        return number

    def revisions(self, plan_id: str, stage: str = "") -> list:
        history = self.load(self.resolve(plan_id)).get("revisions", [])
        return [item for item in history if not stage or item.get("stage") == stage]

    def revision_body(self, plan_id: str, stage: str, revision: int) -> str:
        plan_id = self.resolve(plan_id)
        for item in self.revisions(plan_id, stage):
            if int(item.get("revision", 0)) == int(revision):
                path = self.plan_dir(plan_id) / "revisions" / item["file"]
                text = path.read_text(encoding="utf8")
                return unseal(text) if SEAL_HEADER in text.split("\n", 1)[0] else text
        raise PlanError(f"{stage} has no revision {revision}")

    def attach(
        self,
        plan_id: str,
        name: str,
        body: str,
        *,
        stage: str = "",
        role: str = "",
        note: str = "",
        verifier: bool = False,
    ) -> dict:
        """Carry an artifact that is not prose alongside the plan.

        The first designer wrote a script that re-derived every fenced block
        in its spec from the spec's own stated rules, and swept the width
        invariant across every terminal size. That script found a real
        contradiction between the spec's prose and its examples. It then had
        nowhere to go: `write` takes one body, so the proof died in a scratch
        directory and the tester's choice was to rebuild it or to assert the
        examples without ever checking they were mutually derivable.

        Attachments are checked against the published-destination rules on the
        way in, because unlike a plan body they are usually a file lifted
        whole out of a working directory.
        """
        plan_id = self.resolve(plan_id)
        acting_role = role or current_role()
        if acting_role in ROLES:
            self.claim_agent_role(plan_id, acting_role)
        clean = os.path.basename(name.strip())
        if not clean or clean.startswith("."):
            raise PlanError("an attachment needs a plain file name")
        if not body.strip():
            raise PlanError(f"{clean} is empty; there is nothing to attach")
        leaks = grogu_privacy.blocking(
            grogu_privacy.scan(body, path=clean),
            destination=grogu_privacy.PUBLISHED,
        )
        if leaks:
            raise PlanError(
                f"{clean} carries what looks like private data, and attachments "
                "ship with the plan:\n" + grogu_privacy.report(leaks)
            )
        with self.locked():
            manifest = self.load(plan_id)
            if stage and stage not in manifest.get("stages", {}):
                raise PlanError(f"plan {plan_id} has no {stage} stage to attach to")
            directory = self.plan_dir(plan_id) / "attachments"
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / clean
            replaced = target.exists()
            target.write_text(body, encoding="utf8")
            records = [
                item
                for item in manifest.setdefault("attachments", [])
                if item.get("name") != clean
            ]
            records.append(
                {
                    "name": clean,
                    "stage": stage,
                    "role": role or actor(),
                    "note": note.strip(),
                    "bytes": len(body.encode("utf8")),
                    "at": now(),
                    "verifier": bool(verifier),
                }
            )
            manifest["attachments"] = records
            self._save(manifest, "attached", note=f"{clean} ({len(body)} bytes)")
            return {
                "plan": plan_id,
                "name": clean,
                "path": str(target),
                "bytes": len(body.encode("utf8")),
                "replaced": replaced,
            }

    def attachments(self, plan_id: str) -> list:
        return list(self.load(self.resolve(plan_id)).get("attachments", []))

    def _stage_for_review(self, paths: list) -> list:
        """Put the plans in the index, because "remember to stage them" lost.

        Finalize used to unseal the plans and print a sentence asking someone
        to `git add` them. The tester that ran it reported the plan directory
        still sitting untracked afterwards. The whole point of finalizing is
        that the pull request carries the plan it implements, so the command
        that finalizes does the staging.

        Only the Markdown is staged; manifest.json stays local because it
        carries session ids, actor strings and the full text of every
        amendment, which is working state rather than an account.
        """
        if not paths:
            return []
        try:
            inside = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=self.root,
                capture_output=True,
                text=True,
            )
            if inside.returncode != 0:
                return []
            added = subprocess.run(
                ["git", "add", "--", *paths],
                cwd=self.root,
                capture_output=True,
                text=True,
            )
            return list(paths) if added.returncode == 0 else []
        except OSError:
            return []

    def _plan_record(self, manifest: dict) -> str:
        """The one-page account a reviewer wants and manifest.json is not."""
        plan_id = manifest["id"]
        lines = [
            f"# {manifest.get('title', plan_id)}",
            "",
            f"Plan `{plan_id}`. This is the account of how the plan changed while",
            "it was being carried out; the stage files next to it are the plan.",
            "",
        ]
        declined = manifest.get("declined_stages", {})
        if declined:
            lines.append("## Stages judged unnecessary")
            lines.append("")
            for stage, decision in sorted(declined.items()):
                lines.append(f"- **{stage}** — {decision.get('why', '')}")
            lines.append("")
        accepted = [
            amendment
            for amendment in manifest.get("amendments", [])
            if amendment.get("status") == ACCEPTED
        ]
        if accepted:
            lines.append("## Plan changes accepted mid-flight")
            lines.append("")
            for amendment in accepted:
                lines.append(
                    f"- **{amendment['id']}** (raised by the "
                    f"{amendment.get('raised_by') or amendment.get('role') or 'engineer'}): "
                    f"{amendment.get('claim', '')}"
                )
                if amendment.get("reason"):
                    lines.append(f"  - resolved: {amendment['reason']}")
            lines.append("")
        defects = manifest.get("defects", [])
        if defects:
            lines.append("## Defects found by the tester")
            lines.append("")
            for defect in defects:
                lines.append(
                    f"- **{defect['id']}** ({defect.get('status', '?')}, "
                    f"routed to {defect.get('owner', '?')}): {defect.get('report', '')}"
                )
            lines.append("")
        steering = manifest.get("steering", [])
        if steering:
            lines.append("## Corrections from the user")
            lines.append("")
            for note in steering:
                lines.append(f"- {note.get('text', '')}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

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
        return self._retro(self.load(plan_id))

    def _retro(self, manifest: dict) -> dict:
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
            "plan": manifest.get("id", ""),
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

    def names_the_harness(self, note: str) -> bool:
        """Is this complaint about grogu, whatever flag the caller passed?

        Nineteen friction notes were filed across five dogfood repositories
        and fifteen of them named a failing `grogu` command while omitting
        `--harness`, so they landed in a per-repository file the harness
        maintainer never reads. Asking agents to remember a flag did not
        work; the note says what it is about.
        """
        return bool(_HARNESS_NOTE_PATTERN.search(note))

    def note_friction(
        self,
        note: str,
        *,
        plan_id: str = "",
        role: str = "",
        target: str = TARGET_REPO,
    ) -> dict:
        """Record friction an agent hit, for review rather than for nobody.

        Friction with the *harness* is written to the user-scoped store rather
        than this repository's, because it would otherwise land in the one
        place its reader never looks: an engineer in some other repository
        hitting a missing grogu command writes the complaint into that
        repository, while the harness is fixed here. Cross-repository is also
        the only scope at which the useful signal exists — the same gap hit in
        four repositories is the one worth fixing.
        """
        if not note.strip():
            raise PlanError("friction needs a note")
        if target not in FRICTION_TARGETS:
            raise PlanError(
                f"unknown friction target {target!r}; expected one of "
                + ", ".join(FRICTION_TARGETS)
            )
        role = role or current_role() or "unknown"
        if target == TARGET_REPO_ONLY:
            target = TARGET_REPO
        elif target == TARGET_REPO and self.names_the_harness(note):
            target = TARGET_HARNESS
        if target == TARGET_HARNESS:
            # This note leaves the repository it was written in: it is pooled
            # across every repository and later proposed as work in the Grogu
            # checkout, which is public. A complaint quoting the failing command
            # is exactly how a token from a private repository ends up in an
            # issue about the harness, so it is redacted on the way out rather
            # than refused — the complaint is still worth having.
            note = grogu_privacy.redact(note)
            return self._note_harness_friction(note, plan_id=plan_id, role=role)
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

    def _note_harness_friction(self, note: str, *, plan_id: str, role: str) -> dict:
        with harness_friction_lock():
            return self._append_harness_friction(note, plan_id=plan_id, role=role)

    def _append_harness_friction(self, note: str, *, plan_id: str, role: str) -> dict:
        path = harness_friction_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {}
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf8"))
            except (OSError, ValueError):
                payload = {}
        entries = payload.setdefault("entries", [])
        entry = {
            "seq": len(entries) + 1,
            "at": now(),
            "actor": actor(),
            "role": role,
            "plan": plan_id,
            "repository": self.root.name,
            "repository_path": str(self.root),
            "note": note.strip(),
            "status": PENDING,
        }
        entries.append(entry)
        _write_harness_friction(payload)
        return entry

    def friction(self, *, include_resolved: bool = False) -> dict:
        """Recorded friction plus retro findings, aggregated across plans.

        One bad plan is noise. The same finding across several plans is a
        change to make to the harness or to a role overlay, and that is the
        distinction this report exists to draw.
        """
        payload = self._read_json(self.friction_path)
        entries = payload.get("entries", [])
        harness = harness_friction(include_resolved=include_resolved)
        if not include_resolved:
            entries = [entry for entry in entries if entry.get("status") == PENDING]

        totals: dict = {}
        examples: dict = {}
        for plan in self.list_plans():
            # `retro` by id would re-read the manifest `list_plans` just read;
            # the friction report is run often enough for that to matter.
            for finding in self._retro(plan).get("findings", []):
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
            "harness": harness,
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

    def _remember_session_role(self, role: str, plan_id: str, agent: str) -> None:
        path = self.state_dir / "session-roles.json"
        payload = self._read_json(path) if path.exists() else {}
        payload[str(Path.cwd().resolve())] = {
            "role": role,
            "plan": plan_id,
            "at": now(),
            "agent": agent,
        }
        self._write_json(path, payload)

    def bind_session(self, role: str, plan_id: str = "") -> None:
        """Record which role is working in this directory.

        Steering has to reach an agent whose environment we cannot set. A
        subagent's first command is its brief, so the brief is where the role
        becomes discoverable; every later `grogu` call in that directory can
        then carry steering without the model being asked to poll for it.
        """
        self.claim_agent_role(
            plan_id,
            role,
            identify=True,
            record_presence=False,
        )

    def session_bindings(self) -> dict:
        path = self.state_dir / "session-roles.json"
        if not path.exists():
            return {}
        payload = self._read_json(path)
        return payload if isinstance(payload, dict) else {}

    def session_binding(self) -> dict:
        return self.session_bindings().get(str(Path.cwd().resolve()), {})

    def binding_covering(self, root: Optional[Path] = None) -> dict:
        """The role bound anywhere at or above here, or anywhere inside `root`.

        `session_binding` is keyed to the exact directory it was made in, which
        is right for steering -- steering is delivered to the shell that asked.
        It is wrong for a refusal: an engineer that ran `cd src` or passed
        `--repo` at a checkout it had already declared itself in looked to the
        harness like an anonymous shell, which is the user, which is allowed to
        decide skills. So the question a refusal asks is the broader one: is
        there a declared role whose working directory is part of this tree?
        """
        bindings = self.session_bindings()
        here = Path.cwd().resolve()
        covering = [str(parent) for parent in [here] + list(here.parents)]
        if root is not None:
            base = str(Path(root).expanduser().resolve())
            covering += [
                path
                for path in sorted(bindings)
                if path == base or path.startswith(base + os.sep)
            ]
        found = [
            bindings[path]
            for path in dict.fromkeys(covering)
            if isinstance(bindings.get(path), dict) and bindings[path].get("role")
        ]
        # The most recent one, not the nearest. Returning the nearest meant a
        # tester that worked in `src` last week masked the engineer that
        # declared itself at the root ten minutes ago: the caller saw one
        # binding, judged it stale, and allowed what it should have refused.
        # Whoever ran a command most recently is the one who is still here.
        #
        # Sorted on the parsed timestamp rather than the string, because a
        # binding whose `at` was unparseable sorted above every real one -- so
        # any junk in that field masked the live agent and reopened the same
        # hole from the other side. An unreadable timestamp is treated as
        # ancient: it cannot be used to claim someone is still working.
        return max(found, key=self._binding_time, default={})

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
            "commission": (
                (self.load(plan_id).get("commissions", {}) or {}).get(role, {})
                if plan_id
                else {}
            ),
            "design_principles": principles,
            "attachments": self.attachments(plan_id) if plan_id else [],
            "summary": self.summary(plan_id) if plan_id else {},
        }


class PlanDocumentStore:
    """The `.plan` package facade used by PlanStore, the CLI, and HTTP.

    The graph is authoritative.  Stage Markdown remains at the legacy paths,
    but only this facade's compiler writes it.  A role-bounded load opens only
    partitions that role can read; sealed partition bytes may be copied as
    opaque data during a generation, but are never decoded for an unreadable
    role.
    """

    SCHEMA_VERSION = 1
    FORMAT = "grogu.plan_package"
    MAX_PATCH_OPS = 500
    MAX_REQUEST_BYTES = 256 * 1024
    _PARTITION_PATHS = {
        grogu_plandoc.OPEN_PARTITION: "graph/open.json",
        TESTING: "graph/testing.sealed",
        EVALUATION: "graph/evaluation.sealed",
    }

    def __init__(self, plans: PlanStore, plan_id: str):
        self.plans = plans
        self.plan_id = plan_id
        self.package = plans.document_plan_dir(plan_id)
        self.last_recovery: list[str] = []

    # -- identity and paths ----------------------------------------------

    @classmethod
    def for_plan(cls, plans: PlanStore, reference: str) -> "PlanDocumentStore":
        plan_id = plans.resolve(reference)
        if not plans.is_document_plan(plan_id):
            raise PlanError(
                f"plan {plan_id} uses the legacy layout; run "
                f"`grogu plan doc migrate {plan_id}` first"
            )
        return cls(plans, plan_id)

    @staticmethod
    def _role(role: str, *, fallback: str = REVIEWER) -> str:
        effective = (role or current_role() or fallback).strip().lower()
        if effective not in ROLES:
            raise PlanError(
                f"unknown role {effective!r}; expected one of {', '.join(ROLES)}"
            )
        return effective

    def _claim(self, role: str) -> str:
        effective = self._role(role)
        self.plans.claim_agent_role(self.plan_id, effective)
        return effective

    def _partition_path(self, partition: str, *, revision: str = "") -> Path:
        relative = self._PARTITION_PATHS[partition]
        if revision:
            return self.package / "recovery" / revision / relative
        return self.package / relative

    def _stage_relative(self, stage: str) -> str:
        return f"{stage}.sealed" if stage in SEALED_STAGES else f"{stage}.md"

    def _stage_path(self, stage: str, *, revision: str = "") -> Path:
        relative = self._stage_relative(stage)
        if revision:
            return self.package / "recovery" / revision / "artifacts" / relative
        if stage in SEALED_STAGES and (self.package / f"{stage}.md").is_file():
            return self.package / f"{stage}.md"
        return self.package / relative

    # -- canonical serialization ----------------------------------------

    @staticmethod
    def _sealed_bytes(value: dict) -> bytes:
        plain = grogu_plandoc_canon.pretty_dumps(value)
        return seal(plain).encode("utf8")

    @staticmethod
    def _open_bytes(value: dict) -> bytes:
        return grogu_plandoc_canon.pretty_dumpb(value)

    @classmethod
    def _partition_bytes(cls, document: dict) -> dict[str, bytes]:
        partitions = grogu_plandoc.split_partitions(document)
        return {
            cls._PARTITION_PATHS[name]: (
                cls._open_bytes(partition)
                if name == grogu_plandoc.OPEN_PARTITION
                else cls._sealed_bytes(partition)
            )
            for name, partition in partitions.items()
        }

    @staticmethod
    def _storage_state(
        partitions: dict[str, bytes],
        *,
        object_metadata: Optional[dict[str, dict[str, list[str]]]] = None,
    ) -> dict:
        return {
            relative: {
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
                **(
                    {
                        "objects": copy.deepcopy(
                            object_metadata.get(relative, {})
                        )
                    }
                    if object_metadata is not None
                    else {}
                ),
            }
            for relative, payload in sorted(partitions.items())
        }

    @staticmethod
    def _partition_object_metadata(
        document: dict,
        partitions: dict[str, dict],
    ) -> dict[str, dict[str, list[str]]]:
        values: dict[str, dict[str, list[str]]] = {}
        for name, partition in partitions.items():
            objects: dict[str, list[str]] = {}
            for node_id, node in partition.get("nodes", {}).items():
                objects[node_id] = [str(node.get("stage", ""))]
            for edge_id, edge in partition.get("edges", {}).items():
                stages = {
                    str(document["nodes"][endpoint].get("stage", ""))
                    for endpoint in (edge.get("from"), edge.get("to"))
                    if endpoint in document.get("nodes", {})
                }
                objects[edge_id] = sorted(stages)
            values[PlanDocumentStore._PARTITION_PATHS[name]] = dict(
                sorted(
                    objects.items(),
                    key=lambda item: grogu_plandoc_canon.id_sort_key(item[0]),
                )
            )
        return values

    @staticmethod
    def _partition_relationship_metadata(
        partitions: dict[str, dict],
    ) -> dict[str, dict[str, list[str]]]:
        return {
            PlanDocumentStore._PARTITION_PATHS[name]: {
                edge_id: [
                    str(edge.get("from", "")),
                    str(edge.get("to", "")),
                ]
                for edge_id, edge in sorted(
                    partition.get("edges", {}).items(),
                    key=lambda item: grogu_plandoc_canon.id_sort_key(item[0]),
                )
            }
            for name, partition in partitions.items()
        }

    @staticmethod
    def _artifact_bytes(stage: str, markdown: str) -> bytes:
        return (
            seal(markdown).encode("utf8")
            if stage in SEALED_STAGES
            else markdown.encode("utf8")
        )

    @staticmethod
    def _read_json_file(path: Path) -> dict:
        try:
            return grogu_plandoc_canon.loads(
                grogu_plandoc_revision.safe_read(path)
            )
        except FileNotFoundError as error:
            raise PlanError(f"missing plan package part: {path}") from error
        except (UnicodeDecodeError, ValueError) as error:
            raise PlanError(f"invalid plan package part {path}: {error}") from error

    def _read_partition(
        self,
        partition: str,
        *,
        revision: str,
        recover: bool = True,
    ) -> dict:
        current = not revision
        target_revision = revision or self.head(repair=recover)
        path = self._partition_path(partition, revision=revision)
        recovery = self._partition_path(partition, revision=target_revision)
        try:
            raw = grogu_plandoc_revision.safe_read(path)
        except (FileNotFoundError, grogu_plandoc_revision.RevisionError) as error:
            if not current or not recover:
                raise PlanError(f"missing or unsafe plan partition {path}") from error
            try:
                raw = grogu_plandoc_revision.safe_read(recovery)
                grogu_plandoc_revision.atomic_write(path, raw)
                self.last_recovery.append(
                    f"restored {path.relative_to(self.package)} from {target_revision}"
                )
            except (FileNotFoundError, grogu_plandoc_revision.RevisionError) as inner:
                raise PlanError(f"cannot recover plan partition {path}") from inner
        try:
            if partition == grogu_plandoc.OPEN_PARTITION:
                value = grogu_plandoc_canon.loads(raw)
            else:
                value = grogu_plandoc_canon.loads(unseal(raw.decode("utf8")))
        except (UnicodeDecodeError, ValueError, zlib.error) as error:
            if not current or not recover:
                raise PlanError(f"invalid plan partition {path}: {error}") from error
            try:
                restored = grogu_plandoc_revision.safe_read(recovery)
                if restored == raw:
                    raise PlanError(f"invalid recovery partition {recovery}") from error
                grogu_plandoc_revision.atomic_write(path, restored)
                self.last_recovery.append(
                    f"restored corrupted {path.relative_to(self.package)} "
                    f"from {target_revision}"
                )
                raw = restored
                value = (
                    grogu_plandoc_canon.loads(raw)
                    if partition == grogu_plandoc.OPEN_PARTITION
                    else grogu_plandoc_canon.loads(unseal(raw.decode("utf8")))
                )
            except (FileNotFoundError, UnicodeDecodeError, ValueError) as inner:
                raise PlanError(f"cannot recover corrupted partition {path}") from inner
        # A partition only changes when content in it changes, so its stored
        # revision may lag HEAD.  The role-bounded materialization is stamped
        # with the selected revision without rewriting or decoding any other
        # partition.
        value["revision"] = target_revision
        return value

    # -- package creation and migration ---------------------------------

    @staticmethod
    def _counter_state(document: dict) -> dict:
        counters: dict[str, int] = {}
        for node in document.get("nodes", {}).values():
            prefix, _, number = str(node.get("id", "")).rpartition("-")
            if number.isdigit():
                counters[prefix] = max(counters.get(prefix, 0), int(number))
        for edge in document.get("edges", {}).values():
            prefix, _, number = str(edge.get("id", "")).rpartition("-")
            if number.isdigit():
                counters[prefix] = max(counters.get(prefix, 0), int(number))
        return counters

    @staticmethod
    def _merge_counters(existing: dict, observed: dict) -> dict:
        keys = set(existing) | set(observed)
        return {
            key: max(
                int(existing.get(key, 0) or 0),
                int(observed.get(key, 0) or 0),
            )
            for key in sorted(keys)
        }

    @staticmethod
    def _manifest_object_metadata(manifest: dict) -> dict[str, dict[str, list[str]]]:
        state = manifest.get("plandoc", {}).get("partition_state", {})
        return {
            relative: copy.deepcopy(
                details.get("objects", {})
                if isinstance(details, dict)
                and isinstance(details.get("objects", {}), dict)
                else {}
            )
            for relative, details in state.items()
            if relative in PlanDocumentStore._PARTITION_PATHS.values()
        }

    @staticmethod
    def _manifest_relationship_metadata(
        manifest: dict,
    ) -> dict[str, dict[str, list[str]]]:
        state = manifest.get("plandoc", {}).get("partition_state", {})
        return {
            relative: copy.deepcopy(
                details.get("relationships", {})
                if isinstance(details, dict)
                and isinstance(details.get("relationships", {}), dict)
                else {}
            )
            for relative, details in state.items()
            if relative in PlanDocumentStore._PARTITION_PATHS.values()
        }

    @classmethod
    def _all_manifest_object_ids(cls, manifest: dict) -> set[str]:
        return {
            identifier
            for objects in cls._manifest_object_metadata(manifest).values()
            for identifier in objects
        }

    @staticmethod
    def _graph_timestamp(value: str = "") -> str:
        text = str(value or "").strip()
        if text:
            try:
                parsed = dt.datetime.fromisoformat(
                    text.replace("Z", "+00:00")
                )
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=dt.timezone.utc)
                return (
                    parsed.astimezone(dt.timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except ValueError:
                pass
        return grogu_plandoc_revision.utc_now()

    @classmethod
    def create(
        cls,
        plans: PlanStore,
        title: str,
        *,
        task_id: str = "",
        design: bool = False,
        evaluation: bool = False,
        review_required: bool = False,
        requested_by: str = "",
    ) -> dict:
        with plans.locked():
            plan_id = plans._new_id()
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
                "plandoc": {
                    "schema_version": cls.SCHEMA_VERSION,
                    "format": cls.FORMAT,
                    "compiler_version": grogu_plandoc_compile.COMPILER_VERSION,
                    "counters": {},
                    "parts": [],
                },
            }
            package = plans.document_plan_dir(plan_id)
            package.mkdir(parents=True)
            plans._write_json(package / "manifest.json", manifest)
            document = grogu_plandoc.new_document(
                plan_id, title, revision="r0001"
            )
            service = cls(plans, plan_id)
            service._write_initial_generation(
                manifest,
                document,
                origin="create",
                intent="create plan document package",
            )
            return plans.load(plan_id)

    @classmethod
    def migrate(
        cls,
        plans: PlanStore,
        reference: str,
        *,
        role: str = "",
        dry_run: bool = False,
    ) -> dict:
        plan_id = plans.resolve(reference)
        legacy = plans.legacy_plan_dir(plan_id)
        package = plans.document_plan_dir(plan_id)
        if package.exists():
            if legacy.exists():
                plans.plan_dir(plan_id)  # raises the collision with both paths
            raise PlanError(f"plan {plan_id} is already a .plan package")
        if not legacy.is_dir():
            raise PlanError(f"legacy plan directory was not found: {legacy}")
        effective = cls._role(role)
        plans.claim_agent_role(plan_id, effective)
        manifest = plans.load(plan_id)
        readable = ROLE_READABLE_STAGES.get(effective, frozenset())
        written_stages = [
            stage
            for stage in manifest.get("stages", [])
            if manifest.get("stage_written", {}).get(stage)
        ]
        forbidden = [stage for stage in written_stages if stage not in readable]
        if forbidden:
            raise PlanError(
                f"role {effective!r} cannot migrate {plan_id}: migration must "
                "read every written stage through PlanStore.read_stage, and "
                f"{', '.join(forbidden)} is outside that role"
            )
        sources = {
            stage: plans.read_stage(plan_id, stage, role=effective)
            for stage in written_stages
        }
        review_state = {}
        review_path = legacy / "review.json"
        if review_path.is_file():
            review_state = plans._read_json(review_path)
        steps = [
            "copy",
            "parse",
            "import diagrams",
            "import threads",
            "compile",
            "verify",
        ]
        if dry_run:
            return {
                "plan": plan_id,
                "from": str(legacy),
                "to": str(package),
                "steps": steps,
                "written_stages": written_stages,
                "dry_run": True,
            }

        staging = plans.plans_dir / f".{plan_id}.plan.migrating-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True)
        try:
            shutil.copytree(
                legacy,
                staging / "legacy" / "pre-migration",
                symlinks=True,
            )
            migrated_manifest = copy.deepcopy(manifest)
            migrated_manifest["plandoc"] = {
                "schema_version": cls.SCHEMA_VERSION,
                "format": cls.FORMAT,
                "compiler_version": grogu_plandoc_compile.COMPILER_VERSION,
                "counters": {},
                "parts": [],
                "migrated_at": now(),
                "migrated_from": str(legacy),
            }
            document = grogu_plandoc.new_document(
                plan_id,
                str(manifest.get("title", plan_id)),
                revision="r0001",
            )
            for stage in written_stages:
                imported = grogu_plandoc_compile.import_markdown(
                    sources[stage],
                    plan_id=plan_id,
                    stage=stage,
                    revision="r0001",
                )
                cls._merge_imported(document, imported, migrated_manifest)
            cls._migrate_review_threads(
                document,
                review_state,
                migrated_manifest,
            )
            document["revision"] = "r0001"
            document["title"] = str(manifest.get("title", plan_id))
            document["provenance"] = {
                "at": now(),
                "actor": actor(),
                "role": effective,
                "agent": os.environ.get("GROGU_AGENT", "") or "migration",
                "origin": "migration",
            }
            grogu_plandoc_schema.validate_document(document)
            diagnostics = cls._migration_diagnostics(document, sources)
            failed = [item for item in diagnostics if not item["equivalent"]]
            if failed:
                raise PlanError(
                    "migration produced a non-equivalent artifact; the legacy "
                    "directory is untouched: "
                    + "; ".join(
                        f"{item['stage']}: {item['message']}" for item in failed
                    )
                )
            plans._write_json(staging / "manifest.json", migrated_manifest)
            service = cls(plans, plan_id)
            service.package = staging
            service._write_initial_generation(
                migrated_manifest,
                document,
                origin="migration",
                intent="migrate legacy plan without dropping source text",
            )
            # Verify the complete staged package before the legacy path moves.
            service._verify_integrity(migrated_manifest)
            with plans.locked():
                if package.exists():
                    raise PlanError(
                        f"refusing migration because {package} appeared while "
                        "the package was being built"
                    )
                os.replace(staging, package)
                try:
                    shutil.rmtree(legacy)
                except OSError as error:
                    # This is the one intentionally safe failure state: both
                    # paths remain and every command refuses until repaired.
                    raise PlanError(
                        f"the verified package is at {package}, but the legacy "
                        f"path could not be removed ({error}); both paths now "
                        "exist, so remove the one you do not want"
                    ) from error
            return {
                "plan": plan_id,
                "from": str(legacy),
                "to": str(package),
                "steps": steps,
                "revision": "r0001",
                "diagnostics": diagnostics,
                "review_threads": len(
                    [
                        node
                        for node in document["nodes"].values()
                        if node["kind"] == "thread"
                    ]
                ),
                "dry_run": False,
            }
        except Exception:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            raise

    @staticmethod
    def _merge_imported(target: dict, source: dict, manifest: dict) -> None:
        mapping: dict[str, str] = {}
        counters = manifest.setdefault("plandoc", {}).setdefault("counters", {})
        counter_manifest = {"plandoc": {"counters": counters}}
        occupied = set(target["nodes"]) | set(target["edges"])
        for old_id, node in sorted(
            source["nodes"].items(),
            key=lambda item: grogu_plandoc.node_sort_key(item[1]),
        ):
            new_id = grogu_plandoc.allocate_id(
                counter_manifest,
                node["kind"],
                existing_ids=occupied,
            )
            mapping[old_id] = new_id
            occupied.add(new_id)
        for old_id, edge in sorted(
            source["edges"].items(),
            key=lambda item: grogu_plandoc.edge_sort_key(item[1]),
        ):
            new_id = grogu_plandoc.allocate_id(
                counter_manifest,
                "edge",
                existing_ids=occupied,
            )
            mapping[old_id] = new_id
            occupied.add(new_id)

        def remap(value):
            if isinstance(value, str):
                return mapping.get(value, value)
            if isinstance(value, list):
                return [remap(item) for item in value]
            if isinstance(value, dict):
                return {key: remap(item) for key, item in value.items()}
            return value

        for old_id, node in source["nodes"].items():
            copied = remap(copy.deepcopy(node))
            copied["id"] = mapping[old_id]
            target["nodes"][copied["id"]] = copied
        for old_id, edge in source["edges"].items():
            copied = remap(copy.deepcopy(edge))
            copied["id"] = mapping[old_id]
            target["edges"][copied["id"]] = copied

    @staticmethod
    def _migration_diagnostics(document: dict, sources: dict[str, str]) -> list[dict]:
        diagnostics = []
        for stage, source in sources.items():
            spans = sorted(
                (
                    int(node["attrs"]["source_start"]),
                    int(node["attrs"]["source_end"]),
                    node["body"],
                )
                for node in document["nodes"].values()
                if node["stage"] == stage
                and node["kind"] == "note"
                and node.get("attrs", {}).get("source") == "markdown-import"
            )
            exact_spans = all(
                0 <= start <= end <= len(source)
                and source[start:end] == body
                for start, end, body in spans
            )
            represented = "".join(body for _start, _end, body in spans)
            diagnostics.append(
                {
                    "stage": stage,
                    # Headings are represented structurally as node titles;
                    # their original bytes remain in legacy/pre-migration.
                    # Every prose span imported into the graph must still be
                    # byte-exact, which detects parser loss without pretending
                    # the normalized compiler is a byte-for-byte copier.
                    "equivalent": exact_spans,
                    "source_digest": hashlib.sha256(
                        source.encode("utf8")
                    ).hexdigest(),
                    "represented_digest": hashlib.sha256(
                        represented.encode("utf8")
                    ).hexdigest(),
                    "message": (
                        "imported prose spans are byte-exact and the complete "
                        "source is retained in legacy/pre-migration"
                        if exact_spans
                        else "an imported source span differs from the legacy stage"
                    ),
                }
            )
        return diagnostics

    @classmethod
    def _migrate_review_threads(
        cls,
        document: dict,
        state: dict,
        manifest: dict,
    ) -> None:
        threads = state.get("threads", []) if isinstance(state, dict) else []
        if not isinstance(threads, list):
            return
        manifest["review_rounds"] = copy.deepcopy(
            state.get("rounds", []) if isinstance(state.get("rounds"), list) else []
        )
        counters = manifest.setdefault("plandoc", {}).setdefault("counters", {})
        counter_manifest = {"plandoc": {"counters": counters}}
        for legacy in threads:
            if not isinstance(legacy, dict):
                continue
            selector = cls._selector_for_legacy_anchor(document, legacy)
            stage = str(legacy.get("stage", ""))
            thread_id = grogu_plandoc.allocate_id(
                counter_manifest,
                "thread",
                existing_ids=document["nodes"],
            )
            comments = []
            for index, comment in enumerate(legacy.get("comments", []), 1):
                if not isinstance(comment, dict) or not str(comment.get("body", "")).strip():
                    continue
                item = {
                    "id": f"{thread_id}-c{index}",
                    "at": cls._graph_timestamp(str(comment.get("at") or "")),
                    "author": str(comment.get("author") or actor()),
                    "body": str(comment.get("body", "")),
                    "revision": "r0001",
                }
                comments.append(item)
            if not comments:
                continue
            attrs = {
                "selector": selector,
                "comments": comments,
                "status": (
                    "resolved"
                    if legacy.get("status") == "resolved"
                    else "open"
                ),
                "anchor_state": (
                    "orphaned"
                    if legacy.get("anchor_state") == "orphaned"
                    else (
                        "shifted"
                        if legacy.get("anchor_state") == "shifted"
                        else "resolved"
                    )
                ),
                "thread_kind": "discussion",
                "round": int(legacy.get("round", 1) or 1),
                "legacy_id": str(legacy.get("id", "")),
                "anchor_history": copy.deepcopy(
                    legacy.get("anchor_history", [])
                ),
            }
            node = grogu_plandoc.make_node(
                thread_id,
                "thread",
                f"Review thread {attrs['legacy_id'] or thread_id}",
                stage=stage,
                body="",
                attrs=attrs,
                order=2_000_000 + len(document["nodes"]),
                revision="r0001",
                ext={"dev.grogu.review": copy.deepcopy(legacy)},
            )
            document["nodes"][thread_id] = node

    @staticmethod
    def _selector_for_legacy_anchor(document: dict, thread: dict) -> dict:
        anchor = thread.get("anchor") if isinstance(thread.get("anchor"), dict) else {}
        stage = str(thread.get("stage", ""))
        if anchor.get("kind") == "text":
            start = int(anchor.get("start", 0) or 0)
            end = int(anchor.get("end", start) or start)
            owner = next(
                (
                    node
                    for node in sorted(
                        document["nodes"].values(),
                        key=grogu_plandoc.node_sort_key,
                    )
                    if node["stage"] == stage
                    and node["kind"] == "note"
                    and node.get("attrs", {}).get("source") == "markdown-import"
                    and int(node["attrs"].get("source_start", 0)) <= start
                    and int(node["attrs"].get("source_end", 0)) >= end
                ),
                None,
            )
            if owner is not None:
                offset = int(owner["attrs"]["source_start"])
                return {
                    "type": "text",
                    "node": owner["id"],
                    "quote": {
                        "exact": str(anchor.get("exact", "")),
                        "prefix": str(anchor.get("prefix", "")),
                        "suffix": str(anchor.get("suffix", "")),
                    },
                    "position": {
                        "start": max(0, start - offset),
                        "end": max(0, end - offset),
                    },
                    "body_digest": grogu_plandoc_anchor.digest(owner["body"]),
                }
        # Mermaid anchors carry stable ids when possible.  Fall back to an
        # orphaned node selector rather than inventing a fuzzy graph target.
        if anchor.get("kind") == "mermaid":
            diagram_nodes = [
                node
                for node in document["nodes"].values()
                if node["stage"] == stage and node["kind"] == "diagram"
            ]
            block_index = int(anchor.get("block_index", 0) or 0)
            if block_index < len(diagram_nodes):
                diagram = sorted(diagram_nodes, key=grogu_plandoc.node_sort_key)[
                    block_index
                ]
                if anchor.get("target") == "node":
                    wanted = str(anchor.get("node_id", ""))
                    child = next(
                        (
                            node
                            for node in document["nodes"].values()
                            if node.get("attrs", {}).get("diagram") == diagram["id"]
                            and node.get("attrs", {}).get("mermaid_id") == wanted
                        ),
                        None,
                    )
                    if child is not None:
                        return {"type": "node", "id": child["id"]}
                return {"type": "node", "id": diagram["id"]}
        fallback = next(
            (
                node
                for node in document["nodes"].values()
                if node["stage"] == stage and node["kind"] != "thread"
            ),
            None,
        )
        if fallback is not None:
            return {"type": "node", "id": fallback["id"]}
        # The schema requires a stable id even for an orphaned selector.  The
        # caller sets anchor_state=orphaned, so the target may be absent.
        return {"type": "node", "id": "note-1"}

    def _write_initial_generation(
        self,
        manifest: dict,
        document: dict,
        *,
        origin: str,
        intent: str,
    ) -> None:
        partitions = self._partition_bytes(document)
        artifacts, results = self._compiled_stage_artifacts(
            document, manifest, stages=manifest.get("stages", [])
        )
        recovery = self._recovery_artifacts(
            "r0001", partitions=partitions, stage_artifacts=artifacts
        )
        before = {}
        split = grogu_plandoc.split_partitions(document)
        object_metadata = self._partition_object_metadata(document, split)
        relationship_metadata = self._partition_relationship_metadata(split)
        after = self._storage_state(
            partitions,
            object_metadata=object_metadata,
        )
        empty = grogu_plandoc.new_document(
            self.plan_id,
            str(manifest.get("title", self.plan_id)),
            revision="r0000",
        )
        operations = grogu_plandoc_patch.diff(empty, document)
        visibility = self._operation_visibility(empty, document, operations)
        envelope = grogu_plandoc_revision.make_revision(
            before,
            after,
            seq=1,
            actor=actor(),
            role=current_role() or REVIEWER,
            agent=os.environ.get("GROGU_AGENT", "") or "grogu",
            intent=intent,
            origin=origin,
            ops=operations,
        )
        grogu_plandoc_revision.write_generation(
            self.package,
            envelope,
            partitions=partitions,
            artifacts={
                **artifacts,
                **recovery,
                "revision-meta/r0001.json": grogu_plandoc_canon.pretty_dumpb(
                    visibility
                ),
            },
            base="",
            identity_checks=[
                lambda evidence=result["identities"]: all(evidence.values())
                for result in results.values()
            ],
        )
        manifest.setdefault("plandoc", {})["counters"] = self._counter_state(
            document
        )
        self._update_manifest_parts(
            manifest,
            partitions,
            artifacts,
            envelope,
            object_metadata=object_metadata,
            relationship_metadata=relationship_metadata,
        )
        self.plans._write_json(self.package / "manifest.json", manifest)
        for stage, result in results.items():
            spec = self._projection_spec(REVIEWER, [stage])
            grogu_plandoc_compile.write_cache(
                self.package, result["ir"], spec, format="md"
            )
            grogu_plandoc_compile.write_cache(
                self.package, result["ir"], spec, format="json"
            )

    @staticmethod
    def _recovery_artifacts(
        revision: str,
        *,
        partitions: dict[str, bytes],
        stage_artifacts: dict[str, bytes],
    ) -> dict[str, bytes]:
        values = {
            f"recovery/{revision}/{relative}": payload
            for relative, payload in partitions.items()
        }
        values.update(
            {
                f"recovery/{revision}/artifacts/{relative}": payload
                for relative, payload in stage_artifacts.items()
            }
        )
        return values

    def _compiled_stage_artifacts(
        self,
        document: dict,
        manifest: dict,
        *,
        stages,
    ) -> tuple[dict[str, bytes], dict[str, dict]]:
        artifacts: dict[str, bytes] = {}
        results: dict[str, dict] = {}
        partitions = grogu_plandoc.split_partitions(document)
        reviewer_view = grogu_plandoc.load_visible(
            lambda name: partitions[name],
            REVIEWER,
            sealed_relationship_count=int(
                manifest.get("plandoc", {}).get(
                    "sealed_relationship_count", 0
                )
            ),
        )
        for stage in STAGES:
            if stage not in stages:
                continue
            spec = self._projection_spec(REVIEWER, [stage])
            result = grogu_plandoc_compile.compile_checked(
                reviewer_view, spec
            )
            artifacts[self._stage_relative(stage)] = self._artifact_bytes(
                stage, result["markdown"]
            )
            results[stage] = result
        return artifacts, results

    @staticmethod
    def _projection_spec(
        role: str,
        stages,
        *,
        include: str = "all",
        budget: Optional[int] = None,
        since: str = "",
        depth: Optional[int] = None,
    ) -> dict:
        value = {
            "role": role,
            "stages": list(stages),
            "include": include,
            "budget_chars": budget,
            "since": since,
            "compiler": grogu_plandoc_compile.COMPILER_VERSION,
        }
        if depth is not None:
            value["depth"] = depth
        return grogu_plandoc_schema.validate_projection_spec(value)

    def _update_manifest_parts(
        self,
        manifest: dict,
        partitions: dict[str, bytes],
        artifacts: dict[str, bytes],
        envelope: dict,
        *,
        object_metadata: Optional[dict[str, dict[str, list[str]]]] = None,
        relationship_metadata: Optional[
            dict[str, dict[str, list[str]]]
        ] = None,
    ) -> None:
        entries = []
        for relative, payload in sorted({**partitions, **artifacts}.items()):
            if relative.startswith("graph/"):
                role = "graph-partition"
                media = "application/json"
                if relative.endswith(".sealed"):
                    media = "application/vnd.grogu.sealed+text"
            else:
                role = "compiled-stage"
                media = (
                    "application/vnd.grogu.sealed+text"
                    if relative.endswith(".sealed")
                    else "text/markdown"
                )
            entries.append(
                {
                    "path": relative,
                    "media_type": media,
                    "role": role,
                    "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                    "relationships": [
                        {
                            "kind": "compiled-from"
                            if role == "compiled-stage"
                            else "materializes",
                            "target": envelope["revision"],
                        }
                    ],
                }
            )
        partition_state = self._storage_state(
            partitions,
            object_metadata=object_metadata,
        )
        if relationship_metadata is not None:
            for relative, relationships in relationship_metadata.items():
                partition_state.setdefault(relative, {})[
                    "relationships"
                ] = copy.deepcopy(relationships)
        manifest.setdefault("plandoc", {}).update(
            {
                "schema_version": self.SCHEMA_VERSION,
                "format": self.FORMAT,
                "compiler_version": grogu_plandoc_compile.COMPILER_VERSION,
                "head": envelope["revision"],
                "partition_state": partition_state,
                "parts": entries,
            }
        )

    # -- revision-safe reads ---------------------------------------------

    def _revision_complete(self, envelope: dict) -> bool:
        revision = str(envelope.get("revision", ""))
        return all(
            self._partition_path(name, revision=revision).is_file()
            for name in self._PARTITION_PATHS
        )

    def head(self, *, repair: bool = True) -> str:
        try:
            head = grogu_plandoc_revision.read_head(self.package)
            grogu_plandoc_revision.read_revision(self.package, head)
            if self._revision_complete(
                grogu_plandoc_revision.read_revision(self.package, head)
            ):
                return head
        except (grogu_plandoc_revision.RevisionError, FileNotFoundError):
            pass
        try:
            recovered = grogu_plandoc_revision.recover_head(
                self.package,
                repair=repair,
                is_complete=self._revision_complete,
            )
        except grogu_plandoc_revision.RevisionError as error:
            raise PlanError(f"could not recover {self.plan_id} HEAD: {error}") from error
        if repair:
            self.last_recovery.append(f"recovered HEAD to {recovered}")
        return recovered

    def load(
        self,
        *,
        role: str,
        revision: str = "",
        stages=None,
        record: bool = False,
    ) -> dict:
        effective = self._claim(role)
        allowed = ROLE_READABLE_STAGES.get(effective, frozenset())
        if not allowed:
            raise PlanError(f"role {effective!r} may not read plan document stages")
        manifest = self.plans.load(self.plan_id)
        source_stages = (
            list(stages)
            if stages is not None
            else [
                stage
                for stage in manifest.get("stages", [])
                if stage in allowed
            ]
        )
        requested = [
            stage
            for stage in source_stages
            if stage in manifest.get("stages", [])
        ]
        unreadable = set(requested) - set(allowed)
        if unreadable:
            raise PlanError(
                f"role {effective!r} may not read stage(s): "
                + ", ".join(sorted(unreadable))
            )
        # Stage authority stays in PlanStore.read_stage.  The call is
        # record-free for polling surfaces but still enforces the role and
        # identified-agent binding before a graph partition is opened.
        for stage in requested:
            self.plans.read_stage(
                self.plan_id, stage, role=effective, record=record
            )
        selected_revision = revision or self.head(repair=True)
        if revision:
            try:
                grogu_plandoc_revision.read_revision(self.package, revision)
            except grogu_plandoc_revision.RevisionError as error:
                raise PlanError(str(error)) from error

        def loader(name: str) -> dict:
            return self._read_partition(
                name,
                revision=selected_revision if revision else "",
                recover=not bool(revision),
            )

        visible = grogu_plandoc.load_visible(
            loader,
            effective,
            sealed_relationship_count=int(
                manifest.get("plandoc", {}).get(
                    "sealed_relationship_count", 0
                )
            ),
        )
        visible["revision"] = selected_revision
        if requested:
            keep = set(requested) | {""}
            node_ids = {
                node_id
                for node_id, node in visible["nodes"].items()
                if node.get("stage", "") in keep
            }
            visible["nodes"] = {
                node_id: node
                for node_id, node in visible["nodes"].items()
                if node_id in node_ids
            }
            visible["edges"] = {
                edge_id: edge
                for edge_id, edge in visible["edges"].items()
                if edge.get("from") in node_ids and edge.get("to") in node_ids
            }
            visible = grogu_plandoc_schema.validate_document(visible)
        return visible

    def load_all(self, *, role: str, revision: str = "") -> dict:
        effective = self._claim(role)
        if not set(STAGES).issubset(ROLE_READABLE_STAGES.get(effective, ())):
            raise PlanError(
                f"role {effective!r} cannot materialize every partition"
            )
        selected_revision = revision or self.head(repair=True)
        partitions = {
            name: self._read_partition(
                name,
                revision=selected_revision if revision else "",
                recover=not bool(revision),
            )
            for name in self._PARTITION_PATHS
        }
        document = grogu_plandoc.merge_partitions(partitions)
        document["revision"] = selected_revision
        return document

    # -- projections and verification -----------------------------------

    def projection(
        self,
        *,
        role: str,
        stages=None,
        include: str = "normative",
        budget: Optional[int] = None,
        since: str = "",
        format: str = "md",
        use_cache: bool = True,
    ) -> dict:
        effective = self._claim(role)
        if format not in {"md", "json"}:
            raise PlanError("projection format must be md or json")
        manifest = self.plans.load(self.plan_id)
        allowed = ROLE_READABLE_STAGES.get(effective, frozenset())
        source_stages = (
            list(stages)
            if stages is not None
            else [
                stage
                for stage in manifest.get("stages", [])
                if stage in allowed
            ]
        )
        selected = [
            stage
            for stage in source_stages
            if stage in manifest.get("stages", [])
        ]
        unreadable = set(selected) - set(
            allowed
        )
        if unreadable:
            raise PlanError(
                f"role {effective!r} may not compile stage(s): "
                + ", ".join(sorted(unreadable))
            )
        head = self.head(repair=True)
        spec = self._projection_spec(
            effective,
            selected,
            include=include,
            budget=budget,
            since=since,
        )
        if use_cache:
            try:
                cached = grogu_plandoc_compile.read_cache(
                    self.package, head, spec, format=format
                )
            except grogu_plandoc_compile.CompileError as error:
                raise PlanError(str(error)) from error
            if cached is not None:
                payload = (
                    cached.payload.decode("utf8")
                    if format == "md"
                    else grogu_plandoc_canon.loads(cached.payload)
                )
                return {
                    "plan": self.plan_id,
                    "revision": head,
                    "role": effective,
                    "stages": selected,
                    "include": include,
                    "budget": budget,
                    "format": format,
                    "etag": cached.etag,
                    "payload": payload,
                    "cached": True,
                }
        document = self.load(
            role=effective,
            stages=selected,
            record=False,
        )
        try:
            result = grogu_plandoc_compile.compile_checked(document, spec)
            grogu_plandoc_compile.write_cache(
                self.package, result["ir"], spec, format=format
            )
        except (
            grogu_plandoc_compile.CompileError,
            grogu_plandoc_schema.SchemaError,
        ) as error:
            field = getattr(error, "field", "")
            detail = f" ({field})" if field else ""
            raise PlanError(f"projection is not equivalent{detail}: {error}") from error
        return {
            "plan": self.plan_id,
            "revision": head,
            "role": effective,
            "stages": selected,
            "include": include,
            "budget": budget,
            "format": format,
            "etag": result["ir"].projection_digest,
            "payload": (
                result["markdown"] if format == "md" else result["json"]
            ),
            "identities": result["identities"],
            "elided": [
                {"id": item.id, "kind": item.kind, "detail": item.detail}
                for item in result["ir"].elided
            ],
            "cached": False,
        }

    def verify(self, *, role: str) -> dict:
        effective = self._claim(role)
        head = self.head(repair=False)
        manifest = self.plans.load(self.plan_id)
        try:
            integrity = self._verify_integrity(manifest)
        except (
            grogu_plandoc_revision.RevisionError,
            FileNotFoundError,
        ) as error:
            raise PlanError(f"package integrity failed: {error}") from error
        stages = [
            stage
            for stage in manifest.get("stages", [])
            if stage in ROLE_READABLE_STAGES.get(effective, ())
        ]
        projections = {}
        for stage in stages:
            try:
                actual = grogu_plandoc_revision.safe_read(
                    self._stage_path(stage)
                )
            except FileNotFoundError as error:
                raise PlanError(f"compiled artifact is missing for {stage}") from error
            try:
                markdown = (
                    unseal(actual.decode("utf8"))
                    if self._stage_path(stage).suffix == ".sealed"
                    else actual.decode("utf8")
                )
                parsed = grogu_plandoc_compile.parse(markdown)
                source_revision = parsed.provenance.revision
                document = self.load(
                    role=effective,
                    revision=source_revision,
                    stages=[stage],
                    record=False,
                )
                # The tracked artifact is the reviewer projection. Recompile
                # its stated source revision, not HEAD: an unrelated revision
                # does not make an unchanged stage projection stale.
                spec = self._projection_spec(REVIEWER, [stage])
                result = grogu_plandoc_compile.compile_checked(document, spec)
                expected = (
                    result["markdown"].encode("utf8")
                    if self._stage_path(stage).suffix == ".md"
                    else self._artifact_bytes(stage, result["markdown"])
                )
            except (
                UnicodeDecodeError,
                ValueError,
                grogu_plandoc_compile.CompileError,
            ) as error:
                raise PlanError(
                    f"compiled {stage} artifact is invalid: {error}"
                ) from error
            if actual != expected:
                raise PlanError(
                    f"compiled {stage} artifact differs from a fresh compile "
                    f"of its stated revision {source_revision}; run "
                    f"`grogu plan doc compile {self.plan_id}`"
                )
            projections[stage] = result["identities"]
        return {
            **integrity,
            "plan": self.plan_id,
            "role": effective,
            "projections": projections,
            "recoveries": list(self.last_recovery),
        }

    def _verify_integrity(self, manifest: dict) -> dict:
        raw_partitions = {
            relative: grogu_plandoc_revision.safe_read(self.package / relative)
            for relative in self._PARTITION_PATHS.values()
        }
        object_metadata = {
            relative: copy.deepcopy(
                manifest.get("plandoc", {})
                .get("partition_state", {})
                .get(relative, {})
                .get("objects", {})
            )
            for relative in raw_partitions
        }
        return grogu_plandoc_revision.verify_package(
            self.package,
            materialized=self._storage_state(
                raw_partitions,
                object_metadata=object_metadata,
            ),
        )

    def compile(
        self,
        *,
        role: str,
        stages=None,
        check: bool = False,
    ) -> dict:
        effective = self._claim(role)
        manifest = self.plans.load(self.plan_id)
        selected = [
            stage
            for stage in (stages or manifest.get("stages", []))
            if stage in manifest.get("stages", [])
            and stage in ROLE_READABLE_STAGES.get(effective, ())
        ]
        if stages and set(stages) - set(selected):
            raise PlanError(
                f"role {effective!r} cannot compile every requested stage"
            )
        if check:
            verified = self.verify(role=effective)
            return {
                "plan": self.plan_id,
                "revision": verified["head"],
                "role": effective,
                "checked": True,
                "changed": [],
                "ok": True,
                "identities": verified["projections"],
            }
        document = self.load(role=effective, stages=selected, record=False)
        changed = []
        results = {}
        for stage in selected:
            spec = self._projection_spec(REVIEWER, [stage])
            result = grogu_plandoc_compile.compile_checked(document, spec)
            path = self._stage_path(stage)
            payload = (
                result["markdown"].encode("utf8")
                if path.suffix == ".md"
                else self._artifact_bytes(stage, result["markdown"])
            )
            try:
                current = grogu_plandoc_revision.safe_read(path)
            except FileNotFoundError:
                current = b""
            if current != payload:
                changed.append(stage)
                if not check:
                    grogu_plandoc_revision.atomic_write(path, payload)
            if not check:
                grogu_plandoc_compile.write_cache(
                    self.package, result["ir"], spec, format="md"
                )
                grogu_plandoc_compile.write_cache(
                    self.package, result["ir"], spec, format="json"
                )
            results[stage] = result["identities"]
        return {
            "plan": self.plan_id,
            "revision": self.head(repair=True),
            "role": effective,
            "checked": bool(check),
            "changed": changed,
            "ok": not (check and changed),
            "identities": results,
        }

    # -- writes and history ----------------------------------------------

    @staticmethod
    def _reserved_ids(operations) -> tuple[set[str], set[str]]:
        added: set[str] = set()
        removed: set[str] = set()
        pattern = re.compile(r"^/(?:nodes|edges)/([^/]+)$")
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            path = str(operation.get("path", ""))
            match = pattern.fullmatch(path)
            if not match:
                continue
            if operation.get("op") == "add":
                added.add(match.group(1))
            elif operation.get("op") == "remove":
                removed.add(match.group(1))
        return added, removed

    @staticmethod
    def _changed_ids(operations) -> list[str]:
        changed = set()
        pattern = re.compile(r"^/(?:nodes|edges)/([^/]+)")
        for operation in operations:
            for key in ("path", "from"):
                match = pattern.match(str(operation.get(key, "")))
                if match:
                    changed.add(match.group(1))
        return sorted(changed, key=grogu_plandoc_canon.id_sort_key)

    @staticmethod
    def _object_stages(document: dict, bucket: str, identifier: str) -> list[str]:
        if bucket == "nodes":
            node = document.get("nodes", {}).get(identifier)
            return [str(node.get("stage", ""))] if isinstance(node, dict) else []
        edge = document.get("edges", {}).get(identifier)
        if not isinstance(edge, dict):
            return []
        return sorted(
            {
                str(document["nodes"][endpoint].get("stage", ""))
                for endpoint in (edge.get("from"), edge.get("to"))
                if endpoint in document.get("nodes", {})
            }
        )

    @classmethod
    def _operation_visibility(
        cls,
        before: dict,
        after: dict,
        operations,
    ) -> dict:
        entries = []
        for operation in operations:
            stages = set()
            identifiers = []
            for key in ("path", "from"):
                match = re.match(
                    r"^/(nodes|edges)/([^/]+)",
                    str(operation.get(key, "")),
                )
                if not match:
                    continue
                bucket, identifier = match.groups()
                identifiers.append(identifier)
                stages.update(cls._object_stages(after, bucket, identifier))
                stages.update(cls._object_stages(before, bucket, identifier))
            entries.append(
                {
                    "stages": sorted(stages),
                    "objects": sorted(
                        set(identifiers),
                        key=grogu_plandoc_canon.id_sort_key,
                    ),
                }
            )
        return {"schema_version": 1, "ops": entries}

    def _revision_meta_path(self, revision: str) -> Path:
        return self.package / "revision-meta" / f"{revision}.json"

    def _read_revision_meta(self, revision: str) -> dict:
        try:
            value = self._read_json_file(self._revision_meta_path(revision))
        except PlanError:
            return {}
        entries = value.get("ops", [])
        if not isinstance(entries, list):
            return {}
        return value

    @staticmethod
    def _stamp_revision(before: dict, after: dict, revision: str) -> dict:
        stamped = copy.deepcopy(after)
        stamped["revision"] = revision
        for node_id, node in stamped["nodes"].items():
            previous = before.get("nodes", {}).get(node_id)
            if previous is None:
                node["created_rev"] = revision
                node["updated_rev"] = revision
            elif grogu_plandoc_canon.digest(previous) != grogu_plandoc_canon.digest(node):
                node["created_rev"] = previous["created_rev"]
                node["updated_rev"] = revision
        for edge_id, edge in stamped["edges"].items():
            previous = before.get("edges", {}).get(edge_id)
            if previous is None:
                edge["created_rev"] = revision
            elif "created_rev" not in edge:
                edge["created_rev"] = previous["created_rev"]
        return grogu_plandoc_schema.validate_document(stamped)

    @staticmethod
    def _partitions_touched(before: dict, after: dict, changed_ids) -> set[str]:
        partitions = set()
        for document in (before, after):
            split = grogu_plandoc.split_partitions(document)
            for name, partition in split.items():
                if any(
                    identifier in partition.get(bucket, {})
                    for identifier in changed_ids
                    for bucket in ("nodes", "edges")
                ):
                    partitions.add(name)
        return partitions

    @staticmethod
    def _stages_touched(before: dict, after: dict, changed_ids, manifest: dict) -> list[str]:
        stages = set()
        plan_level = False
        for identifier in changed_ids:
            for document in (before, after):
                node = document.get("nodes", {}).get(identifier)
                if node:
                    stage = node.get("stage", "")
                    plan_level = plan_level or not stage
                    if stage:
                        stages.add(stage)
                edge = document.get("edges", {}).get(identifier)
                if edge:
                    for endpoint in (edge.get("from"), edge.get("to")):
                        endpoint_node = document.get("nodes", {}).get(endpoint)
                        if endpoint_node and endpoint_node.get("stage"):
                            stages.add(endpoint_node["stage"])
        if plan_level:
            stages.update(manifest.get("stages", []))
        return [stage for stage in STAGES if stage in stages]

    def _current_partition_payloads(self) -> dict[str, bytes]:
        values = {}
        for relative in self._PARTITION_PATHS.values():
            try:
                values[relative] = grogu_plandoc_revision.safe_read(
                    self.package / relative
                )
            except FileNotFoundError as error:
                raise PlanError(f"missing plan partition {relative}") from error
        return values

    def patch(
        self,
        *,
        role: str,
        base: str,
        operations,
        intent: str = "",
        origin: str = "cli",
        dry_run: bool = False,
        extra_artifacts: Optional[dict[str, bytes]] = None,
        proposal_guard: str = "",
    ) -> dict:
        effective = self._claim(role)
        operations = list(operations or [])
        if len(operations) > self.MAX_PATCH_OPS:
            raise PlanError(
                f"patch has {len(operations)} operations; maximum is "
                f"{self.MAX_PATCH_OPS}"
            )
        before = self.load(role=effective, record=False)
        current = before["revision"]
        if base and base != current:
            error = grogu_plandoc_patch.StaleRevision(base, current)
            error.ops_since = self.ops_since(base, role=effective)
            raise error
        added, removed = self._reserved_ids(operations)
        manifest = self.plans.load(self.plan_id)
        visible_ids = set(before.get("nodes", {})) | set(
            before.get("edges", {})
        )
        hidden_collisions = sorted(
            (
                added
                & self._all_manifest_object_ids(manifest)
                - visible_ids
            ),
            key=grogu_plandoc_canon.id_sort_key,
        )
        if hidden_collisions:
            # Do not say which partition owns the id. The caller learns only
            # that the globally unique id is unavailable.
            raise PlanError(
                "one or more requested object ids are already in use"
            )
        readable = ROLE_READABLE_STAGES[effective]
        # This call deliberately precedes schema validation and application.
        # It examines pointer paths and the minimum stage metadata needed to
        # refuse an unreadable target before a parser can walk its value.
        grogu_plandoc_patch.assert_patch_authorized(
            before,
            operations,
            readable,
            authorized_new_ids=added,
            authorized_remove_ids=removed,
        )
        try:
            applied = grogu_plandoc_patch.apply_patch(
                before,
                operations,
                base=base or current,
                current_revision=current,
                readable_stages=readable,
                authorized_new_ids=added,
                authorized_remove_ids=removed,
            )
        except grogu_plandoc_patch.PatchError:
            raise
        next_seq = int(current[1:]) + 1
        revision = grogu_plandoc_revision.revision_id(next_seq)
        after = self._stamp_revision(before, applied, revision)
        # Re-run authorization over the validated result so a move or nested
        # replacement cannot smuggle a selector across the seal.
        grogu_plandoc_patch.assert_patch_authorized(
            after,
            operations,
            readable,
            authorized_new_ids=added,
            authorized_remove_ids=removed,
            after=True,
        )
        relationship_metadata = self._manifest_relationship_metadata(
            manifest
        )
        object_metadata = self._manifest_object_metadata(manifest)
        removed_nodes = {
            node_id
            for node_id in before.get("nodes", {})
            if node_id not in after.get("nodes", {})
        }
        repartitioned_nodes = {
            node_id
            for node_id in before.get("nodes", {})
            if node_id in after.get("nodes", {})
            and before["nodes"][node_id].get("stage", "")
            != after["nodes"][node_id].get("stage", "")
        }
        for relative, relationships in relationship_metadata.items():
            for edge_id, endpoints in relationships.items():
                endpoint_set = set(endpoints)
                edge_stages = set(
                    object_metadata.get(relative, {}).get(edge_id, [])
                )
                if endpoint_set & repartitioned_nodes:
                    raise PlanError(
                        "move or remove relationships before changing a "
                        "connected node's stage"
                    )
                if (
                    endpoint_set & removed_nodes
                    and edge_stages - (set(readable) | {""})
                ):
                    raise PlanError(
                        f"role {effective!r} cannot remove an object that is "
                        "referenced outside that role's view"
                    )
        changed = self._changed_ids(operations)
        touched_partitions = self._partitions_touched(
            before, after, changed
        )
        if (
            grogu_plandoc.OPEN_PARTITION in touched_partitions
            and not {DESIGN, IMPLEMENTATION}.issubset(readable)
        ):
            raise PlanError(
                f"role {effective!r} cannot rewrite the shared open graph "
                "partition without access to both design and implementation"
            )
        allowed_stages = set(readable) | {""}
        manifest_objects = self._manifest_object_metadata(manifest)
        if any(
            set(stages) - allowed_stages
            for partition in touched_partitions
            for stages in manifest_objects.get(
                self._PARTITION_PATHS[partition], {}
            ).values()
        ):
            # A role-bounded materialization omits cross-seal relationships.
            # Re-serializing that partial partition would silently delete
            # them, while decoding them here would expose hidden ids. Refuse
            # the write and require a role that can see the whole partition.
            raise PlanError(
                f"role {effective!r} cannot rewrite a graph partition that "
                "contains relationships outside that role's view"
            )
        affected_stages = [
            stage
            for stage in self._stages_touched(
                before, after, changed, manifest
            )
            if stage in readable
        ]
        checks = {}
        stage_artifacts = {}
        for stage in affected_stages:
            result = grogu_plandoc_compile.compile_checked(
                after, self._projection_spec(REVIEWER, [stage])
            )
            stage_artifacts[self._stage_relative(stage)] = self._artifact_bytes(
                stage, result["markdown"]
            )
            checks[stage] = result
        if dry_run:
            return {
                "plan": self.plan_id,
                "base": current,
                "revision": revision,
                "changed": changed,
                "stages": affected_stages,
                "digest": grogu_plandoc.document_digest(after),
                "dry_run": True,
                "identities": {
                    stage: result["identities"]
                    for stage, result in checks.items()
                },
            }

        with self.plans.locked():
            if proposal_guard:
                guarded = self._read_json_file(
                    self._proposal_path(proposal_guard)
                )
                if guarded.get("status") != "pending":
                    raise PlanError(
                        f"proposal {proposal_guard} is "
                        f"{guarded.get('status', 'unknown')}"
                    )
            locked_head = self.head(repair=False)
            if locked_head != current:
                error = grogu_plandoc_patch.StaleRevision(current, locked_head)
                # We already hold the plan lock; deriving a role-filtered
                # rebase set would re-enter role binding and the same lock.
                # An empty set is safe and tells the client to refresh.
                error.ops_since = []
                raise error
            latest_manifest = self.plans.load(self.plan_id)
            fresh_hidden_collisions = sorted(
                (
                    added
                    & self._all_manifest_object_ids(latest_manifest)
                    - visible_ids
                ),
                key=grogu_plandoc_canon.id_sort_key,
            )
            if fresh_hidden_collisions:
                raise PlanError(
                    "one or more requested object ids are already in use"
                )
            current_payloads = self._current_partition_payloads()
            visible_partitions = grogu_plandoc.split_partitions(after)
            touched = touched_partitions
            before_metadata = self._manifest_object_metadata(latest_manifest)
            after_metadata = copy.deepcopy(before_metadata)
            before_relationships = self._manifest_relationship_metadata(
                latest_manifest
            )
            after_relationships = copy.deepcopy(before_relationships)
            visible_metadata = self._partition_object_metadata(
                after, visible_partitions
            )
            visible_relationships = self._partition_relationship_metadata(
                visible_partitions
            )
            for identifier in changed:
                for objects in after_metadata.values():
                    objects.pop(identifier, None)
                for relative, objects in visible_metadata.items():
                    if identifier in objects:
                        after_metadata.setdefault(relative, {})[
                            identifier
                        ] = copy.deepcopy(objects[identifier])
            changed_edges = {
                identifier
                for identifier in changed
                if identifier in before.get("edges", {})
                or identifier in after.get("edges", {})
            }
            for edge_id in changed_edges:
                for relationships in after_relationships.values():
                    relationships.pop(edge_id, None)
                for relative, relationships in visible_relationships.items():
                    if edge_id in relationships:
                        after_relationships.setdefault(relative, {})[
                            edge_id
                        ] = copy.deepcopy(relationships[edge_id])
            for partition in touched:
                value = visible_partitions[partition]
                relative = self._PARTITION_PATHS[partition]
                current_payloads[relative] = (
                    self._open_bytes(value)
                    if partition == grogu_plandoc.OPEN_PARTITION
                    else self._sealed_bytes(value)
                )
            before_state = self._storage_state(
                self._current_partition_payloads(),
                object_metadata=before_metadata,
            )
            after_state = self._storage_state(
                current_payloads,
                object_metadata=after_metadata,
            )
            envelope = grogu_plandoc_revision.make_revision(
                before_state,
                after_state,
                seq=next_seq,
                actor=actor(),
                role=effective,
                agent=os.environ.get("GROGU_AGENT", "") or "grogu",
                intent=(intent or "apply plan document patch")[:1000],
                origin=(origin or "cli")[:128],
                ops=operations,
            )
            visibility = self._operation_visibility(
                before, after, operations
            )
            recovery = self._recovery_artifacts(
                revision,
                partitions=current_payloads,
                stage_artifacts={
                    stage: (
                        stage_artifacts.get(stage)
                        or grogu_plandoc_revision.safe_read(self.package / stage)
                    )
                    for stage in [
                        self._stage_relative(item)
                        for item in latest_manifest.get("stages", [])
                    ]
                },
            )
            artifacts = {
                **stage_artifacts,
                **recovery,
                f"revision-meta/{revision}.json": (
                    grogu_plandoc_canon.pretty_dumpb(visibility)
                ),
                **(extra_artifacts or {}),
            }
            grogu_plandoc_revision.write_generation(
                self.package,
                envelope,
                partitions=current_payloads,
                artifacts=artifacts,
                base=current,
                identity_checks=[
                    lambda result=result: grogu_plandoc_compile.verify_identities(
                        result["fragment"], result["ir"]
                    )
                    for result in checks.values()
                ],
            )
            plandoc_manifest = latest_manifest.setdefault("plandoc", {})
            plandoc_manifest["counters"] = self._merge_counters(
                plandoc_manifest.get("counters", {}),
                self._counter_state(after),
            )
            self._update_manifest_parts(
                latest_manifest, current_payloads, {
                    self._stage_relative(stage): grogu_plandoc_revision.safe_read(
                        self._stage_path(stage)
                    )
                    for stage in latest_manifest.get("stages", [])
                },
                envelope,
                object_metadata=after_metadata,
                relationship_metadata=after_relationships,
            )
            latest_manifest["updated_at"] = now()
            latest_manifest.setdefault("events", []).append(
                {
                    "at": now(),
                    "actor": actor(),
                    "event": "document_revision",
                    "revision": revision,
                    "origin": origin,
                }
            )
            self.plans._write_json(
                self.package / "manifest.json", latest_manifest
            )
        for stage, result in checks.items():
            spec = self._projection_spec(REVIEWER, [stage])
            grogu_plandoc_compile.write_cache(
                self.package, result["ir"], spec, format="md"
            )
            grogu_plandoc_compile.write_cache(
                self.package, result["ir"], spec, format="json"
            )
        return {
            "plan": self.plan_id,
            "revision": revision,
            "digest": envelope["after_digest"],
            "changed": changed,
            "stages": affected_stages,
            "dry_run": False,
            "identities": {
                stage: result["identities"] for stage, result in checks.items()
            },
        }

    def replace_stage(
        self,
        stage: str,
        body: str,
        *,
        role: str,
        replace: bool = False,
    ) -> dict:
        """Import a legacy stage write into the graph, then recompile it."""
        effective = self._claim(role)
        if effective not in STAGE_WRITERS.get(stage, frozenset()):
            raise PlanError(
                f"role {effective!r} may not write the {stage} stage"
            )
        manifest = self.plans.load(self.plan_id)
        if stage not in manifest.get("stages", []):
            raise PlanError(f"plan {self.plan_id} has no {stage} stage")
        if not body.strip():
            raise PlanError("refusing to write an empty plan stage")
        padding = padded_body(body)
        if padding:
            raise PlanError(f"refusing to write the {stage} stage: {padding}")
        if stage == DESIGN:
            missing = missing_design_sections(body)
            if missing:
                raise PlanError(
                    "the design spec is missing required sections: "
                    + ", ".join(missing)
                )
            unfilled = unfilled_design_sections(body)
            if unfilled:
                raise PlanError(
                    "these design sections still hold the template's own "
                    "instructions: "
                    + ", ".join(unfilled)
                )
            hollow = hollow_design_sections(body)
            if hollow:
                raise PlanError(
                    "these design sections say nothing a tester could check: "
                    + ", ".join(hollow)
                )
            vague = vague_design_terms(body)
            if vague:
                raise PlanError(
                    "the design spec leans on adjectives instead of decisions: "
                    + ", ".join(sorted(vague))
                )
        state = manifest.get("stage_state", {}).get(stage)
        previous = self.plans.read_stage(
            self.plan_id, stage, role=effective, record=False
        )
        if state == COMPLETE and not replace:
            raise PlanError(
                f"the {stage} stage is complete and this rewrites it "
                f"({len(previous)} bytes -> imported source {len(body)} bytes). "
                "Pass --replace if you mean it; the compiled text you replace "
                "is kept either way."
            )
        before = self.load_all(role=effective)
        working = copy.deepcopy(before)
        removed_nodes = {
            node_id
            for node_id, node in working["nodes"].items()
            if node.get("stage") == stage and node.get("kind") != "thread"
        }
        working["edges"] = {
            edge_id: edge
            for edge_id, edge in working["edges"].items()
            if edge.get("from") not in removed_nodes
            and edge.get("to") not in removed_nodes
        }
        for node_id in removed_nodes:
            working["nodes"].pop(node_id, None)
        imported = grogu_plandoc_compile.import_markdown(
            body,
            plan_id=self.plan_id,
            stage=stage,
            revision=before["revision"],
        )
        manifest_copy = copy.deepcopy(manifest)
        manifest_copy.setdefault("plandoc", {}).setdefault(
            "counters", self._counter_state(working)
        )
        self._merge_imported(working, imported, manifest_copy)
        self._sync_manifest_directives(
            working, manifest_copy, stage=stage
        )
        self._reanchor_graph_threads(
            working,
            stage=stage,
            removed_nodes=removed_nodes,
        )
        working = grogu_plandoc_schema.validate_document(working)
        operations = grogu_plandoc_patch.diff(before, working)
        if operations:
            result = self.patch(
                role=effective,
                base=before["revision"],
                operations=operations,
                intent=f"import and compile {stage} stage",
                origin="legacy-stage-adapter",
            )
        else:
            result = {
                "revision": before["revision"],
                "changed": [],
                "digest": grogu_plandoc.document_digest(before),
            }
        compiled = self.plans.read_stage(
            self.plan_id, stage, role=effective, record=False
        )
        warnings = []
        with self.plans.locked():
            latest = self.plans.load(self.plan_id)
            revision_number = 0
            if previous and previous != compiled:
                revision_number = self.plans._keep_revision(
                    self.plan_id,
                    stage,
                    (
                        seal(previous)
                        if stage in SEALED_STAGES
                        else previous
                    ),
                    latest,
                    plain_bytes=len(previous),
                )
            latest.setdefault("stage_written", {})[stage] = True
            latest["last_write"] = {
                "stage": stage,
                "by": effective,
                "at": now(),
                "was": len(previous),
                "now": len(compiled),
                "revision": revision_number,
                "document_revision": result["revision"],
                "source_bytes": len(body),
            }
            if state == COMPLETE and previous != compiled:
                latest.setdefault("stage_state", {})[stage] = PENDING
                if stage == IMPLEMENTATION:
                    for name, value in list(
                        latest.setdefault("workstream_state", {}).items()
                    ):
                        if value == COMPLETE:
                            latest["workstream_state"][name] = PENDING
            for amendment in latest.get("amendments", []):
                if (
                    amendment.get("status") == ACCEPTED
                    and amendment.get("stage") == stage
                    and not amendment.get("incorporated")
                    and previous != compiled
                ):
                    amendment["incorporated"] = True
                    amendment["incorporated_at"] = now()
            latest["updated_at"] = now()
            latest.setdefault("events", []).append(
                {
                    "at": now(),
                    "actor": actor(),
                    "event": "stage_written",
                    "stage": stage,
                    "bytes": len(compiled),
                    "source_bytes": len(body),
                    "revision": result["revision"],
                }
            )
            self.plans._write_json(self.package / "manifest.json", latest)
        returned = self.plans.load(self.plan_id)
        returned["warnings"] = warnings
        returned["document_write"] = result
        return returned

    @staticmethod
    def _sync_manifest_directives(
        document: dict,
        manifest: dict,
        *,
        stage: str,
    ) -> None:
        origins = {
            str(node.get("attrs", {}).get("origin", ""))
            for node in document["nodes"].values()
            if node.get("kind") == "directive"
        }
        candidates = []
        for amendment in manifest.get("amendments", []):
            if (
                amendment.get("status") == ACCEPTED
                and (amendment.get("stage") or IMPLEMENTATION) == stage
            ):
                candidates.append(
                    (
                        f"amendment:{amendment.get('id', '')}",
                        f"Accepted amendment {amendment.get('id', '')}",
                        str(amendment.get("claim", "")),
                        ["everyone"],
                    )
                )
        if stage == IMPLEMENTATION:
            for note in manifest.get("steering", []):
                if note.get("retracted") or note.get("withdrawn"):
                    continue
                target = str(note.get("role", "all"))
                audience = ["everyone"] if target == "all" else [target]
                candidates.append(
                    (
                        f"steering:{note.get('seq', 0)}",
                        f"Steering #{note.get('seq', 0)}",
                        str(note.get("text", "")),
                        audience,
                    )
                )
        counter_manifest = {
            "plandoc": {
                "counters": manifest.setdefault("plandoc", {}).setdefault(
                    "counters", PlanDocumentStore._counter_state(document)
                )
            }
        }
        for origin, title, body, audience in candidates:
            if not origin or origin in origins or not body.strip():
                continue
            directive_id = grogu_plandoc.allocate_id(
                counter_manifest,
                "directive",
                existing_ids=document["nodes"],
            )
            document["nodes"][directive_id] = grogu_plandoc.make_node(
                directive_id,
                "directive",
                title[:256],
                stage=stage,
                body=body,
                attrs={
                    "binding": "must",
                    "audience": audience,
                    "status": "active",
                    "origin": origin,
                },
                order=500 + len(document["nodes"]),
                revision=document["revision"],
            )
            origins.add(origin)

    def add_stage(self, stage: str, *, role: str) -> dict:
        effective = self._claim(role)
        if effective != ARCHITECT:
            raise PlanError("only the architect may add a plan stage")
        if stage not in {DESIGN, EVALUATION}:
            raise PlanError(f"{stage} is not an optional plan stage")
        document = self.load_all(role=effective)
        with self.plans.locked():
            manifest = self.plans.load(self.plan_id)
            if stage in manifest.get("stages", []):
                raise PlanError(f"plan {self.plan_id} already has a {stage} stage")
            manifest["stages"] = [
                item
                for item in STAGES
                if item in manifest.get("stages", []) or item == stage
            ]
            manifest.setdefault("stage_state", {})[stage] = PENDING
            manifest.setdefault("stage_written", {})[stage] = False
            manifest.get("declined_stages", {}).pop(stage, None)
            spec = self._projection_spec(REVIEWER, [stage])
            result = grogu_plandoc_compile.compile_checked(document, spec)
            payload = self._artifact_bytes(stage, result["markdown"])
            grogu_plandoc_revision.atomic_write(self._stage_path(stage), payload)
            head = self.head(repair=False)
            grogu_plandoc_revision.atomic_write(
                self._stage_path(stage, revision=head), payload
            )
            manifest["updated_at"] = now()
            manifest.setdefault("events", []).append(
                {"at": now(), "actor": actor(), "event": "stage_added", "stage": stage}
            )
            self.plans._write_json(self.package / "manifest.json", manifest)
        grogu_plandoc_compile.write_cache(
            self.package, result["ir"], spec, format="md"
        )
        grogu_plandoc_compile.write_cache(
            self.package, result["ir"], spec, format="json"
        )
        return self.plans.load(self.plan_id)

    def reset_stage(self, stage: str, *, role: str) -> dict:
        effective = self._claim(role)
        if effective != ARCHITECT:
            raise PlanError("only the architect may reset a plan stage")
        manifest = self.plans.load(self.plan_id)
        if stage not in manifest.get("stages", []):
            raise PlanError(f"plan {self.plan_id} has no {stage} stage")
        document = self.load_all(role=effective)
        node_ids = {
            node_id
            for node_id, node in document["nodes"].items()
            if node.get("stage") == stage
        }
        operations = [
            {"op": "remove", "path": f"/edges/{edge_id}"}
            for edge_id, edge in document["edges"].items()
            if edge.get("from") in node_ids or edge.get("to") in node_ids
        ] + [
            {"op": "remove", "path": f"/nodes/{node_id}"}
            for node_id in sorted(node_ids, key=grogu_plandoc_canon.id_sort_key)
        ]
        if operations:
            result = self.patch(
                role=effective,
                base=document["revision"],
                operations=operations,
                intent=f"reset {stage} stage",
                origin="stage-reset",
            )
        else:
            result = {"revision": document["revision"]}
        with self.plans.locked():
            latest = self.plans.load(self.plan_id)
            latest.setdefault("stage_written", {})[stage] = False
            latest.setdefault("stage_state", {})[stage] = PENDING
            latest["updated_at"] = now()
            latest.setdefault("events", []).append(
                {
                    "at": now(),
                    "actor": actor(),
                    "event": "stage_reset",
                    "stage": stage,
                    "revision": result["revision"],
                }
            )
            self.plans._write_json(self.package / "manifest.json", latest)
        return self.plans.load(self.plan_id)

    @staticmethod
    def _reanchor_graph_threads(
        document: dict,
        *,
        stage: str,
        removed_nodes: set[str],
    ) -> None:
        candidates = [
            node
            for node in document["nodes"].values()
            if node.get("stage") == stage and node.get("kind") != "thread"
        ]
        for thread in document["nodes"].values():
            if thread.get("kind") != "thread" or thread.get("stage") != stage:
                continue
            attrs = thread.get("attrs", {})
            selector = attrs.get("selector", {})
            referenced = set(grogu_plandoc_schema.selector_node_ids(selector))
            if not referenced & removed_nodes:
                continue
            exact = ""
            if selector.get("type") == "text":
                exact = str(selector.get("quote", {}).get("exact", ""))
            matches = []
            if exact:
                for candidate in candidates:
                    start = candidate.get("body", "").find(exact)
                    if start >= 0 and candidate["body"].find(
                        exact, start + max(1, len(exact))
                    ) < 0:
                        matches.append((candidate, start))
            if len(matches) == 1:
                candidate, start = matches[0]
                attrs["selector"] = {
                    "type": "text",
                    "node": candidate["id"],
                    "quote": {
                        "exact": exact,
                        "prefix": candidate["body"][
                            max(0, start - 32) : start
                        ],
                        "suffix": candidate["body"][
                            start + len(exact) : start + len(exact) + 32
                        ],
                    },
                    "position": {
                        "start": start,
                        "end": start + len(exact),
                    },
                    "body_digest": grogu_plandoc_anchor.digest(
                        candidate["body"]
                    ),
                }
                attrs["anchor_state"] = "shifted"
            else:
                attrs["anchor_state"] = "orphaned"

    def revisions(self, *, role: str) -> list[dict]:
        effective = self._claim(role)
        values = []
        for envelope in grogu_plandoc_revision.list_revisions(self.package):
            item = copy.deepcopy(envelope)
            visible_ops, complete = self._visible_ops(item, effective)
            item["ops"] = visible_ops
            if not complete:
                item["intent"] = "revision includes changes outside this role's view"
            values.append(item)
        return values

    def _visible_ops(self, envelope: dict, role: str) -> tuple[list[dict], bool]:
        operations = envelope.get("ops", [])
        metadata = self._read_revision_meta(str(envelope.get("revision", "")))
        entries = metadata.get("ops", []) if isinstance(metadata, dict) else []
        if len(entries) != len(operations):
            # A package predating revision visibility metadata fails closed.
            return [], not operations
        allowed = set(ROLE_READABLE_STAGES.get(role, ())) | {""}
        result = []
        for operation, entry in zip(operations, entries):
            stages = set(entry.get("stages", [])) if isinstance(entry, dict) else set()
            if stages <= allowed:
                result.append(copy.deepcopy(operation))
        return result, len(result) == len(operations)

    def ops_since(self, revision: str, *, role: str) -> list[dict]:
        if not re.fullmatch(r"r[0-9]{4,}", revision or ""):
            return []
        start = int(revision[1:])
        operations = []
        for envelope in grogu_plandoc_revision.list_revisions(self.package):
            if int(envelope["seq"]) > start:
                visible, _complete = self._visible_ops(envelope, role)
                operations.extend(visible)
        return operations

    def diff(self, *, role: str, before: str, after: str) -> dict:
        effective = self._claim(role)
        left = self.load(role=effective, revision=before, record=False)
        right = self.load(role=effective, revision=after, record=False)
        return {
            "plan": self.plan_id,
            "from": before,
            "to": after,
            "ops": grogu_plandoc_patch.diff(left, right),
        }

    def query(
        self,
        *,
        role: str,
        stage: str = "",
        kind: str = "",
        text: str = "",
        identifier: str = "",
    ) -> dict:
        effective = self._claim(role)
        stages = [stage] if stage else None
        document = self.load(
            role=effective, stages=stages, record=False
        )
        folded = text.casefold()
        nodes = [
            copy.deepcopy(node)
            for node in sorted(
                document["nodes"].values(), key=grogu_plandoc.node_sort_key
            )
            if (not kind or node["kind"] == kind)
            and (not identifier or node["id"] == identifier)
            and (
                not folded
                or folded
                in f"{node['id']} {node['title']} {node['body']}".casefold()
            )
        ]
        edges = [
            copy.deepcopy(edge)
            for edge in sorted(
                document["edges"].values(), key=grogu_plandoc.edge_sort_key
            )
            if (not kind or edge["kind"] == kind)
            and (not identifier or edge["id"] == identifier)
            and (
                not folded
                or folded
                in (
                    f"{edge['id']} {edge['kind']} "
                    f"{edge['from']} {edge['to']}"
                ).casefold()
            )
        ]
        return {
            "plan": self.plan_id,
            "revision": document["revision"],
            "role": effective,
            "nodes": nodes,
            "edges": edges,
        }

    def export(
        self,
        *,
        role: str,
        stages=None,
        include: str = "normative",
        budget: Optional[int] = None,
        since: str = "",
        format: str = "md",
        output: Optional[Path] = None,
    ) -> dict:
        result = self.projection(
            role=role,
            stages=stages,
            include=include,
            budget=budget,
            since=since,
            format=format,
        )
        if output is not None:
            target = Path(output).expanduser().resolve()
            package = self.package.resolve()
            if target == package or package in target.parents:
                raise PlanError(
                    "refusing to export inside the .plan package; compiled "
                    "parts are written only by the compiler"
                )
            payload = result["payload"]
            data = (
                payload.encode("utf8")
                if isinstance(payload, str)
                else grogu_plandoc_canon.pretty_dumpb(payload)
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            grogu_plandoc_revision.atomic_write(target, data)
            result["output"] = str(target)
        return result

    # -- comments, directives, and proposals ----------------------------

    @staticmethod
    def _comment_html(body: str) -> str:
        return grogu_markdown.render_document(body)["html"]

    @staticmethod
    def _thread_dto(node: dict) -> dict:
        attrs = node.get("attrs", {})
        comments = []
        for comment in attrs.get("comments", []):
            item = copy.deepcopy(comment)
            item["body_html"] = PlanDocumentStore._comment_html(
                str(item.get("body", ""))
            )
            item.setdefault("role", "")
            comments.append(item)
        selector = copy.deepcopy(attrs.get("selector", {}))
        return {
            "id": node["id"],
            "legacy_id": attrs.get("legacy_id", ""),
            "selector": selector,
            "kind": attrs.get("thread_kind", "discussion"),
            "status": attrs.get("status", "open"),
            "anchor_state": attrs.get("anchor_state", "resolved"),
            "anchor_last_exact": attrs.get("anchor_last_exact", ""),
            "resolved_members": attrs.get("resolved_members"),
            "total_members": attrs.get("total_members"),
            "quote_context": attrs.get("quote_context", ""),
            "round": attrs.get("round", 1),
            "comments": comments,
            "promoted_directive": attrs.get("promoted_directive", ""),
            "stage": node.get("stage", ""),
            "created_at": attrs.get("created_at", ""),
            "resolved_at": attrs.get("resolved_at", ""),
            "resolved_by": attrs.get("resolved_by", ""),
            "anchor_history": copy.deepcopy(attrs.get("anchor_history", [])),
        }

    def threads(
        self,
        *,
        role: str,
        stage: str = "",
        status: str = "",
    ) -> list[dict]:
        effective = self._claim(role)
        document = self.load(
            role=effective,
            stages=[stage] if stage else None,
            record=False,
        )
        values = [
            self._thread_dto(node)
            for node in sorted(
                document["nodes"].values(), key=grogu_plandoc.node_sort_key
            )
            if node.get("kind") == "thread"
            and (not stage or node.get("stage") == stage)
            and (not status or node.get("attrs", {}).get("status") == status)
        ]
        return values

    def _thread_node(self, document: dict, thread_id: str) -> dict:
        direct = document["nodes"].get(thread_id)
        if direct and direct.get("kind") == "thread":
            return direct
        for node in document["nodes"].values():
            if (
                node.get("kind") == "thread"
                and node.get("attrs", {}).get("legacy_id") == thread_id
            ):
                return node
        raise PlanError(f"review has no thread {thread_id!r}")

    def add_thread(
        self,
        *,
        role: str,
        selector: dict,
        body: str,
        kind: str = "discussion",
        legacy_id: str = "",
    ) -> dict:
        effective = self._claim(role)
        if kind not in {
            "discussion",
            "question",
            "suggestion",
            "blocking",
            "directive",
        }:
            raise PlanError(f"unknown thread kind {kind!r}")
        body = str(body)
        if not body.strip():
            raise PlanError("comment body may not be empty")
        if len(body) > 8000:
            raise PlanError("comment body exceeds 8000 characters")
        document = self.load(role=effective, record=False)
        normalized = grogu_plandoc_schema.validate_selector(selector)
        resolution = grogu_plandoc.resolve_selector(document, normalized)
        node_ids = resolution.get("nodes", [])
        stage = ""
        if node_ids:
            stage = document["nodes"][node_ids[0]].get("stage", "")
        manifest = self.plans.load(self.plan_id)
        counter_manifest = {
            "plandoc": {
                "counters": copy.deepcopy(
                    manifest.get("plandoc", {}).get(
                        "counters", self._counter_state(document)
                    )
                )
            }
        }
        thread_id = grogu_plandoc.allocate_id(
            counter_manifest,
            "thread",
            existing_ids=document["nodes"],
        )
        stamp = self._graph_timestamp()
        attrs = {
            "selector": normalized,
            "comments": [
                {
                    "id": f"{thread_id}-c1",
                    "at": stamp,
                    "author": actor(),
                    "role": effective,
                    "body": body,
                    "revision": document["revision"],
                }
            ],
            "status": "open",
            "anchor_state": resolution.get("state", "resolved"),
            "thread_kind": kind,
            "round": self._open_review_round(manifest, thread_id),
            "legacy_id": legacy_id,
            "created_at": stamp,
            "anchor_history": [],
        }
        node = grogu_plandoc.make_node(
            thread_id,
            "thread",
            f"{kind.title()} thread",
            stage=stage,
            attrs=attrs,
            order=2_000_000 + len(document["nodes"]),
            revision=document["revision"],
        )
        result = self.patch(
            role=effective,
            base=document["revision"],
            operations=[
                {"op": "add", "path": f"/nodes/{thread_id}", "value": node}
            ],
            intent=f"add {kind} thread",
            origin="review-adapter",
        )
        self._save_review_rounds(manifest)
        return self._thread_dto(
            self._thread_node(
                self.load(role=effective, record=False), thread_id
            )
        ) | {"revision": result["revision"]}

    @staticmethod
    def _open_review_round(manifest: dict, thread_id: str) -> int:
        rounds = manifest.setdefault("review_rounds", [])
        if not rounds or rounds[-1].get("state") != "open":
            rounds.append(
                {
                    "number": len(rounds) + 1,
                    "state": "open",
                    "opened_at": now(),
                    "requested_at": "",
                    "answered_at": "",
                    "steering_seq": 0,
                    "thread_ids": [],
                    "note": "",
                }
            )
        if thread_id not in rounds[-1]["thread_ids"]:
            rounds[-1]["thread_ids"].append(thread_id)
        return int(rounds[-1]["number"])

    def _save_review_rounds(self, source: dict) -> None:
        with self.plans.locked():
            manifest = self.plans.load(self.plan_id)
            manifest["review_rounds"] = copy.deepcopy(
                source.get("review_rounds", [])
            )
            self.plans._write_json(self.package / "manifest.json", manifest)

    def reply_thread(
        self,
        thread_id: str,
        body: str,
        *,
        role: str,
    ) -> dict:
        effective = self._claim(role)
        body = str(body)
        if not body.strip():
            raise PlanError("comment body may not be empty")
        document = self.load(role=effective, record=False)
        thread = self._thread_node(document, thread_id)
        comments = copy.deepcopy(thread["attrs"]["comments"])
        comments.append(
            {
                "id": f"{thread['id']}-c{len(comments) + 1}",
                "at": self._graph_timestamp(),
                "author": actor(),
                "role": effective,
                "body": body,
                "revision": document["revision"],
            }
        )
        self.patch(
            role=effective,
            base=document["revision"],
            operations=[
                {
                    "op": "replace",
                    "path": f"/nodes/{thread['id']}/attrs/comments",
                    "value": comments,
                }
            ],
            intent=f"reply to {thread['id']}",
            origin="review-adapter",
        )
        return self._thread_dto(
            self._thread_node(
                self.load(role=effective, record=False), thread["id"]
            )
        )

    def set_thread_status(
        self,
        thread_id: str,
        status: str,
        *,
        role: str,
        note: str = "",
    ) -> dict:
        effective = self._claim(role)
        if status not in {"open", "resolved"}:
            raise PlanError(f"invalid thread status {status!r}")
        document = self.load(role=effective, record=False)
        thread = self._thread_node(document, thread_id)
        operations = [
            {
                "op": "replace",
                "path": f"/nodes/{thread['id']}/attrs/status",
                "value": status,
            }
        ]
        if note.strip():
            comments = copy.deepcopy(thread["attrs"]["comments"])
            comments.append(
                {
                    "id": f"{thread['id']}-c{len(comments) + 1}",
                    "at": self._graph_timestamp(),
                    "author": actor(),
                    "role": effective,
                    "body": note.strip(),
                    "revision": document["revision"],
                }
            )
            operations.append(
                {
                    "op": "replace",
                    "path": f"/nodes/{thread['id']}/attrs/comments",
                    "value": comments,
                }
            )
        if status == "resolved":
            operations.extend(
                [
                    {
                        "op": "add",
                        "path": f"/nodes/{thread['id']}/attrs/resolved_at",
                        "value": now(),
                    },
                    {
                        "op": "add",
                        "path": f"/nodes/{thread['id']}/attrs/resolved_by",
                        "value": actor(),
                    },
                ]
            )
        else:
            operations.extend(
                [
                    {
                        "op": "add",
                        "path": f"/nodes/{thread['id']}/attrs/resolved_at",
                        "value": "",
                    },
                    {
                        "op": "add",
                        "path": f"/nodes/{thread['id']}/attrs/resolved_by",
                        "value": "",
                    },
                ]
            )
        self.patch(
            role=effective,
            base=document["revision"],
            operations=operations,
            intent=f"{status} {thread['id']}",
            origin="review-adapter",
        )
        if status == "open":
            manifest = self.plans.load(self.plan_id)
            self._open_review_round(manifest, thread["id"])
            self._save_review_rounds(manifest)
        return self._thread_dto(
            self._thread_node(
                self.load(role=effective, record=False), thread["id"]
            )
        )

    def promote_thread(
        self,
        thread_id: str,
        *,
        role: str,
        title: str,
        body: str = "",
        audience=None,
        binding: str = "must",
    ) -> dict:
        effective = self._claim(role)
        if effective != ARCHITECT:
            raise PlanError("only the architect may promote a directive")
        document = self.load(role=effective, record=False)
        thread = self._thread_node(document, thread_id)
        manifest = self.plans.load(self.plan_id)
        counter_manifest = {
            "plandoc": {
                "counters": copy.deepcopy(
                    manifest.get("plandoc", {}).get(
                        "counters", self._counter_state(document)
                    )
                )
            }
        }
        directive_id = grogu_plandoc.allocate_id(
            counter_manifest, "directive", existing_ids=document["nodes"]
        )
        edge_id = grogu_plandoc.allocate_id(
            counter_manifest,
            "edge",
            existing_ids=document["edges"],
        )
        directive = grogu_plandoc.make_node(
            directive_id,
            "directive",
            title,
            stage=thread.get("stage", ""),
            body=body,
            attrs={
                "binding": binding,
                "audience": list(audience or ["everyone"]),
                "status": "active",
                "origin": f"thread:{thread['id']}",
            },
            order=max(
                [
                    node.get("order", 0)
                    for node in document["nodes"].values()
                    if node.get("kind") == "directive"
                ]
                or [0]
            )
            + 1000,
            revision=document["revision"],
        )
        edge = grogu_plandoc.make_edge(
            edge_id,
            "derives_from",
            thread["id"],
            directive_id,
            revision=document["revision"],
        )
        operations = [
            {"op": "add", "path": f"/nodes/{directive_id}", "value": directive},
            {"op": "add", "path": f"/edges/{edge_id}", "value": edge},
            {
                "op": "replace",
                "path": f"/nodes/{thread['id']}/attrs/status",
                "value": "resolved",
            },
            {
                "op": "add",
                "path": f"/nodes/{thread['id']}/attrs/promoted_directive",
                "value": directive_id,
            },
        ]
        result = self.patch(
            role=effective,
            base=document["revision"],
            operations=operations,
            intent=f"promote {thread['id']} to {directive_id}",
            origin="directive-promotion",
        )
        return {
            "directive": directive_id,
            "edge": edge_id,
            "thread": thread["id"],
            "revision": result["revision"],
        }

    def _proposal_path(self, proposal_id: str) -> Path:
        if re.fullmatch(r"pr-[1-9][0-9]*", proposal_id or "") is None:
            raise PlanError(f"invalid proposal id {proposal_id!r}")
        return self.package / "proposals" / f"{proposal_id}.json"

    def proposals(self, *, role: str) -> list[dict]:
        effective = self._claim(role)
        directory = self.package / "proposals"
        if not directory.is_dir():
            return []
        values = []
        for path in sorted(directory.glob("pr-*.json")):
            try:
                value = self._read_json_file(path)
            except PlanError:
                continue
            if value.get("id") and self._proposal_readable(
                value, effective
            ):
                values.append(self._public_proposal(value))
        return values

    @staticmethod
    def _public_proposal(proposal: dict) -> dict:
        return {
            key: copy.deepcopy(value)
            for key, value in proposal.items()
            if key != "visibility"
        }

    @staticmethod
    def _proposal_readable(proposal: dict, role: str) -> bool:
        operations = proposal.get("ops", [])
        visibility = proposal.get("visibility", {})
        entries = (
            visibility.get("ops", [])
            if isinstance(visibility, dict)
            else []
        )
        if len(entries) != len(operations):
            return False
        allowed = set(ROLE_READABLE_STAGES.get(role, ())) | {""}
        return all(
            isinstance(entry, dict)
            and set(entry.get("stages", [])) <= allowed
            for entry in entries
        )

    def propose(
        self,
        *,
        role: str,
        operations,
        why: str,
        base: str = "",
        from_thread: str = "",
    ) -> dict:
        effective = self._claim(role)
        document = self.load(role=effective, record=False)
        base = base or document["revision"]
        # Full dry-run validation, including authorization and compiler
        # equivalence, occurs before a proposal file is accepted.
        self.patch(
            role=effective,
            base=base,
            operations=operations,
            intent=why or "proposal preview",
            origin="proposal-preview",
            dry_run=True,
        )
        added, removed = self._reserved_ids(operations)
        proposed = grogu_plandoc_patch.apply_patch(
            document,
            operations,
            base=base,
            current_revision=base,
            readable_stages=ROLE_READABLE_STAGES[effective],
            authorized_new_ids=added,
            authorized_remove_ids=removed,
        )
        visibility = self._operation_visibility(
            document, proposed, operations
        )
        with self.plans.locked():
            directory = self.package / "proposals"
            numbers = [
                int(match.group(1))
                for path in directory.glob("pr-*.json")
                if (match := re.fullmatch(r"pr-([1-9][0-9]*)\.json", path.name))
            ] if directory.is_dir() else []
            proposal_id = f"pr-{max(numbers, default=0) + 1}"
            proposal = {
                "schema_version": 1,
                "id": proposal_id,
                "at": now(),
                "from_thread": from_thread,
                "base": base,
                "why": str(why),
                "ops": copy.deepcopy(list(operations or [])),
                "visibility": visibility,
                "status": "pending",
                "decided_at": "",
                "decided_by": "",
                "why_not": "",
            }
            grogu_plandoc_revision.atomic_write(
                self._proposal_path(proposal_id),
                grogu_plandoc_canon.pretty_dumpb(proposal),
            )
        return self._public_proposal(proposal)

    def _proposal(self, proposal_id: str, *, role: str) -> dict:
        effective = self._claim(role)
        try:
            proposal = self._read_json_file(
                self._proposal_path(proposal_id)
            )
        except PlanError as error:
            raise PlanError(f"proposal {proposal_id!r} was not found") from error
        if not self._proposal_readable(proposal, effective):
            raise PlanError(f"proposal {proposal_id!r} was not found")
        return proposal

    def proposal_preview(self, proposal_id: str, *, role: str) -> dict:
        effective = self._claim(role)
        proposal = self._proposal(proposal_id, role=effective)
        base_document = self.load(
            role=effective,
            revision=proposal["base"],
            record=False,
        )
        added, removed = self._reserved_ids(proposal.get("ops", []))
        after = grogu_plandoc_patch.apply_patch(
            base_document,
            proposal.get("ops", []),
            base=proposal["base"],
            current_revision=proposal["base"],
            readable_stages=ROLE_READABLE_STAGES[effective],
            authorized_new_ids=added,
            authorized_remove_ids=removed,
        )
        after = self._stamp_revision(
            base_document, after, base_document["revision"]
        )
        impact = self._impact_dto(
            base_document,
            after,
            proposal.get("ops", []),
        )
        node_diff = []
        for operation in proposal.get("ops", []):
            match = re.match(
                r"^/(nodes|edges)/([^/]+)", str(operation.get("path", ""))
            )
            if not match:
                continue
            bucket, identifier = match.groups()
            before_value = base_document.get(bucket, {}).get(identifier)
            after_value = after.get(bucket, {}).get(identifier)
            node_diff.append(
                {
                    "id": identifier,
                    "op": (
                        "add"
                        if operation.get("op") == "add"
                        else (
                            "remove"
                            if operation.get("op") == "remove"
                            else (
                                "link"
                                if bucket == "edges"
                                and before_value is None
                                else "set"
                            )
                        )
                    ),
                    "kind": (
                        (after_value or before_value or {}).get("kind", "")
                    ),
                    "title": (
                        (after_value or before_value or {}).get(
                            "title", identifier
                        )
                    ),
                    "before": copy.deepcopy(before_value),
                    "after": copy.deepcopy(after_value),
                }
            )
        compiled = []
        manifest = self.plans.load(self.plan_id)
        for stage in manifest.get("stages", []):
            if stage not in ROLE_READABLE_STAGES[effective]:
                continue
            spec = self._projection_spec(effective, [stage])
            left = grogu_plandoc_compile.compile_checked(
                base_document, spec
            )["markdown"]
            right = grogu_plandoc_compile.compile_checked(after, spec)[
                "markdown"
            ]
            if left != right:
                compiled.append(
                    {"role": effective, "stage": stage, "before": left, "after": right}
                )
        return {
            "node_diff": node_diff,
            "compiled_diff": compiled,
            "impact": impact,
            "stale": proposal["base"] != self.head(repair=True),
            "head": self.head(repair=True),
        }

    def _impact_dto(self, before: dict, after: dict, operations) -> dict:
        changed = self._changed_ids(operations)
        selected = next(
            (item for item in changed if item in before.get("nodes", {})),
            next((item for item in changed if item in after.get("nodes", {})), ""),
        )
        raw = (
            grogu_plandoc.impact(
                before,
                {"type": "node", "id": selected},
                proposed=after,
            )
            if selected
            else {
                "direct": [],
                "transitive": [],
                "cycles": [],
                "compiled_delta": grogu_plandoc.compiled_delta(before, after),
            }
        )

        def item(identifier: str, *, reason: str = "") -> dict:
            value = after["nodes"].get(identifier) or before["nodes"].get(
                identifier
            ) or after["edges"].get(identifier) or before["edges"].get(identifier)
            value = value or {}
            return {
                "id": identifier,
                "kind": value.get("kind", "task"),
                "title": value.get("title", identifier),
                "reason": reason,
                "destructive": False,
            }

        return {
            "direct": [item(identifier) for identifier in raw.get("direct", [])],
            "transitive": [
                item(
                    entry["id"],
                    reason=" → ".join(entry.get("path", [])),
                )
                for entry in raw.get("transitive", [])
            ],
            "cycles": raw.get("cycles", []),
            "compiled_delta": raw.get("compiled_delta", []),
        }

    def impact(
        self,
        *,
        role: str,
        selector: dict,
        depth: Optional[int] = None,
    ) -> dict:
        effective = self._claim(role)
        document = self.load(role=effective, record=False)
        raw = grogu_plandoc.impact(document, selector, depth=depth)

        def item(identifier: str, reason: str = "") -> dict:
            value = document["nodes"].get(identifier, {})
            return {
                "id": identifier,
                "kind": value.get("kind", "task"),
                "title": value.get("title", identifier),
                "reason": reason,
                "destructive": False,
            }

        return {
            "direct": [item(identifier) for identifier in raw["direct"]],
            "transitive": [
                item(entry["id"], " → ".join(entry["path"]))
                for entry in raw["transitive"]
            ],
            "cycles": raw["cycles"],
            "compiled_delta": raw["compiled_delta"],
        }

    def accept_proposal(self, proposal_id: str, *, role: str) -> dict:
        effective = self._claim(role)
        proposal = self._proposal(proposal_id, role=effective)
        if proposal.get("status") != "pending":
            raise PlanError(
                f"proposal {proposal_id} is {proposal.get('status', 'unknown')}"
            )
        head = self.head(repair=True)
        if proposal.get("base") != head:
            raise grogu_plandoc_patch.StaleRevision(
                str(proposal.get("base", "")), head
            )
        predicted = grogu_plandoc_revision.revision_id(int(head[1:]) + 1)
        decided = {
            **proposal,
            "status": "accepted",
            "decided_at": now(),
            "decided_by": actor(),
            "revision": predicted,
        }
        result = self.patch(
            role=effective,
            base=head,
            operations=proposal.get("ops", []),
            intent=f"accept {proposal_id}",
            origin=f"proposal:{proposal_id}",
            extra_artifacts={
                f"proposals/{proposal_id}.json": grogu_plandoc_canon.pretty_dumpb(
                    decided
                )
            },
            proposal_guard=proposal_id,
        )
        return {
            "revision": result["revision"],
            "proposal": self._public_proposal(decided),
        }

    def reject_proposal(
        self,
        proposal_id: str,
        *,
        role: str,
        why_not: str,
    ) -> dict:
        self._claim(role)
        if not str(why_not).strip():
            raise PlanError("rejecting a proposal requires a reason")
        proposal = self._proposal(proposal_id, role=role)
        if proposal.get("status") != "pending":
            raise PlanError(
                f"proposal {proposal_id} is {proposal.get('status', 'unknown')}"
            )
        proposal.update(
            {
                "status": "rejected",
                "decided_at": now(),
                "decided_by": actor(),
                "why_not": str(why_not).strip(),
            }
        )
        with self.plans.locked():
            current = self._read_json_file(self._proposal_path(proposal_id))
            if current.get("status") != "pending":
                raise PlanError(
                    f"proposal {proposal_id} is "
                    f"{current.get('status', 'unknown')}"
                )
            grogu_plandoc_revision.atomic_write(
                self._proposal_path(proposal_id),
                grogu_plandoc_canon.pretty_dumpb(proposal),
            )
        return self._public_proposal(proposal)

    # -- explicit agent registration and the privacy-normalized board ----

    def _registration_dir(self) -> Path:
        return self.plans.state_dir / "controlroom" / "registrations"

    def register_session(self, value: dict) -> dict:
        registration_value = dict(value)
        registration_value.setdefault("repository", str(self.plans.root))
        registration_value.setdefault("plan", self.plan_id)
        registration_value.setdefault("registered_at", now())
        registration = grogu_controlroom.load_registration(registration_value)
        if not registration.run_id:
            raise PlanError("a control-room registration requires run_id")
        if registration.plan != self.plan_id:
            raise PlanError(
                f"registration belongs to {registration.plan!r}, not "
                f"{self.plan_id!r}"
            )
        if Path(registration.repository).expanduser().resolve() != self.plans.root:
            raise PlanError("registration repository does not match this plan store")
        if registration.events_path and not grogu_agentevents.is_session_events_path(
            Path(registration.events_path)
        ):
            raise PlanError(
                "events_path must be an explicit "
                "session-state/<session>/events.jsonl path"
            )
        current = self.registrations()
        paths = {
            item.get("events_path", "")
            for item in current
            if item.get("events_path")
        }
        if (
            registration.events_path
            and registration.events_path not in paths
            and len(paths) >= grogu_agentevents.MAX_REGISTERED_SOURCES
        ):
            raise PlanError("at most 32 registered session event sources are allowed")
        directory = self._registration_dir()
        directory.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(registration.run_id.encode("utf8")).hexdigest()
        self.plans._write_json(directory / f"{name}.json", registration.to_dict())
        return registration.to_dict()

    def registrations(self) -> list[dict]:
        directory = self._registration_dir()
        if not directory.is_dir():
            return []
        values = []
        for path in sorted(directory.glob("*.json")):
            value = self.plans._read_json(path)
            try:
                registration = grogu_controlroom.load_registration(value)
            except ValueError:
                continue
            try:
                repository = Path(registration.repository).expanduser().resolve()
            except OSError:
                continue
            if repository != self.plans.root or registration.plan != self.plan_id:
                continue
            values.append(registration.to_dict())
        return values

    def control_room(self, *, role: str):
        effective = self._role(role)
        home = Path(os.environ.get("GROGU_HOME", Path.home() / ".grogu"))
        room = grogu_controlroom.ControlRoom(
            effective_role=effective,
            watch_home=home,
            traces_db=home / "traces.db",
        )
        for value in self.registrations():
            room.register(grogu_controlroom.load_registration(value))
        return room

    @staticmethod
    def _epoch_millis(value) -> int:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(float(value) * (1000 if float(value) < 10_000_000_000 else 1))
        if not isinstance(value, str) or not value:
            return 0
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return 0
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return int(parsed.timestamp() * 1000)

    @staticmethod
    def _badge(row: dict) -> str:
        if int(row.get("steering", {}).get("unread", 0) or 0) > 0:
            return "waiting"
        lifecycle = row.get("lifecycle")
        if lifecycle == "finished":
            return "finished"
        if lifecycle == "failed" or row.get("failures"):
            return "error"
        if row.get("connection") == "disconnected":
            return "disconnected"
        if row.get("activity") == "possibly_stuck":
            return "stuck"
        if row.get("activity") == "active":
            return "live"
        return "idle"

    @staticmethod
    def _limit_detail(category: str) -> str:
        return {
            "model_reasoning": "Model reasoning is never recorded.",
            "command_arguments": "Command arguments are never shown.",
            "tool_arguments": "Tool arguments are dropped before parsing returns.",
            "tool_results": "Tool results are dropped before parsing returns.",
            "file_edits": "File edits are not observable from passive sources.",
            "tool_activity": "Tool activity is unavailable without registration.",
            "sealed_stage_content": "Sealed-stage content is never returned.",
            "trace_payload": "Trace payloads are never selected or returned.",
        }.get(category, "Unavailable by design.")

    def _normalized_agent(
        self,
        row: dict,
        *,
        plan_titles: dict,
        room,
        registrations: dict,
        effective_role: str,
    ) -> dict:
        registration = registrations.get(row.get("agent_key", ""))
        last_tool = None
        if registration is not None:
            drill = room.drill_in(registration, max_events=25)
            for event in reversed(drill.get("activity", [])):
                if event.get("kind") != "tool_completed":
                    continue
                last_tool = {
                    "tool_name": event.get("name", ""),
                    "duration_ms": int(event.get("duration_ms", 0) or 0),
                    "outcome": (
                        "ok"
                        if event.get("success") is True
                        else (
                            "err"
                            if event.get("success") is False
                            else "timeout"
                        )
                    ),
                }
                break
        current_action = row.get("current_action")
        if current_action:
            current_action = {
                "tool_name": current_action.get("tool_name", ""),
                "phase": current_action.get("phase", "unknown"),
                "at": self._epoch_millis(current_action.get("at")),
                "summary": None,
            }
        steering = dict(row.get("steering", {}))
        feedback = self.feedback_records(role=effective_role)
        matching_feedback = [
            item
            for item in feedback
            if item.get("scope", {}).get("kind") == "agent"
            and item.get("scope", {}).get("agent_key") == row.get("agent_key")
        ]
        steering["unread"] = sum(
            item.get("state") not in {"acknowledged", "withdrawn", "undeliverable"}
            for item in matching_feedback
        )
        plan_id = str(row.get("plan", ""))
        try:
            revision = (
                PlanDocumentStore.for_plan(self.plans, plan_id).head()
                if plan_id
                else ""
            )
        except PlanError:
            revision = ""
        failures = [
            {
                "code": str(item.get("code", "")),
                "at": self._epoch_millis(item.get("at")),
            }
            for item in row.get("failures", [])
        ]
        return {
            "agent_key": str(row.get("agent_key", "")),
            "agent": str(row.get("agent", "")),
            "run_id": str(row.get("run_id", "")),
            "role": str(row.get("role", "")),
            "roles": list(row.get("roles", [])),
            "workstream": str(row.get("workstream", "")),
            "plan": plan_id,
            "plan_title": plan_titles.get(plan_id, ""),
            "revision": {
                "last_read": str(row.get("revision", {}).get("last_read", "")),
                "current": revision,
                "relation": (
                    "current"
                    if revision
                    and row.get("revision", {}).get("last_read") == revision
                    else (
                        "behind"
                        if revision and row.get("revision", {}).get("last_read")
                        else "unknown"
                    )
                ),
            },
            "lifecycle": row.get("lifecycle", "unknown"),
            "activity": row.get("activity", "unknown"),
            "connection": row.get("connection", "unknown"),
            "badge": self._badge({**row, "steering": steering}),
            "started_at": (
                self._epoch_millis(row.get("started_at"))
                if row.get("started_at")
                else None
            ),
            "last_observed_at": self._epoch_millis(
                row.get("last_observed_at")
            ),
            "elapsed_ms": int(row.get("elapsed_ms") or 0),
            "elapsed_basis": row.get("elapsed_basis", "unknown"),
            "current_action": current_action,
            "last_tool": last_tool,
            "tools": {
                "started": int(row.get("tools", {}).get("started", 0)),
                "completed": int(row.get("tools", {}).get("completed", 0)),
                "failed": int(row.get("tools", {}).get("failed", 0)),
                "inflight": int(row.get("tools", {}).get("inflight", 0)),
                "complete": bool(row.get("tools", {}).get("complete", False)),
            },
            "grogu_commands": {
                "calls": int(row.get("grogu_commands", {}).get("calls", 0)),
                "failures": int(
                    row.get("grogu_commands", {}).get("failures", 0)
                ),
                "last_command": str(
                    row.get("grogu_commands", {}).get("last_command", "")
                ),
            },
            "failures": failures,
            "blockers": [str(item) for item in row.get("blockers", [])],
            "steering": {
                "unread": int(steering.get("unread", 0)),
                "delivered": int(steering.get("delivered", 0)),
                "acknowledged": int(steering.get("acknowledged", 0)),
            },
            "coverage": {
                "lifecycle": row.get("coverage", {}).get(
                    "lifecycle", "unavailable"
                ),
                "tools": row.get("coverage", {}).get(
                    "tools", "unavailable"
                ),
                "outcomes": row.get("coverage", {}).get(
                    "outcomes", "unavailable"
                ),
            },
            "basis": "observed",
            "stuck_threshold_s": 300,
            "last_error": "",
            "error_token": failures[-1]["code"] if failures else "",
        }

    def control_snapshot(
        self,
        *,
        role: str,
        room=None,
        plan: str = "",
        filter_role: str = "",
        workstream: str = "",
        state: str = "",
        window_minutes: int = 120,
    ) -> dict:
        self._role(role)
        room = room or self.control_room(role=role)
        summaries = {
            item["id"]: self.plans.summary(item["id"])
            for item in self.plans.list_plans()
        }
        raw = room.snapshot(
            plan_summaries=summaries,
            window_minutes=window_minutes,
        )
        registration_values = [
            grogu_controlroom.load_registration(item)
            for item in self.registrations()
        ]
        registrations = {
            item.agent_key: item for item in registration_values
        }
        titles = {
            plan_id: str(summary.get("title", ""))
            for plan_id, summary in summaries.items()
        }
        agents = [
            self._normalized_agent(
                row,
                plan_titles=titles,
                room=room,
                registrations=registrations,
                effective_role=role,
            )
            for row in raw.get("agents", [])
            if row.get("agent_key") in registrations
        ]
        if plan:
            agents = [item for item in agents if item["plan"] == plan]
        if filter_role:
            agents = [item for item in agents if item["role"] == filter_role]
        if workstream:
            agents = [
                item for item in agents if item["workstream"] == workstream
            ]
        if state:
            wanted = state.casefold()
            agents = [
                item
                for item in agents
                if wanted
                in {
                    str(item["badge"]).casefold(),
                    str(item["activity"]).casefold(),
                    str(item["lifecycle"]).casefold(),
                    str(item["connection"]).casefold(),
                }
            ]
        plans = {}
        for item in agents:
            entry = plans.setdefault(
                item["plan"],
                {
                    "title": titles.get(item["plan"], item["plan"]),
                    "agents": 0,
                },
            )
            entry["agents"] += 1
        waiting = [
            {
                "agent_key": item["agent_key"],
                "agent": item["agent"],
                "reason": (
                    f"{item['steering']['unread']} unread steering"
                    if item["steering"]["unread"]
                    else "; ".join(item["blockers"])
                ),
            }
            for item in agents
            if item["steering"]["unread"] or item["blockers"]
        ]
        return {
            "fresh_as_of": self._epoch_millis(raw.get("fresh_as_of")),
            "limits": [
                {
                    "category": str(category),
                    "detail": self._limit_detail(str(category)),
                }
                for category in raw.get("limits", [])
            ],
            "waiting_on_you": waiting,
            "agents": agents,
            "plans": plans,
        }

    def control_drill(self, agent_key: str, *, role: str, room=None) -> dict:
        self._role(role)
        room = room or self.control_room(role=role)
        registrations = {
            item.agent_key: item
            for item in (
                grogu_controlroom.load_registration(value)
                for value in self.registrations()
            )
        }
        registration = registrations.get(agent_key)
        if registration is None:
            raise PlanError(f"agent {agent_key!r} is not registered")
        snapshot = self.control_snapshot(role=role, room=room)
        agent = next(
            (
                item
                for item in snapshot["agents"]
                if item["agent_key"] == agent_key
            ),
            None,
        )
        if agent is None:
            raise PlanError(f"agent {agent_key!r} is unavailable")
        raw = room.drill_in(registration)
        activity = []
        for index, event in enumerate(raw.get("activity", []), 1):
            kind = event.get("kind", "")
            if kind.startswith("tool_"):
                event_type = "tool"
                summary = (
                    f"{event.get('name', 'tool')} "
                    f"{'completed' if kind == 'tool_completed' else 'started'}"
                )
            elif kind == "subagent_started":
                event_type = "workstream_started"
                summary = "Agent run started"
            elif kind == "subagent_completed":
                event_type = "workstream_merged"
                summary = "Agent run completed"
            elif kind == "permission_requested":
                event_type = "gate_blocked"
                summary = "Permission requested"
            elif kind == "permission_completed":
                event_type = "gate_passed"
                summary = "Permission completed"
            else:
                event_type = "disconnected"
                summary = "Coverage gap"
            item = {
                "id": f"{registration.run_id}:{index}",
                "type": event_type,
                "at": self._epoch_millis(event.get("at")),
                "summary": summary,
                "basis": "observed",
            }
            if kind.startswith("tool_"):
                item["tool"] = {
                    "tool_name": str(event.get("name", "")),
                    "duration_ms": int(event.get("duration_ms", 0) or 0),
                    "outcome": (
                        "ok"
                        if event.get("success") is True
                        else (
                            "err"
                            if event.get("success") is False
                            else "timeout"
                        )
                    ),
                }
            activity.append(item)
        return {
            "agent": agent,
            "activity": activity,
            "evidence": [
                {
                    "id": f"{registration.run_id}:evidence:{index}",
                    "label": "Observed plan object",
                    "plan": value.get("plan_id", registration.plan),
                    "node": value.get("object_id") or None,
                    "revision": value.get("revision") or None,
                    "available": value.get("object_id") is not None,
                }
                for index, value in enumerate(raw.get("evidence", []), 1)
            ],
            "limits": [
                {
                    "category": str(category),
                    "detail": self._limit_detail(str(category)),
                }
                for category in raw.get("limits", [])
            ],
            "blockers": [str(item) for item in raw.get("blockers", [])],
        }

    # -- durable feedback receipts through PlanStore.steer --------------

    @staticmethod
    def _feedback_gates(role: str) -> list[str]:
        return {
            ARCHITECT: [GATE_IMPLEMENT],
            DESIGNER: [GATE_TEST],
            ENGINEER: [GATE_IMPLEMENT],
            TESTER: [GATE_TEST, GATE_EVALUATE],
            REVIEWER: [GATE_IMPLEMENT],
            "all": list(GATES),
        }.get(role, list(GATES))

    def route_feedback(
        self,
        *,
        role: str,
        scope: dict,
        text: str,
        binding: bool,
        room=None,
    ) -> dict:
        effective = self._role(role)
        sender_agent = self.plans._identified_agent()[0]
        text = str(text).strip()
        if not text:
            raise PlanError("refusing to route empty feedback")
        kind = str(scope.get("kind", ""))
        target_role = "all"
        target_agent = ""
        delivered_to = str(scope.get("label", ""))
        registration = None
        if kind == "agent":
            agent_key = str(scope.get("agent_key", ""))
            registrations = {
                item.agent_key: item
                for item in (
                    grogu_controlroom.load_registration(value)
                    for value in self.registrations()
                )
            }
            registration = registrations.get(agent_key)
            if registration is None:
                raise PlanError(f"agent {agent_key!r} is not registered")
            target_role = registration.role or "all"
            target_agent = registration.agent
            delivered_to = agent_key
        elif kind == "role":
            target_role = str(scope.get("role", ""))
        elif kind == "plan":
            if str(scope.get("plan", "")) not in {"", self.plan_id}:
                raise PlanError("feedback plan scope does not match this plan")
            target_role = "all"
        elif kind == "role_plan":
            if str(scope.get("plan", "")) not in {"", self.plan_id}:
                raise PlanError("feedback plan scope does not match this plan")
            target_role = str(scope.get("role", ""))
        else:
            raise PlanError(f"unknown feedback scope {kind!r}")
        if target_role != "all" and target_role not in ROLES:
            raise PlanError(f"unknown feedback role {target_role!r}")
        gates = self._feedback_gates(target_role) if binding else []
        if registration is not None:
            room = room or self.control_room(role=role)
            receipt = room.route_feedback(
                registration,
                text=text,
                binding=False,
                plan_store=self.plans,
                gates_map={target_role: gates},
            )
            seq = int(receipt["seq"])
        else:
            note = self.plans.steer(
                text,
                plan_id=self.plan_id,
                role=target_role,
                requires_replan=False,
            )
            seq = int(note["seq"])
        feedback_id = f"f-{seq:03d}"
        sent_at = self._epoch_millis(now())
        with self.plans.locked():
            manifest = self.plans.load(self.plan_id)
            note = next(
                (
                    item
                    for item in manifest.get("steering", [])
                    if int(item.get("seq", 0)) == seq
                ),
                None,
            )
            if note is None:
                raise PlanError("feedback steering record disappeared")
            note.update(
                {
                    "feedback_id": feedback_id,
                    "feedback_scope": copy.deepcopy(scope),
                    "binding_feedback": bool(binding),
                    "target_agent": target_agent,
                    "feedback_sender_role": effective,
                    "feedback_sender_agent": sender_agent,
                    "gates": gates,
                    "feedback_history": [
                        {"state": "sent", "at": sent_at},
                        {"state": "routed", "at": sent_at},
                        {"state": "delivered", "at": sent_at},
                    ],
                }
            )
            self.plans._write_json(self.plans.manifest_path(self.plan_id), manifest)
        record = self.feedback_record(feedback_id, role=effective)
        return {
            "seq": feedback_id,
            "delivered_to": delivered_to,
            "gates_closed": gates,
            "record": record,
        }

    def feedback_records(self, *, role: str) -> list[dict]:
        effective = self._role(role)
        caller_agent = self.plans._identified_agent()[0]
        manifest = self.plans.load(self.plan_id)
        values = []
        for note in manifest.get("steering", []):
            if not note.get("feedback_id"):
                continue
            target_visible = (
                effective in {"all", ARCHITECT}
                or (
                    note.get("role") in {"all", effective}
                    and (
                        not note.get("target_agent")
                        or note.get("target_agent") == caller_agent
                    )
                )
            )
            sender_visible = (
                note.get("feedback_sender_role") == effective
                and (
                    not note.get("feedback_sender_agent")
                    or note.get("feedback_sender_agent") == caller_agent
                )
            )
            if not (target_visible or sender_visible):
                continue
            acknowledged = self.plans._feedback_acknowledged(manifest, note)
            if note.get("withdrawn"):
                state = "withdrawn"
            elif acknowledged:
                state = "acknowledged"
            else:
                state = "delivered"
            history = copy.deepcopy(note.get("feedback_history", []))
            if state == "acknowledged" and not any(
                item.get("state") == "acknowledged" for item in history
            ):
                history.append(
                    {
                        "state": "acknowledged",
                        "at": self._epoch_millis(now()),
                    }
                )
            if state == "withdrawn" and not any(
                item.get("state") == "withdrawn" for item in history
            ):
                history.append(
                    {
                        "state": "withdrawn",
                        "at": self._epoch_millis(
                            note.get("withdrawn_at", now())
                        ),
                    }
                )
            gates = list(note.get("gates", []))
            values.append(
                {
                    "id": note["feedback_id"],
                    "scope": copy.deepcopy(note.get("feedback_scope", {})),
                    "binding": bool(note.get("binding_feedback")),
                    "text": str(note.get("text", "")),
                    "state": state,
                    "gate": (
                        {
                            "stage": (
                                IMPLEMENTATION
                                if gates and gates[0] == GATE_IMPLEMENT
                                else (
                                    TESTING
                                    if gates and gates[0] == GATE_TEST
                                    else EVALUATION
                                )
                            ),
                            "state": (
                                "open"
                                if state in {"acknowledged", "withdrawn"}
                                else "blocked"
                            ),
                        }
                        if gates
                        else None
                    ),
                    "history": history,
                    "at": (
                        int(history[0].get("at", 0))
                        if history
                        else self._epoch_millis(note.get("at"))
                    ),
                }
            )
        return sorted(values, key=lambda item: item["at"], reverse=True)

    def feedback_record(self, feedback_id: str, *, role: str) -> dict:
        record = next(
            (
                item
                for item in self.feedback_records(role=role)
                if item["id"] == feedback_id
            ),
            None,
        )
        if record is None:
            raise PlanError(f"feedback {feedback_id!r} was not found")
        return record

    def withdraw_feedback(self, feedback_id: str, *, role: str) -> dict:
        effective = self._role(role)
        caller_agent = self.plans._identified_agent()[0]
        with self.plans.locked():
            manifest = self.plans.load(self.plan_id)
            note = next(
                (
                    item
                    for item in manifest.get("steering", [])
                    if item.get("feedback_id") == feedback_id
                ),
                None,
            )
            if note is None:
                raise PlanError(f"feedback {feedback_id!r} was not found")
            sender_role = str(note.get("feedback_sender_role", ""))
            sender_agent = str(note.get("feedback_sender_agent", ""))
            if not (
                sender_role == effective
                and (not sender_agent or sender_agent == caller_agent)
            ):
                raise PlanError(
                    "only the feedback sender may withdraw it"
                )
            if self.plans._feedback_acknowledged(manifest, note):
                raise PlanError("acknowledged feedback cannot be withdrawn")
            if not note.get("binding_feedback"):
                raise PlanError("only unacknowledged binding feedback can be withdrawn")
            note["withdrawn"] = True
            note["withdrawn_at"] = now()
            self.plans._write_json(self.plans.manifest_path(self.plan_id), manifest)
        return {"ok": True, "feedback": feedback_id}

    @classmethod
    def revert(
        cls,
        plans: PlanStore,
        reference: str,
        *,
        role: str = "",
        dry_run: bool = False,
    ) -> dict:
        service = cls.for_plan(plans, reference)
        effective = service._claim(role)
        if effective not in {ARCHITECT, REVIEWER}:
            raise PlanError("reverting a package requires architect or reviewer")
        head = service.head(repair=False)
        if head != "r0001":
            raise PlanError(
                f"cannot revert {service.plan_id}: {head} includes revisions "
                "after migration"
            )
        backup = service.package / "legacy" / "pre-migration"
        if not backup.is_dir():
            raise PlanError(f"{service.plan_id} has no pre-migration copy")
        legacy = plans.legacy_plan_dir(service.plan_id)
        result = {
            "plan": service.plan_id,
            "from": str(service.package),
            "to": str(legacy),
            "discarded_revisions": [item["revision"] for item in service.revisions(role=effective)],
            "dry_run": bool(dry_run),
        }
        if dry_run:
            return result
        staging = plans.plans_dir / f".{service.plan_id}.legacy.reverting-{os.getpid()}"
        if staging.exists():
            shutil.rmtree(staging)
        shutil.copytree(backup, staging, symlinks=True)
        with plans.locked():
            if legacy.exists():
                raise PlanError(f"legacy path already exists: {legacy}")
            os.replace(staging, legacy)
            try:
                shutil.rmtree(service.package)
            except OSError as error:
                raise PlanError(
                    f"restored {legacy}, but could not remove {service.package}; "
                    "both layouts now exist and must be repaired"
                ) from error
        return result
