"""Grogu's startup mark inside the Copilot CLI banner area.

Copilot CLI renders its own mascot from hardcoded art, so it cannot be
replaced. It does, however, render `companyAnnouncements` directly inside the
startup banner block, and that renderer preserves 24-bit SGR colour, leading
whitespace and multi-line layout. Grogu uses that documented setting to place
an original pixel-art mark next to Copilot's own banner text.

Nothing here writes to stdout ahead of Copilot, patches the Copilot binary or
intercepts its terminal output.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import random
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

RESET = "\033[0m"
TRANSPARENT = None

PALETTE: dict[str, tuple[int, int, int] | None] = {
    ".": TRANSPARENT,
    "g": (176, 214, 150),
    "G": (140, 186, 112),
    "d": (100, 140, 82),
    "e": (28, 34, 28),
    "w": (240, 246, 235),
    "r": (214, 199, 166),
    "R": (176, 158, 122),
    "s": (128, 112, 84),
}

# Original artwork: a small green space-frog child. 20x14 pixels, rendered two
# pixel rows per terminal row with half blocks.
ART_ROWS: tuple[str, ...] = (
    ".......dggGGd.......",
    "......dgggGGGd......",
    "......dgGGGGGd......",
    ".dg...dgGGGGGd...gd.",
    "dgggggGEEGGEEGgggggd",
    ".dddggGeeGGeeGggddd.",
    "...dggdGGddGGdggd...",
    ".......dGGGGd.......",
    "........dGGd........",
    "......rrRRRRrr......",
    ".....rrRRRRRRrr.....",
    "....rrRRRRRRRRrr....",
    "....rRRRRRRRRRRr....",
    "....ssssssssssss....",
)

# `EE` is the upper eye row and `ee` the lower one; both are swapped per frame.
EYE_FRAMES: dict[str, tuple[str, str]] = {
    "open": ("we", "ee"),
    "half": ("GG", "ee"),
    "closed": ("GG", "dd"),
}

ART_WIDTH = 20
NAME_COLOR = (149, 214, 130)
TEXT_COLOR = (145, 152, 161)
ACCENT_COLOR = (176, 158, 122)
CHECK_THE_WORK_SAMPLES: tuple[str, ...] = (
    "Check the work, you must.",
    "Trust, but verify, you should.",
    "Test it before ship, you must.",
    "Read the diff, you should.",
    "Small steps, fewer bugs.",
    "Measure twice, merge once.",
    "The edge cases, seek them.",
    "Proof beats hope, it does.",
    "Question every assumption.",
    "Make it work, then make it clear.",
    "The tests know the way.",
    "No shortcuts through correctness.",
    "A clean diff is a happy diff.",
    "Review the details, you must.",
    "First understand, then change.",
    "Reliable code, we seek.",
    "The bug hides in the edge case.",
    "Ship confidence, not guesses.",
    "One careful change at a time.",
    "Check twice, regret never.",
)


def _foreground(color: tuple[int, int, int]) -> str:
    return f"\033[38;2;{color[0]};{color[1]};{color[2]}m"


def _background(color: tuple[int, int, int]) -> str:
    return f"\033[48;2;{color[0]};{color[1]};{color[2]}m"


def _eye_row(row: str, placeholder: str, pixels: str) -> str:
    """Replace the left eye with `pixels` and the right eye with its mirror."""
    left = row.replace(placeholder, pixels, 1)
    return left.replace(placeholder, pixels[::-1], 1)


def frame_rows(frame: str = "open") -> list[str]:
    upper, lower = EYE_FRAMES.get(frame, EYE_FRAMES["open"])
    rows = []
    for index, row in enumerate(ART_ROWS):
        if index == 4:
            rows.append(_eye_row(row, "EE", upper))
        elif index == 5:
            rows.append(_eye_row(row, "ee", lower))
        else:
            rows.append(row)
    return rows


def _render_pair(top: str, bottom: str) -> str:
    """Render two pixel rows into one terminal row of half blocks."""
    out: list[str] = []
    current_fg: tuple[int, int, int] | None = None
    current_bg: tuple[int, int, int] | None = None
    for column in range(ART_WIDTH):
        upper = PALETTE[top[column]]
        lower = PALETTE[bottom[column]]
        if upper is None and lower is None:
            if current_fg is not None or current_bg is not None:
                out.append(RESET)
                current_fg = current_bg = None
            out.append(" ")
            continue
        if upper is None:
            glyph, foreground, background = "\u2584", lower, None
        elif lower is None:
            glyph, foreground, background = "\u2580", upper, None
        elif upper == lower:
            glyph, foreground, background = "\u2588", upper, None
        else:
            glyph, foreground, background = "\u2580", upper, lower
        if background != current_bg:
            out.append(RESET if background is None else _background(background))
            current_fg = None if background is None else current_fg
            current_bg = background
        if foreground != current_fg:
            out.append(_foreground(foreground))
            current_fg = foreground
        out.append(glyph)
    out.append(RESET)
    return "".join(out)


def render_mark(
    frame: str = "open", version: str = "", check_text: str | None = None
) -> list[str]:
    """Return the terminal rows for the Grogu banner mark."""
    rows = frame_rows(frame)
    lines = [_render_pair(rows[i], rows[i + 1]) for i in range(0, len(rows), 2)]
    label = f"Grogu v{version}" if version else "Grogu"
    check_text = (
        random.choice(CHECK_THE_WORK_SAMPLES)
        if check_text is None
        else check_text
    )
    captions = {
        2: f"{_foreground(NAME_COLOR)}{label}{RESET}"
        f"{_foreground(TEXT_COLOR)}  a Copilot CLI harness{RESET}",
        3: f"{_foreground(TEXT_COLOR)}Uses AI, it does."
        f"{_foreground(ACCENT_COLOR)}  {check_text}{RESET}",
    }
    for index, caption in captions.items():
        lines[index] = f"{lines[index]}  {caption}"
    return lines


def announcement(
    frame: str = "open", version: str = "", check_text: str | None = None
) -> str:
    """Announcement payload rendered inside Copilot's startup banner block.

    The leading newline keeps Copilot's own `Company announcement:` label on
    its own row so the artwork stays left aligned.
    """
    return "\n" + "\n".join(render_mark(frame, version, check_text))


def announcements(
    version: str = "", previous_check_text: str | None = None
) -> list[str]:
    """Choose a launch phrase, then provide the frames Copilot picks between."""
    choices = [
        sample
        for sample in CHECK_THE_WORK_SAMPLES
        if sample != previous_check_text
    ]
    check_text = random.choice(choices)
    return [
        announcement("open", version, check_text),
        announcement("open", version, check_text),
        announcement("open", version, check_text),
        announcement("half", version, check_text),
    ]


BLINK_CYCLE_SECONDS = 5.0


def status_line_frame(moment: float | None = None) -> str:
    """One-line Grogu whose eyes blink; refreshed by Copilot's status line."""
    moment = time.time() if moment is None else moment
    position = moment % BLINK_CYCLE_SECONDS
    if position < 1.0:
        eyes = "-\u1d55-"
    elif position < 2.0:
        eyes = "\u2022\u1d55\u2022"
    else:
        eyes = "\u25cf\u1d55\u25cf"
    skin = PALETTE["G"] or (140, 186, 112)
    dark = PALETTE["e"] or (28, 34, 28)
    ear = _foreground(skin)
    face = f"{_background(skin)}{_foreground(dark)}"
    return (
        f"{ear}\u2570{face} {eyes} {RESET}{ear}\u256f{RESET}"
        f"{_foreground(TEXT_COLOR)} grogu{RESET}"
    )


