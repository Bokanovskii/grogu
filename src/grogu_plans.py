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
import datetime as dt
import uuid
import fnmatch
import json
import os
import re
import sys
import subprocess
import secrets
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import grogu_platform
import grogu_privacy
from grogu_tasks import actor, repository_root, session_id
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


def primary_worktree(root: Path) -> Path:
    """The main working tree of `root`'s repository, or `root` itself.

    Plan state has to be one thing per repository. The pipeline's advertised way
    to parallelise is one worktree per workstream, and `.grogu/state` is
    gitignored, so a per-worktree store would give each agent a private copy of
    the manifest: two engineers would take separate locks on separate files,
    both write, and the defects, reviews and steering acks of whichever wrote
    first would simply vanish. Resolving to the main worktree means every agent
    in every worktree contends for the same lock over the same file, which is
    what the locking was for.

    `--git-common-dir` is the shared `.git` for a linked worktree, so its parent
    is the main checkout. In a plain clone it is already `<root>/.git`.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return root
    if result.returncode != 0:
        return root
    common = result.stdout.strip()
    if not common:
        return root
    candidate = Path(common).parent
    # A bare repository has no working tree to put plans in.
    try:
        return candidate.resolve() if candidate.is_dir() else root
    except OSError:
        return root


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
    _LOCAL_ONLY = "manifest.json\nrevisions/\n*.sealed\n"

    def _protect_working_state(self) -> None:
        try:
            self.plans_dir.mkdir(parents=True, exist_ok=True)
            marker = self.plans_dir / ".gitignore"
            if not marker.exists():
                marker.write_text(self._LOCAL_ONLY, encoding="utf8")
        except OSError:
            return

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

    def steering(self, *, role: str = "all", plan_id: str = "", unread: bool = False) -> dict:
        """Steering visible to `role`, newest last, cheap enough to poll."""
        if role != "all" and role not in ROLES:
            raise PlanError(f"unknown role {role!r}")

        def visible(notes: list, acked: int) -> list:
            selected = [
                note
                for note in notes
                # The architect owns the plan, so a correction aimed at the
                # engineer is still the architect's problem: it is how a plan
                # goes stale. Steering the plan's owner cannot see is the one
                # kind that silently invalidates everything downstream of it.
                if (role in ("all", ARCHITECT) or note.get("role") in ("all", role))
                and not note.get("retracted")
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
