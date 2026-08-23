# Testing plan

1. Store tests: establish an engineer identity, verify that the same identity cannot read a sealed testing stage as tester or architect, and verify the denied access is recorded without exposing the body.
2. Persistence tests: bind an identified engineer through a brief, clear `GROGU_ROLE` and `GROGU_AGENT` to simulate a fresh shell, and verify a tester claim remains refused from the persisted session identity.
3. Distinct-agent tests: after an engineer is present, verify a fresh tester identity can read testing and a fresh architect identity can read/write appropriate stages; verify briefs bind each legitimate identity and reject role switching by an existing identity.
4. Human and routing regression tests: verify `--as-user` stage completion/finalization remains available, steering another role remains a subject operation rather than impersonation, and brief/steering delivery still uses stable identities across fresh shells.
5. Run targeted plan-store and CLI tests, then the full existing suite plus repository guard and diff checks.
