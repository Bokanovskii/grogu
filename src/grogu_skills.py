"""Skills the agents write for the agents that come after them.

An agent that works out how to do something awkward -- which command actually
proves a change in this repository, which three steps always precede a release,
which trap swallowed an hour -- currently spends that knowledge and throws it
away. The next agent, in a fresh context, rediscovers it. `plan friction` covers
the case where the harness is *wrong*; this covers the case where the harness is
fine and the knowledge is missing.

Two things keep it from becoming a landfill of half-remembered notes:

Proposals are pooled across every repository, the same way harness friction is,
so the same lesson learned in three places adds up into one entry with echoes
rather than three near-duplicate skills. Repetition is the evidence that a
lesson generalises, and it is the only such evidence available.

And an agent may propose but not install. A skill is a standing instruction to
every future agent, which is the same authority as a role contract; an agent
granting itself that is how a wrong lesson becomes permanent. The user or the
supervisor accepts, and acceptance is what writes the file into the repository
where it can be reviewed in a diff like any other change.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import grogu_privacy

PENDING = "pending"
ACCEPTED = "accepted"
DECLINED = "declined"

SKILLS_DIRNAME = ".github/skills"

# A body past this is not a skill, it is a file somebody meant to attach. The
# comparison work is quadratic in the corpus and the redaction pass scans every
# byte, so a five megabyte paste did not fail -- it sat there for minutes with
# no output, which reads as a hang rather than as a mistake.
MAX_SKILL_CHARS = 20000

# Below this a "skill" is a sentence somebody meant to expand later. A standing
# instruction that vague costs every future agent a guess about what it meant.
HOLLOW_SKILL_CHARS = 160

# Same threshold the friction clusters use, for the same reason: high enough
# that two genuinely different lessons stay apart, low enough that the same
# lesson phrased differently by two agents lands in one place.
SKILL_SIMILARITY = 0.34

# Linking is not a claim, it is a note to whoever decides, so it is worth being
# wrong about more often: two agents writing one lesson in genuinely different
# words came out unlinked at 0.34, and an unlinked pair gives the reviewer no
# repetition signal at all -- which is the whole reason proposals are pooled.
SKILL_RELATED = 0.2

# A skill name has to contain a letter. `--not-the-same 1` was otherwise two
# questions at once -- the installed skill named "1" or declined proposal #1 --
# and it skipped both, which is exactly the blanket override the flag was
# written to avoid. Names and numbers now live in disjoint namespaces, and
# nothing useful is lost: "1" was never a name a future agent could act on.
NAME_PATTERN = re.compile(r"^[a-z0-9]*[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

_STOPWORDS = frozenset(
    """a an the and or but to of in on for with by is are was were be been it
    this that these those i we you have has had do does did so should would
    could there here when then than as at from up out if too very just really
    use using used when while about into over under after before your our""".split()
)


class SkillError(Exception):
    """Something about a skill proposal does not hold."""


class AlreadyKnown(SkillError):
    """The repository already has this lesson under another name."""


class AlreadyDeclined(SkillError):
    """This lesson was proposed before and turned down, with a reason."""


def skills_path() -> Path:
    home = os.environ.get("GROGU_HOME", "").strip()
    return (Path(home) if home else Path.home() / ".grogu") / "skills.json"


@contextmanager
def skills_lock() -> Iterator[None]:
    path = skills_path().with_name("skills.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


def _read() -> dict:
    """The store, or a refusal -- never a silent empty one.

    Treating an unreadable store as empty is the worst available option: the
    next `propose` writes proposal #1 over the top and every lesson anybody had
    filed is gone, with `proposals` having cheerfully reported "no proposals"
    on the way past. Reading nothing and reading a damaged file are different
    facts and have to stay different.
    """
    path = skills_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf8"))
    except OSError as error:
        raise SkillError(f"cannot read the skill store at {path}: {error}")
    except ValueError:
        raise SkillError(
            f"the skill store at {path} is not valid JSON. Nothing has been "
            "changed, because the alternative is overwriting whatever is still "
            "in there. Move it aside to start over: "
            f"`mv {path} {path}.broken`."
        )
    if not isinstance(payload, dict):
        raise SkillError(f"the skill store at {path} is not an object")
    entries = payload.get("entries", [])
    # Parsing as JSON is not the same as being usable, and the gap between the
    # two was doing real damage: two entries sharing a seq made the second one
    # unreachable through every command that takes a number, while it still
    # appeared in the listing as something waiting for a decision. Refusing is
    # the only safe answer, because the alternative is renumbering somebody
    # else's records to make our own display work.
    if not isinstance(entries, list):
        raise SkillError(
            f"the skill store at {path} has an 'entries' that is not a list"
        )
    seen = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise SkillError(f"entry {index} in {path} is not an object")
        seq = entry.get("seq")
        if not isinstance(seq, int):
            raise SkillError(
                f"entry {index} in {path} has no usable seq, so it cannot be "
                "shown, accepted or declined; nothing will be written until it "
                "is fixed or removed"
            )
        if seq in seen:
            raise SkillError(
                f"the skill store at {path} has two entries numbered #{seq}. "
                "Every command that takes a number would reach only the first, "
                "and the second would sit in the listing looking like it was "
                "waiting for you. Fix the file; nothing will be written until "
                "it is unambiguous."
            )
        seen.add(seq)
        if isinstance(seq, bool) or seq < 1:
            raise SkillError(
                f"entry {index} in {path} has seq {seq!r}; proposal numbers "
                "count up from 1"
            )
        for field in ("name", "description", "body"):
            if not isinstance(entry.get(field, ""), str):
                raise SkillError(
                    f"entry #{seq} in {path} has a {field} that is not text"
                )
        # Everything below is read by a command that indexes or iterates it.
        # Validating only the fields the store writes first left the rest to
        # fail as a traceback in front of whoever was reading the listing --
        # a related_to holding an object, an echoes holding a string, a status
        # nobody recognises silently vanishing from the queue.
        status = entry.get("status", PENDING)
        if status not in (PENDING, ACCEPTED, DECLINED):
            raise SkillError(
                f"entry #{seq} in {path} has status {status!r}, which is none "
                f"of {PENDING}, {ACCEPTED} or {DECLINED}; it would drop out of "
                "the queue without ever being decided"
            )
        for field in ("related_to", "nearby"):
            value = entry.get(field, [])
            if not isinstance(value, list) or any(
                not isinstance(item, int) or isinstance(item, bool) for item in value
            ):
                raise SkillError(
                    f"entry #{seq} in {path} has a {field} that is not a list "
                    "of proposal numbers"
                )
        if not isinstance(entry.get("echoes", []), list) or any(
            not isinstance(echo, dict) for echo in entry.get("echoes", [])
        ):
            raise SkillError(
                f"entry #{seq} in {path} has echoes that are not records"
            )
        if not isinstance(entry.get("overrode", []), list):
            raise SkillError(
                f"entry #{seq} in {path} has an overrode that is not a list"
            )
    return payload


def _write(payload: dict) -> None:
    path = skills_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf8")
    os.replace(temporary, path)


def _tokens(text: str) -> set:
    """Words, with hyphens split and plurals folded.

    Skill names are hyphen-joined by requirement, and the name is the densest
    part of a proposal -- so keeping `narrow-tests-first` whole made it share
    nothing at all with `narrow-first`, and two spellings of one lesson landed
    as two skills. The plural fold is for the same reason at a smaller scale:
    these are one-line descriptions, so `test` against `tests` is a tenth of
    the overlap between two texts this short.
    """
    words = re.findall(r"[a-z][a-z0-9_]{2,}", (text or "").lower().replace("-", " "))
    folded = {word[:-1] if len(word) > 4 and word.endswith("s") else word for word in words}
    return {word for word in folded if word not in _STOPWORDS}


def _similarity(left: set, right: set) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


SAME = "same"
RELATED = "related"
DIFFERENT = "different"


def _closeness(subject: tuple, entry: dict) -> float:
    name, description, body = subject
    return max(
        _similarity(
            _tokens(f"{name} {description}"),
            _tokens(f"{entry.get('name','')} {entry.get('description','')}"),
        ),
        _similarity(_tokens(body), _tokens(entry.get("body", ""))),
    )


def _match(subject: tuple, entry: dict) -> str:
    """How alike two lessons are, on the two signals worth reading.

    `why` is excluded from both. It holds the incident that prompted the
    proposal -- "ran the full suite six times chasing one assertion" -- which is
    different every time by construction, and including it pushed two verbatim
    copies of one procedure apart far enough to be filed twice.

    Headline and procedure are kept apart rather than pooled because either one
    alone is a bad judge, and in opposite directions. Two agents writing one
    lesson often disagree completely about what to call it. And two unrelated
    lessons routinely share a headline: an adversarial probe filed a
    dependency-license audit and a podcast mastering procedure under the same
    honest description, "run narrow validation before reporting success", which
    is a true thing to say about both and tells you nothing about either.

    Only both signals agreeing is treated as the same lesson, and that verdict
    is used exclusively to *refuse* -- never to discard anything.
    """
    name, description, body = subject
    headline = _similarity(
        _tokens(f"{name} {description}"),
        _tokens(f"{entry.get('name','')} {entry.get('description','')}"),
    )
    procedure = _similarity(_tokens(body), _tokens(entry.get("body", "")))
    if headline >= SKILL_SIMILARITY and procedure >= SKILL_SIMILARITY:
        return SAME
    if max(headline, procedure) >= SKILL_RELATED:
        return RELATED
    return DIFFERENT


def installed_skills(root: Path) -> list:
    """Skills already available in a repository."""
    directory = Path(root) / SKILLS_DIRNAME
    found = []
    if not directory.is_dir():
        return found
    for entry in sorted(directory.iterdir()):
        manifest = entry / "SKILL.md"
        if not manifest.is_file():
            continue
        text = manifest.read_text(encoding="utf8", errors="replace")
        found.append(
            {
                "name": entry.name,
                "description": _frontmatter_field(text, "description"),
                # The body is read because matching on the description alone
                # redirected an agent to an unrelated skill that happened to
                # share an honest one-line summary.
                "body": text.partition("---\n")[2].partition("\n---")[2],
                "path": str(manifest),
            }
        )
    return found


def _frontmatter_field(text: str, field: str) -> str:
    if not text.startswith("---"):
        return ""
    _, _, rest = text.partition("\n")
    body, _, _ = rest.partition("\n---")
    for line in body.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == field:
            return value.strip()
    return ""


def render(name: str, description: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body.strip()}\n"


def proposals(*, include_decided: bool = False) -> list:
    entries = _read().get("entries", [])
    if include_decided:
        return entries
    return [entry for entry in entries if entry.get("status") == PENDING]


def _check_overrides(overrode: list, entries: list, installed: list) -> None:
    """An override has to name something that exists.

    Unvalidated it was free text on a record nobody could check -- an agent
    could assert it had read a skill that was never written, and a probe put a
    credential-shaped string in the field, which then sat unredacted in a store
    that is pooled across repositories and reviewed in public. Requiring the
    target to resolve makes the field an audit record rather than a comment,
    and incidentally means nothing arbitrary can be written into it at all.
    """
    known = {skill.get("name", "") for skill in installed}
    declined = {str(entry["seq"]) for entry in entries if entry.get("status") == DECLINED}
    for target in overrode:
        if target in known or target in declined:
            continue
        raise SkillError(
            f"--not-the-same {target!r} names nothing you were shown. It takes "
            "the name of the installed skill you were sent to read, or the "
            "number of the declined proposal you were shown. Nothing was "
            "recorded; it is meant to be a note that you read that one, and a "
            "note about something that does not exist is not one."
        )


def _check_links(liked: list, entries: list) -> None:
    by_seq = {entry["seq"]: entry for entry in entries}
    for seq in liked:
        entry = by_seq.get(seq)
        if entry is None:
            raise SkillError(
                f"there is no proposal #{seq}. `grogu skill proposals` lists "
                "the numbers."
            )
        if entry.get("status") != PENDING:
            raise SkillError(
                f"proposal #{seq} was already {entry.get('status')}; linking to "
                "it would put your lesson next to a decision that has been made"
            )


def _validate(name: str, description: str, body: str) -> None:
    if not NAME_PATTERN.match(name):
        raise SkillError(
            f"{name!r} is not a skill name: lowercase words joined by hyphens, "
            "because the name becomes a directory and is how every future agent "
            "refers to it"
        )
    if len(name) > 40:
        raise SkillError("skill names are directories and prompts; keep it under 40 characters")
    if not description.strip():
        raise SkillError(
            "a skill needs a description: it is the only part an agent reads "
            "when deciding whether this skill applies to what it is doing"
        )
    if len(body) > MAX_SKILL_CHARS:
        raise SkillError(
            f"this body is {len(body)} characters, over the {MAX_SKILL_CHARS} a "
            "skill can be. A skill is the procedure, not the material: link or "
            "cite the long thing and write the steps."
        )
    if len(body.strip()) < HOLLOW_SKILL_CHARS:
        raise SkillError(
            f"this skill body is {len(body.strip())} characters once trimmed, "
            f"under the {HOLLOW_SKILL_CHARS} it takes to be a procedure rather "
            "than a reminder. Write what to do, in what order, and how the "
            "result is checked -- a future agent has none of the context you "
            "have now."
        )


def propose(
    name: str,
    *,
    description: str,
    body: str,
    why: str = "",
    role: str = "",
    plan: str = "",
    repository: str = "",
    repository_path: str = "",
    actor: str = "",
    installed: Optional[list] = None,
    overrode: Optional[list] = None,
    liked: Optional[list] = None,
) -> dict:
    """Record a skill an agent thinks the next agent should have.

    Bodies are pooled across repositories and end up proposed in a public
    checkout, so they are redacted on the way out for the same reason harness
    friction is: the most useful skill quotes the command that actually worked,
    and that command is where a token from a private repository escapes.

    `overrode` carries the agent's assertion that it read a named skill or a
    named decline and this is not that lesson. Both refusals below are
    judgements made by counting shared words, so both are sometimes wrong, and
    a wrong one here ends with the lesson never written at all. The assertion
    is recorded rather than trusted quietly: whoever reviews the proposal sees
    what was overridden and can disagree.
    """
    name = (name or "").strip().lower()
    overrode = [str(item).strip() for item in (overrode or []) if str(item).strip()]
    liked = list(dict.fromkeys(int(seq) for seq in (liked or [])))
    # Measured before redaction, not after. The scan reads every byte, so
    # checking the length afterwards meant an oversized paste was rejected only
    # once the expensive part had already run -- which is the hang the cap was
    # added to prevent, arriving a few seconds later with a message.
    if len(body or "") > MAX_SKILL_CHARS:
        raise SkillError(
            f"this body is {len(body)} characters, over the {MAX_SKILL_CHARS} a "
            "skill can be. A skill is the procedure, not the material: link or "
            "cite the long thing and write the steps."
        )
    description = grogu_privacy.redact((description or "").strip())
    body = grogu_privacy.redact(body or "")
    why = grogu_privacy.redact((why or "").strip())
    _validate(name, description, body)

    existing = {skill["name"] for skill in (installed or [])}
    amends = name in existing
    if not amends:
        # An agent that reaches a lesson the repository already wrote down
        # under another name should be sent to read it, not add a second copy
        # for the next agent to have to choose between.
        for skill in installed or []:
            if skill.get("name") in overrode:
                continue
            # Strict: headline *and* procedure. Sending an agent away to read
            # an unrelated skill is worse than letting a near-duplicate through
            # -- the duplicate gets caught by whoever reviews the proposal, and
            # the wrong redirect ends with the lesson never written at all.
            if _match((name, description, body), skill) == SAME:
                raise AlreadyKnown(
                    f"this is {skill['name']}, already installed: "
                    f"{skill.get('description','')}\nRead {skill.get('path','it')}. "
                    f"If it is wrong or incomplete, propose it as an amendment: "
                    f"`grogu skill propose {skill['name']} ...`.\n"
                    f"If you have read it and this is a different lesson, say "
                    f"so: add `--not-the-same {skill['name']}`."
                )

    with skills_lock():
        payload = _read()
        entries = payload.setdefault("entries", [])
        _check_overrides(overrode, entries, installed or [])
        _check_links(liked, entries)
        subject = (name, description, body)
        for entry in entries:
            if entry.get("status") != DECLINED:
                continue
            if str(entry["seq"]) in overrode:
                continue
            if _match(subject, entry) == SAME:
                # The decline note exists for exactly this moment. A fresh
                # context has no memory of being told no, so without this the
                # same rejected lesson comes back every time an agent hits the
                # same wall, and the reason the user wrote is read by nobody.
                # The attempt is still counted: a lesson declined once and
                # re-derived five times is evidence the decline was wrong.
                entry.setdefault("echoes", []).append(
                    {"role": role, "repository": repository, "why": why, "actor": actor}
                )
                _write(payload)
                count = len(entry["echoes"])
                agents = "agent has" if count == 1 else "agents have"
                raise AlreadyDeclined(
                    "this was proposed before and declined: "
                    f"{entry.get('decision') or 'no reason recorded'}\n"
                    f"({count} {agents} now reached it anyway.)\n"
                    "If that reason does not hold any more, say why: "
                    f"`grogu skill contest {entry['seq']} --note \"...\"`. "
                    "That puts it back in front of whoever declined it, with "
                    "your argument attached -- it does not install anything. "
                    "If you have read that reason and this is a different "
                    f"lesson, say so: add `--not-the-same {entry['seq']}`."
                )
        # Nothing here merges. A near-match used to be folded into the older
        # proposal, which meant every false positive silently destroyed a
        # lesson while telling its author it had been recorded -- and token
        # overlap produces false positives that no threshold removes, because
        # "these two texts share words" and "these two agents learned the same
        # thing" are different questions. Both survive, linked, and whoever
        # decides reads them side by side and merges if they really are one.
        related = [
            other["seq"]
            for other in entries
            if other.get("status") == PENDING and _match(subject, other) != DIFFERENT
        ]
        # Whatever the threshold, paraphrase defeats word counting: two agents
        # wrote the same rollback-rehearsal lesson in different vocabulary and
        # scored 0.16. Rather than tune a number until it is wrong in the other
        # direction, the nearest existing proposals are simply handed to the
        # one party that can actually judge -- the agent proposing, which has
        # the lesson in mind -- and it can link them itself with `--like`.
        neighbours = sorted(
            (
                (_closeness(subject, other), other)
                for other in entries
                if other.get("status") == PENDING and other["seq"] not in related
            ),
            key=lambda pair: -pair[0],
        )
        nearby = [other["seq"] for score, other in neighbours[:3] if score > 0]
        related += [seq for seq in liked if seq not in related]
        entry = {
            "seq": max([other["seq"] for other in entries] or [0]) + 1,
            "name": name,
            "description": description,
            "body": body.strip(),
            "why": why,
            "role": role,
            "plan": plan,
            "repository": repository,
            "repository_path": repository_path,
            "actor": actor,
            "amends": amends,
            "status": PENDING,
            "echoes": [],
            "related_to": related,
            "overrode": overrode,
            "nearby": nearby,
        }
        for other in entries:
            if other.get("seq") in related:
                other.setdefault("related_to", []).append(entry["seq"])
        entries.append(entry)
        _write(payload)
        return entry


def link(seq: int, other: int) -> dict:
    """Say that two proposals already filed are the same lesson.

    The nearest-proposals list is printed *after* the proposal is written --
    it cannot be printed before, because the thing it compares against is the
    proposal itself. So telling the agent to add `--like` and propose again
    meant filing the lesson a second time, which is how a probe ended up with
    two identical proposals and a link between one of them and a third. The
    advice now points here, and this amends what is already there.
    """
    if seq == other:
        raise SkillError("a proposal cannot be the same lesson as itself")
    with skills_lock():
        payload = _read()
        entries = payload.setdefault("entries", [])
        subject = _find(seq, entries)
        _check_links([other], entries)
        for one, two in ((subject, other), (_find(other, entries), seq)):
            links = one.setdefault("related_to", [])
            if two not in links:
                links.append(two)
        subject["nearby"] = [
            near for near in subject.get("nearby", []) if near != other
        ]
        _write(payload)
        return subject


def _find(seq: int, entries: list) -> dict:
    for entry in entries:
        if entry.get("seq") == seq:
            return entry
    raise SkillError(f"no skill proposal #{seq}")


def accept(seq: int, *, root: Path, note: str = "") -> dict:
    """Install a proposal into a repository, where a diff can review it."""
    with skills_lock():
        payload = _read()
        entry = _find(seq, payload.get("entries", []))
        if entry.get("status") != PENDING:
            raise SkillError(
                f"skill proposal #{seq} was already {entry.get('status')}"
            )
        # Re-validated here, not just at propose time. Between the two the
        # name has been through a plain JSON file on disk that anything can
        # edit, and this is the step that turns a name into a filesystem path:
        # a `../` that got in by any route would write outside the repository.
        # The whole proposal, not just the name. An entry that reached the
        # store by hand rather than through `propose` has been through none of
        # the checks, and one with an empty body installed as a skill file with
        # nothing under the front matter -- a standing instruction saying
        # nothing, which every future agent then reads.
        try:
            _validate(
                entry.get("name", ""),
                entry.get("description", ""),
                entry.get("body", ""),
            )
        except SkillError as error:
            raise SkillError(
                f"skill proposal #{seq} cannot be installed as it stands: {error}"
            )
        directory = Path(root) / SKILLS_DIRNAME / entry["name"]
        manifest = directory / "SKILL.md"
        if not manifest.resolve().parent.parent == (Path(root) / SKILLS_DIRNAME).resolve():
            raise SkillError(f"refusing to install outside {SKILLS_DIRNAME}")
        if manifest.exists() and not entry.get("amends"):
            raise SkillError(
                f"{manifest} already exists and this proposal was not written "
                "as an amendment to it. Re-propose it against the installed "
                "skill so the change to a standing instruction is visible."
            )
        directory.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            render(entry["name"], entry["description"], entry["body"]), encoding="utf8"
        )
        entry["status"] = ACCEPTED
        entry["installed_at"] = str(manifest)
        entry["decision"] = note.strip()
        _write(payload)
        return entry


def decline(seq: int, *, note: str = "") -> dict:
    if not note.strip():
        raise SkillError(
            "say why: the agent that proposed this will propose it again from "
            "a fresh context, and the reason is the only thing that stops it"
        )
    with skills_lock():
        payload = _read()
        entry = _find(seq, payload.get("entries", []))
        if entry.get("status") != PENDING:
            raise SkillError(f"skill proposal #{seq} was already {entry.get('status')}")
        entry["status"] = DECLINED
        entry["decision"] = note.strip()
        _write(payload)
        return entry


def contest(seq: int, *, note: str, role: str = "", repository: str = "") -> dict:
    """Put a declined lesson back in front of whoever declined it.

    The refusal told agents to say so if the reason no longer held, and gave
    them no way to say it -- so the only move left was to re-propose under a
    new name, which is precisely what the refusal exists to stop. Any role may
    contest, because the agent hitting the wall is the one with the evidence.
    It returns the proposal to the queue and nothing further: installing is
    still the user's or the supervisor's call, so this cannot be used to
    overturn a decision, only to ask again with an argument.
    """
    if not note.strip():
        raise SkillError(
            "say what changed. Contesting without an argument is re-proposing "
            "it with extra steps, and the person who declined it already gave "
            "a reason."
        )
    with skills_lock():
        payload = _read()
        entry = _find(seq, payload.get("entries", []))
        if entry.get("status") != DECLINED:
            raise SkillError(
                f"skill proposal #{seq} is {entry.get('status')}, not declined; "
                "there is nothing to contest"
            )
        entry["status"] = PENDING
        entry["contested"] = {
            "note": grogu_privacy.redact(note.strip()),
            "role": role,
            "repository": repository,
            "declined_for": entry.get("decision", ""),
        }
        _write(payload)
        return entry


def ripe(entries: Optional[list] = None) -> list:
    """Lessons more than one agent independently arrived at.

    Ripeness survives the move away from merging: it was never the merge that
    carried the signal, it was the repetition. A proposal linked to another, or
    one re-derived after being declined, is the same evidence it always was --
    it is now readable as two texts instead of one text and a counter.
    """
    entries = entries if entries is not None else proposals()
    return [entry for entry in entries if entry.get("echoes") or entry.get("related_to")]


def unwritten_lessons(clusters: list, *, installed: Optional[list] = None) -> list:
    """Recurring harness friction that nobody has turned into anything.

    A complaint filed once is a bad afternoon. The same complaint from three
    agents in three repositories is either a bug to fix or a procedure nobody
    wrote down, and this is the half the skill store can answer. It reads the
    friction clusters that already exist rather than inventing a detector,
    because the harness only ever sees its own commands -- it cannot watch an
    agent repeat itself in bash, so self-report is the only channel there is.
    """
    known = [
        _tokens(f"{skill.get('name','')} {skill.get('description','')}")
        for skill in (installed or [])
    ]
    known += [
        _tokens(f"{entry.get('name','')} {entry.get('description','')} {entry.get('why','')}")
        for entry in proposals(include_decided=True)
    ]
    unwritten = []
    for cluster in clusters:
        if cluster.get("count", 1) < 2:
            continue
        subject = _tokens(cluster.get("title", ""))
        if any(_similarity(subject, other) >= SKILL_SIMILARITY for other in known):
            continue
        unwritten.append(
            {
                "id": cluster.get("id", ""),
                "title": cluster.get("title", ""),
                "count": cluster.get("count", 1),
                "repositories": cluster.get("repositories", []),
                "roles": cluster.get("roles", []),
            }
        )
    return unwritten
