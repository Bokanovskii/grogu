"""Keep private things out of the places Grogu can publish them.

Grogu can read the user's messages, mail, repositories and personal graph, and
it can write commits, pull request bodies, issue comments and web searches. The
distance between those two facts is one careless paste. Until now the only thing
standing in that gap was a sentence in the operating contract — "never expose
secrets or personal data" — which is exactly the kind of instruction that works
until the one time it matters.

So this module does not advise. It scans, and the callers that publish refuse.

Two classes of finding, deliberately kept apart because they deserve different
answers. A **secret** is a credential: it is never acceptable in a commit or a
pull request, and the answer is always to stop. **Personal data** is contextual —
an email address in a commit message is normal, and the same address in a public
issue body pasted out of the user's inbox is not — so it warns loudly and blocks
only where the destination is genuinely public.

Precision matters more than recall here, and not for the usual reason. A scanner
that cries wolf is not merely annoying: it trains the person and the agent
reading it to pass `--force`, and a guard everyone has learned to override is
worse than no guard, because it also carries an assurance. Every pattern below is
either structurally unambiguous (a PEM header, a key with a vendor prefix and a
fixed length) or corroborated by a second signal before it is reported.
"""

from __future__ import annotations

import math
import re
import shlex
import subprocess
from pathlib import Path
from typing import Optional

SECRET = "secret"
PERSONAL = "personal"

# Destinations, ordered by how public they are.
LOCAL = "local"          # stays on the machine
REPOSITORY = "repository"  # committed; as public as the repository
PUBLISHED = "published"  # pull requests, issues, the web


class Finding:
    __slots__ = ("kind", "label", "line", "excerpt", "path")

    def __init__(self, kind: str, label: str, line: int, excerpt: str, path: str = ""):
        self.kind = kind
        self.label = label
        self.line = line
        self.excerpt = excerpt
        self.path = path

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "label": self.label,
            "line": self.line,
            "excerpt": self.excerpt,
            "path": self.path,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Finding {self.kind} {self.label} line {self.line}>"


# -- detectors -------------------------------------------------------------

# Vendor-prefixed credentials. These are worth listing individually rather than
# reaching for one clever generic rule, because the prefix *is* the proof: no
# ordinary sentence contains "ghp_" followed by thirty-six base62 characters, so
# these fire with essentially no false positives and need no entropy check.
_SECRET_PATTERNS = (
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("GitHub fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}\b")),
    ("OpenAI key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b")),
    ("Anthropic key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe key", re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}\b")),
    ("Twilio key", re.compile(r"\bSK[0-9a-fA-F]{32}\b")),
    ("SendGrid key", re.compile(r"\bSG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}\b")),
    ("npm token", re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("PyPI token", re.compile(r"\bpypi-AgEIcHlwaS5vcmc[A-Za-z0-9_-]{16,}\b")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    (
        "connection string with password",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:@/]+:[^\s:@/]{3,}@[^\s/]+", re.I),
    ),
)

# `NAME = value` where the name says credential. The name alone is not enough —
# `API_KEY = os.environ["API_KEY"]` is the correct way to write it and must not
# be flagged — so the value has to look like a literal secret too.
_ASSIGNMENT = re.compile(
    # The leading run is anchored to the start of a word. Without the
    # lookbehind it can begin at every character in the line, and on a long
    # unbroken token the engine walks the rest of the line looking for SECRET
    # from each of those starts in turn -- quadratic, and measurably so: a
    # single 20,000-character word took seventeen seconds. It is also more
    # correct, since a variable name does not start in the middle of another
    # identifier.
    r"""(?<![A-Za-z0-9_.-])(?P<name>[A-Za-z0-9_.-]*(?:SECRET|PASSWORD|PASSWD|TOKEN|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY|CLIENT_?SECRET)[A-Za-z0-9_.-]*)
        \s*[:=]\s*
        (?P<quote>["']?)(?P<value>[^\s"',;]{8,})(?P=quote)""",
    re.IGNORECASE | re.VERBOSE,
)

