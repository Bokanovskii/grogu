#!/usr/bin/env python3
"""Re-derive the design spec's contrast table from its own token block.

Exit 0 when every ratio stated in the design stage's Accessibility section
matches the ratio computed from the hex values in its Tokens section, within
0.05. Exit 1 with a report otherwise.

This exists because a contrast table is the one part of a design spec that
looks correct whatever it says. Change a token, forget the table, and the spec
still reads as if it were verified.

Usage:
    python3 design_contrast_check.py [path-to-design.md]

With no argument it reads .grogu/plans/p-20260904-6095a8/design.md relative to
the current directory.
"""

import re
import sys
from pathlib import Path

DEFAULT = Path(".grogu/plans/p-20260904-6095a8/design.md")
TOLERANCE = 0.05


def luminance(hex_colour):
    value = hex_colour.lstrip("#")
    channels = [int(value[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def ratio(first, second):
    a, b = luminance(first), luminance(second)
    high, low = max(a, b), min(a, b)
    return (high + 0.05) / (low + 0.05)


def token_blocks(body):
    """The light and dark token maps, in the order they are declared."""
    declarations = re.findall(r"(--[a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})\s*;", body)
    light, dark = {}, {}
    for name, colour in declarations:
        target = light if name not in light else dark
        target[name] = colour
    return light, dark


def stated_rows(body):
    """(label, light ratio, dark ratio) for every row of the contrast table."""
    rows = []
    pattern = re.compile(
        r"^(--[a-z0-9- ]+?(?:\s+on\s+--[a-z0-9-]+)?)\s+"
        r"([0-9]+\.[0-9]+)\s*:\s*1\s+([0-9]+\.[0-9]+)\s*:\s*1\s*$"
    )
    for line in body.splitlines():
        match = pattern.match(line.strip())
        if match:
            rows.append(
                (match.group(1).strip(), float(match.group(2)), float(match.group(3)))
            )
    return rows


def resolve(label, light, dark):
    """(light pair, dark pair) of hex colours for a table row's label."""
    if " on " in label:
        foreground, background = [part.strip() for part in label.split(" on ")]
    else:
        foreground, background = label.strip(), "--color-bg"
    try:
        return (
            (light[foreground], light[background]),
            (dark[foreground], dark[background]),
        )
    except KeyError as missing:
        raise SystemExit(f"no token {missing} declared for row '{label}'")


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not path.is_file():
        raise SystemExit(f"no design spec at {path}")
    body = path.read_text(encoding="utf-8")

    light, dark = token_blocks(body)
    if not light or not dark:
        raise SystemExit("could not find both the light and dark token blocks")

    rows = stated_rows(body)
    if len(rows) < 8:
        raise SystemExit(
            f"found {len(rows)} contrast rows; the spec states at least 8"
        )

    failures = []
    for label, stated_light, stated_dark in rows:
        (lf, lb), (df, db) = resolve(label, light, dark)
        actual_light, actual_dark = ratio(lf, lb), ratio(df, db)
        if abs(actual_light - stated_light) > TOLERANCE:
            failures.append(
                f"{label} light: spec says {stated_light:.2f}, "
                f"{lf} on {lb} is {actual_light:.2f}"
            )
        if abs(actual_dark - stated_dark) > TOLERANCE:
            failures.append(
                f"{label} dark: spec says {stated_dark:.2f}, "
                f"{df} on {db} is {actual_dark:.2f}"
            )

    # The floors the spec commits to, restated as checks rather than prose.
    text_tokens = ["--color-ink", "--color-ink-2", "--color-accent", "--color-attention"]
    for name in text_tokens:
        for mode, palette in (("light", light), ("dark", dark)):
            found = ratio(palette[name], palette["--color-bg"])
            if found < 4.5:
                failures.append(f"{name} {mode} is {found:.2f}:1, below the 4.5:1 floor")
    for name in ("--color-rule-strong", "--mark-line"):
        for mode, palette in (("light", light), ("dark", dark)):
            found = ratio(palette[name], palette["--color-bg"])
            if found < 3.0:
                failures.append(f"{name} {mode} is {found:.2f}:1, below the 3:1 floor")

    if failures:
        print(f"{len(failures)} contrast statement(s) do not hold:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print(f"{len(rows)} contrast rows re-derived from the token block; all hold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
