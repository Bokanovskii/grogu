# Confirmation-gated iMessage attachments

Plan `p-20260823-6e719b`. This is the account of how the plan changed while
it was being carried out; the stage files next to it are the plan.

## Plan changes accepted mid-flight

- **a1** (raised by the engineer): Confirmed attachment sends must not pass the immutable GROGU_HOME snapshot directly to Messages. After source and snapshot validation, stage a fresh byte-for-byte copy under a private user-only directory beneath a configurable Messages root (defaulting to ~/Library/Messages/.grogu-send-staging/<draft-or-random-id>/), create AppleScript aliases from staged POSIX paths outside the Messages tell block, send body first then aliases in order with a brief delay, and retain staged files because Messages consumes them asynchronously. The immutable reviewed snapshot and hash metadata remain authoritative; staging occurs only after confirmation and validation.
  - resolved: Verified against the current prototype that it passes the source path directly to Messages. The observed Not Delivered behavior is consistent with Messages sandbox access and asynchronous file consumption; the proposed static argv-only script with aliases created outside the tell block compiles on this macOS host. Accept private post-confirmation staging under a configurable Messages root, brief ordered-send delay, and retained staged files.

## Defects found by the tester

- **d1** (resolved, routed to tester): grogu guard staged reports 19 blocking personal-data findings, all newly introduced by this diff's attachment tests in tests/test_grogu_cli.py. Every affected test used an email-shaped synthetic recipient token, so each occurrence tripped the scanner and blocked the sealed testing plan's mandatory privacy check and a normal guarded commit. Test-harness-only defect: replace those values with non-address synthetic handles that still exercise recipient validation, touching no product code. Self-resolving as tester per 'harness wrong stays with the tester'.