# Values that look like a credential but are not one. Placeholders outnumber
# real secrets in source trees by a wide margin, and every one of them that gets
# reported is a step towards the guard being ignored. Matching is by word rather
# than by exact shape, because placeholders are endlessly inventive about their
# suffixes: `your-api-key-here`, `CHANGEME_token`, `<insert key>`.
_PLACEHOLDER_WORDS = frozenset(
    """your my our the some example examples sample samples dummy fake mock
    placeholder redacted changeme change replace insert fill enter here goes
    todo fixme none null nil undefined empty test testing xxx xxxx yyyy zzzz
    abc123 secret password token key value string me please""".split()
)

_PLACEHOLDER_SHAPES = re.compile(
    r"""^(?:
        (?:x{3,}|\*{3,}|\.{3,}|-{3,}|<.*>|\{\{.*\}\}|\$\{?[a-z_]+\}?)
        |(?:process\.env|os\.environ|env|config|settings|secrets|vault)\b.*
    )$""",
    re.IGNORECASE | re.VERBOSE,
)


def _is_placeholder(value: str) -> bool:
    """A placeholder is *made of* placeholder words, not merely near one.

    Matching on any single word was too eager in the direction that costs
    something: `xoxb-secret-1234` contains "secret" and `my-real-key-9f3a`  grogu-allow-secret
    contains "my", and both were waved through in silence.  grogu-allow-secret A real secret with
    one English token in it is not rare, and the whole value of this guard is
    that it fires on the credential you did not mean to commit.
    """
    value = value.strip().strip("\"'`")
    if _PLACEHOLDER_SHAPES.match(value):
        return True
    words = [word for word in re.split(r"[^A-Za-z0-9]+", value.lower()) if word]
    if not words:
        return False
    hits = sum(1 for word in words if word in _PLACEHOLDER_WORDS)
    return hits * 2 > len(words)

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# Deliberately North-America-shaped and anchored on separators: a bare run of
# ten digits is far more often an id, a timestamp or a hash prefix.
_PHONE = re.compile(r"(?<![\d-])(?:\+1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\d-])")
# Street addresses, which show up when mail or messages get pasted around.
_ADDRESS = re.compile(
    r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s){1,3}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Way|Place|Pl)\b\.?",
)
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")

# Addresses that are examples by convention or belong to the tooling itself.
_BENIGN_EMAIL = re.compile(
    r"(?:@example\.(?:com|org|net)$"
    r"|@localhost$"
    r"|noreply(?:@|\.)"
    r"|@users\.noreply\.github\.com$"
    r"|^[a-f0-9]{16,}@)",
    re.IGNORECASE,
)


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict = {}
    for character in value:
        counts[character] = counts.get(character, 0) + 1
    total = len(value)
    return -sum(
        (count / total) * math.log2(count / total) for count in counts.values()
    )


def _looks_like_a_real_secret(value: str) -> bool:
    """Does this value carry enough disorder to be a credential?

    English prose and code identifiers sit well under three bits per character;
    generated keys sit well above it. The threshold is doing rough work — it only
    has to separate `password = "hunter2"` from `password = os.environ[...]`.
    """
    if _is_placeholder(value):
        return False
    stripped = value.strip("\"'`")
    if len(stripped) < 8:
        return False
    if stripped.startswith(("$", "<", "{", "%")) or stripped.endswith(("}", ">")):
        return False
    if _entropy(stripped) < 2.6:
        return False
    # A value made only of one case with no digits reads as a word, not a key.
    if stripped.isalpha() and stripped.islower():
        return False
    return True


def _luhn(digits: str) -> bool:
    total, alternate = 0, False
    for character in reversed(digits):
        digit = ord(character) - 48
        if alternate:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
        alternate = not alternate
    return total % 10 == 0


ALLOW_MARKER_TEXT = "grogu-allow-secret"
_ALLOW_MARKER = re.compile(re.escape(ALLOW_MARKER_TEXT), re.IGNORECASE)