def copilot_home(environment: dict[str, str] | None = None) -> Path:
    environment = os.environ if environment is None else environment
    home = environment.get("COPILOT_HOME")
    if home:
        return Path(home).expanduser()
    return Path(environment.get("HOME", str(Path.home()))).expanduser() / ".copilot"


def settings_path(environment: dict[str, str] | None = None) -> Path:
    return copilot_home(environment) / "settings.json"


_COMMENTS = re.compile(
    r'"(?:\\.|[^"\\])*"|/\*.*?\*/|//[^\n\r]*',
    re.DOTALL,
)


def strip_jsonc(text: str) -> str:
    """Remove JSONC comments while leaving string literals untouched."""

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        return token if token.startswith('"') else " "

    return _COMMENTS.sub(replace, text)


def load_settings(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf8")
    except OSError:
        return {}
    try:
        value = json.loads(strip_jsonc(raw))
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def write_settings(path: Path, settings: dict) -> None:
    """Write settings, following a symlink rather than replacing it."""
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".grogu.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf8")
    os.replace(temporary, target)


GROGU_KEYS = ("banner", "companyAnnouncements")


def grogu_settings(current: dict, version: str, status_line: bool) -> dict:
    updated = dict(current)
    updated["banner"] = "always"
    previous_check_text = next(
        (
            sample
            for sample in CHECK_THE_WORK_SAMPLES
            if any(
                sample in str(message)
                for message in current.get("companyAnnouncements", [])
            )
        ),
        None,
    )
    updated["companyAnnouncements"] = announcements(version, previous_check_text)
    if status_line and not current.get("statusLine"):
        updated["statusLine"] = {
            "type": "command",
            "command": _status_line_command(),
            "refreshInterval": 1,
            "padding": 1,
        }
    return updated


