---
name: browser-validate
description: Validate web behavior with the configured Playwright MCP server when a browser is available.
---

Use Playwright for user-visible web behavior, navigation, forms, and browser-specific regressions. Prefer headless, isolated, read-only checks when possible. Keep browser output focused and avoid returning large accessibility trees when a targeted assertion is sufficient.

If Playwright is unavailable, report that clearly and use the repository's existing browser test command instead. Do not treat browser access as permission to send messages, submit purchases, or perform other external side effects without confirmation.