def scan(text: str, *, path: str = "", personal: bool = True) -> list:
    """Every finding in `text`, in line order.

    `personal=False` drops the personal-data detectors, for destinations where
    an email address is ordinary — a commit trailer, say.
    """
    findings: list = []
    for number, line in enumerate(text.splitlines(), start=1):
        if _ALLOW_MARKER.search(line):
            # A deliberate, reviewable, single-line exemption. It exists so that
            # detector fixtures and documentation examples do not force the only
            # other escape hatch, which is `--no-verify` on the whole commit —
            # a habit far more dangerous than the line it was used to pass.
            continue
        if len(line) > 4000:
            line = line[:4000]  # minified bundles are not worth the backtracking
        for label, pattern in _SECRET_PATTERNS:
            for match in pattern.finditer(line):
                findings.append(
                    Finding(SECRET, label, number, _excerpt(match.group(0)), path)
                )
        for match in _ASSIGNMENT.finditer(line):
            if _looks_like_a_real_secret(match.group("value")):
                findings.append(
                    Finding(
                        SECRET,
                        f"hard-coded {match.group('name')}",
                        number,
                        _excerpt(match.group("value")),
                        path,
                    )
                )
        if not personal:
            continue
        for match in _EMAIL.finditer(line):
            if not _BENIGN_EMAIL.search(match.group(0)):
                findings.append(
                    Finding(PERSONAL, "email address", number, _excerpt(match.group(0)), path)
                )
        for match in _PHONE.finditer(line):
            findings.append(
                Finding(PERSONAL, "phone number", number, _excerpt(match.group(0)), path)
            )
        for match in _ADDRESS.finditer(line):
            findings.append(
                Finding(PERSONAL, "street address", number, _excerpt(match.group(0)), path)
            )
        for match in _SSN.finditer(line):
            findings.append(
                Finding(PERSONAL, "government id", number, "***-**-****", path)
            )
        for match in _CARD.finditer(line):
            digits = re.sub(r"[ -]", "", match.group(0))
            # A plan id is a date and six hex digits, and one of them passed
            # Luhn during a test run and blocked its own plan from being
            # finalized. A card number is not glued to a letter on either
            # side; an identifier usually is.
            text = match.group(0)
            end = match.end() - (len(text) - len(text.rstrip(" -")))
            before = line[: match.start()][-1:]
            after = line[end:][:1]
            if before.isalnum() or after.isalnum() or before == "-" or after == "-":
                continue
            if 13 <= len(digits) <= 19 and _luhn(digits):
                findings.append(
                    Finding(PERSONAL, "payment card number", number, "**** **** **** " + digits[-4:], path)
                )
    return findings


def _excerpt(value: str) -> str:
    """Never echo the whole thing: the report itself is an egress point."""
    value = value.strip()
    if len(value) <= 12:
        return value[:4] + "…"
    return f"{value[:6]}…{value[-4:]} ({len(value)} chars)"


def blocking(findings: list, *, destination: str = REPOSITORY) -> list:
    """Which findings should actually stop the operation.

    Secrets always stop it. Personal data stops only what is genuinely public,
    because a name or address inside a private working repository is often the
    whole point of the change.
    """
    if destination == PUBLISHED:
        return list(findings)
    return [finding for finding in findings if finding.kind == SECRET]


def redact(text: str) -> str:
    """Replace every detectable secret or personal datum with a marker."""
    for _, pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    text = _ASSIGNMENT.sub(
        lambda match: (
            f"{match.group('name')}=[redacted]"
            if _looks_like_a_real_secret(match.group("value"))
            else match.group(0)
        ),
        text,
    )
    text = _EMAIL.sub(
        lambda match: match.group(0) if _BENIGN_EMAIL.search(match.group(0)) else "[email]",
        text,
    )
    text = _PHONE.sub("[phone]", text)
    text = _ADDRESS.sub("[address]", text)
    text = _SSN.sub("[id]", text)
    text = _CARD.sub(
        lambda match: (
            "[card]"
            if _luhn(re.sub(r"[ -]", "", match.group(0)))
            else match.group(0)
        ),
        text,
    )
    return text


