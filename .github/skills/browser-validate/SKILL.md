---
name: browser-validate
description: Validate web behavior with the configured Playwright MCP server when a browser is available.
---

Use Playwright for user-visible web behavior, navigation, forms, and browser-specific regressions.

Always run validation in a fresh, automation-owned browser context with its own
temporary profile. Never attach to, inspect, or drive the user's current browser
window, tabs, profile, or remote-debugging session. Do not rely on the active
window, focused element, or global mouse and keyboard state.

Prefer headless, read-only checks. When a visible browser is necessary, launch a
separate headed browser/context dedicated to the task and address its pages and
elements through Playwright handles so the user's window switching or clicking
cannot redirect the automation. Recreate the isolated context rather than
borrowing the user's session when authentication or state is missing.

Keep browser output focused and avoid returning large accessibility trees when
a targeted assertion is sufficient.

If Playwright is unavailable, report that clearly and use the repository's
existing browser test command instead. Do not treat browser access as permission
to send messages, submit purchases, or perform other external side effects
without confirmation.