def _status_line_command() -> str:
    launcher = Path(__file__).resolve().parent.parent / "bin" / "grogu"
    return f'"{launcher}" banner status-line'


def _digest(settings: dict) -> str:
    payload = json.dumps(settings, sort_keys=True).encode("utf8")
    return hashlib.sha256(payload).hexdigest()


@contextmanager
def _locked(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    lock = state_dir / "banner.lock"
    handle = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _state_file(state_dir: Path) -> Path:
    return state_dir / "banner-state.json"


def _read_state(state_dir: Path) -> dict:
    try:
        return json.loads(_state_file(state_dir).read_text(encoding="utf8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(state_dir: Path, state: dict) -> None:
    _state_file(state_dir).write_text(json.dumps(state, indent=2), encoding="utf8")


def install(
    state_dir: Path,
    version: str,
    environment: dict[str, str] | None = None,
    status_line: bool = True,
) -> bool:
    """Add Grogu's banner mark to Copilot settings for the current session."""
    path = settings_path(environment)
    with _locked(state_dir):
        state = _read_state(state_dir)
        holders = [pid for pid in state.get("holders", []) if _alive(int(pid))]
        if not holders:
            if state.get("original") is not None:
                # A previous Grogu session was killed before it could restore.
                # Undo it first so the snapshot below is the user's own file.
                _restore_path(Path(state.get("settings_path", str(path))), state)
            existed = path.exists()
            original = path.read_bytes() if existed else b""
            state = {
                "settings_path": str(path),
                "existed": existed,
                "original": base64.b64encode(original).decode("ascii"),
                "holders": [],
            }
        settings = grogu_settings(load_settings(path), version, status_line)
        try:
            write_settings(path, settings)
        except OSError:
            return False
        state["written"] = _digest(settings)
        state["holders"] = holders + [os.getpid()]
        _write_state(state_dir, state)
    return True


def restore(state_dir: Path, pid: int | None = None) -> bool:
    """Drop Grogu's banner keys once the last Grogu session has exited."""
    pid = os.getpid() if pid is None else pid
    with _locked(state_dir):
        state = _read_state(state_dir)
        if not state:
            return False
        holders = [
            held
            for held in state.get("holders", [])
            if int(held) != pid and _alive(int(held))
        ]
        if holders:
            state["holders"] = holders
            _write_state(state_dir, state)
            return False
        path = Path(state.get("settings_path", str(settings_path())))
        _restore_path(path, state)
        _state_file(state_dir).unlink(missing_ok=True)
    return True


def _restore_path(path: Path, state: dict) -> None:
    current = load_settings(path)
    if _digest(current) == state.get("written"):
        # Copilot never rewrote the file; put the original bytes back verbatim
        # so comments and formatting survive.
        original = base64.b64decode(state.get("original", ""))
        target = path.resolve() if path.is_symlink() else path
        if state.get("existed"):
            target.write_bytes(original)
        else:
            target.unlink(missing_ok=True)
        return
    # The user changed settings mid-session; keep their edits and only undo
    # the keys Grogu owns.
    previous = _decode_settings(state.get("original", ""))
    for key in (*GROGU_KEYS, "statusLine"):
        if key == "statusLine":
            active = current.get("statusLine")
            owned = (
                isinstance(active, dict)
                and active.get("command") == _status_line_command()
            )
            if not owned:
                continue
        if key in previous:
            current[key] = previous[key]
        else:
            current.pop(key, None)
    write_settings(path, current)


def _decode_settings(encoded: str) -> dict:
    try:
        raw = base64.b64decode(encoded).decode("utf8")
    except (ValueError, UnicodeDecodeError):
        return {}
    try:
        value = json.loads(strip_jsonc(raw) or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