# -- git -------------------------------------------------------------------

# Files whose entire purpose is to hold credentials. Content scanning finds most
# of what is in them, but the file appearing in a commit at all is the mistake.
_DANGEROUS_PATHS = re.compile(
    r"""(?:^|/)(?:
        \.env(?:\.[A-Za-z0-9_.-]+)?
        |\.netrc|_netrc
        |\.npmrc|\.pypirc
        |id_rsa|id_dsa|id_ecdsa|id_ed25519
        |credentials|\.aws/credentials
        |[A-Za-z0-9_.-]*\.(?:pem|pfx|p12|jks|keystore|ppk)
        |secrets?\.(?:ya?ml|json|toml|ini)
        |[A-Za-z0-9_.-]*service[-_]?account[A-Za-z0-9_.-]*\.json
    )$""",
    re.IGNORECASE | re.VERBOSE,
)

_ENV_EXAMPLE = re.compile(r"\.env\.(?:example|sample|template|dist)$", re.IGNORECASE)


def dangerous_path(path: str) -> bool:
    if _ENV_EXAMPLE.search(path):
        return False
    return bool(_DANGEROUS_PATHS.search(path))


def staged_changes(repo: Optional[Path] = None) -> list:
    """(path, added-content) for everything staged, additions only.

    Only added lines are scanned: a commit that *removes* a leaked key should
    never be blocked, since blocking it is blocking the fix.
    """
    root = str(repo) if repo else "."
    try:
        names = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout.split("\n")
    except (subprocess.CalledProcessError, OSError, FileNotFoundError):
        return []
    changes = []
    for name in [name.strip() for name in names if name.strip()]:
        try:
            diff = subprocess.run(
                ["git", "diff", "--cached", "--unified=0", "--", name],
                cwd=root, capture_output=True, text=True, check=True,
            ).stdout
        except (subprocess.CalledProcessError, OSError):
            continue
        added = "\n".join(
            line[1:]
            for line in diff.split("\n")
            if line.startswith("+") and not line.startswith("+++")
        )
        changes.append((name, added))
    return changes


def scan_staged(repo: Optional[Path] = None, *, personal: bool = False) -> list:
    findings: list = []
    for name, added in staged_changes(repo):
        if dangerous_path(name):
            findings.append(
                Finding(SECRET, "credential file staged for commit", 0, name, name)
            )
        findings.extend(scan(added, path=name, personal=personal))
    return findings


def report(findings: list) -> str:
    lines = []
    for finding in findings:
        where = f"{finding.path}:{finding.line}" if finding.path else f"line {finding.line}"
        mark = "SECRET  " if finding.kind == SECRET else "personal"
        lines.append(f"  {mark} {where}: {finding.label} — {finding.excerpt}")
    return "\n".join(lines)


HOOK = """#!/bin/sh
# Installed by `grogu guard install`. Removing this file is the supported way to
# opt out; editing it will be overwritten on the next install.
exec {python} {script} guard staged --quiet
"""


def hooks_dir(repo: Path) -> Path:
    """Where this checkout's hooks actually live.

    In a linked worktree `.git` is a file, not a directory, and hooks are
    shared from the common directory — so the naive path both fails and, if it
    had succeeded, would have installed a hook that only guarded one worktree.
    Asking Git is the only reliable answer, and it respects core.hooksPath.
    """
    try:
        found = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-path", "hooks"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        ).stdout.strip()
        if found:
            return Path(found)
    except (subprocess.CalledProcessError, OSError, FileNotFoundError):
        pass
    return Path(repo) / ".git" / "hooks"


def install_hook(repo: Path, *, python: str, script: str) -> Path:
    hooks = hooks_dir(repo)
    hooks.mkdir(parents=True, exist_ok=True)
    path = hooks / "pre-commit"
    path.write_text(
        HOOK.format(python=shlex.quote(python), script=shlex.quote(script)),
        encoding="utf8",
    )
    path.chmod(0o755)
    return path
