# The Grogu mark, and why the banner cannot animate

## What ships today

Two things carry Grogu's identity inside an otherwise untouched Copilot CLI:

* **A startup mark.** Grogu writes `companyAnnouncements` in
  `~/.copilot/settings.json`, and Copilot renders that value inside its own
  startup banner block. The value is a 20×14 pixel-art frog child drawn with
  half blocks and 24-bit colour (`src/grogu_banner.py`). Copilot picks one of
  the four supplied frames per launch; three are eyes-open and one is
  eyes-half, so the mark appears to blink between sessions. Each launch also
  randomly selects one phrase from Grogu's precomputed check-the-work list.
* **A one-line animated Grogu.** `statusLine` runs `grogu banner status-line`
  with `refreshInterval: 1`, and Copilot re-runs it on that interval. The eyes
  cycle `●ᵕ●` → `•ᵕ•` → `-ᵕ-` on a five second loop. This is a real animation,
  inside the Copilot UI, using a documented setting.

Both keys are restored on exit — byte for byte when Copilot never rewrote the
file, key by key when the user edited settings mid-session — and a session that
is killed is repaired by the next launch.

## Why the banner itself cannot animate

Copilot renders the banner once, during startup, as part of its own React/Ink
tree. The evidence, from Copilot CLI 1.0.78:

1. `companyAnnouncements` is documented as "list of messages to display in the
   banner on startup". It is read at startup and rendered into a static block.
2. The only re-render hook Copilot exposes to configuration is
   `statusLine.refreshInterval` — "optional integer number of seconds between
   command re-runs (minimum `1`)". There is no equivalent for the banner.
3. Changing `companyAnnouncements` while a session runs does not redraw the
   banner. Copilot's settings watcher notices the change and emits an ephemeral
   `Company announcement:` notification into the timeline instead.
4. Copilot's mascot is hardcoded art inside the binary. No setting, plugin, MCP
   server or custom agent replaces it.

So an animated character *in the banner area* would require repainting a region
of the screen that Copilot owns and redraws on its own schedule. Doing that from
a wrapper means driving Copilot through a pseudo-terminal, tracking its cursor
and re-emitting frames between its own writes: a rewrite of the Copilot frontend
in everything but name, guaranteed to break on the next UI change, and liable to
corrupt the display during scroll, resize or alt-screen transitions.

**Conclusion: not possible without replacing the Copilot UI.** Grogu will not do
that. If in-UI animation is a hard requirement, the honest options are (a) the
status line, which already animates and is what Grogu uses, (b) an ACP or
JSON-RPC client that renders its own frontend around a Copilot session, which is
a separate product rather than a wrapper, or (c) upstream support for an
animated or refreshable banner slot in Copilot itself.

## Controls

| Variable | Effect |
| --- | --- |
| `GROGU_BANNER=0` | Do not touch `~/.copilot/settings.json` at all |
| `GROGU_STATUS_LINE=0` | Install the mark, but not the animated status line |
| `GROGU_TAB_COLOR=0` | Do not colour or title the iTerm2 tab |

```sh
grogu banner show --frame half   # print a frame, for previewing
grogu banner status-line         # print one animation frame
grogu banner restore             # force-restore settings after a hard kill
```

An existing `statusLine` is never replaced; Grogu only installs its own when the
user has none.
