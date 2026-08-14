---
name: tester-independent-verification-script
description: Write a throwaway, tester-authored Node script that loads the app's own modules and exercises them directly, instead of trusting or extending the engineers' own test files, when hunting a specific class of bug the user flagged.
---

1. Find the test-support loader the engineers already wrote (e.g. test/support/load-waaq.mjs) and reuse ONLY the loading mechanism (it just requires the source files into global scope in dependency order) — never reuse or extend their assertions.
2. Create a scratch .mjs file OUTSIDE test/ (so it can never be mistaken for part of the shipped suite or accidentally picked up by "node --test"), e.g. a .tester-scratch/ directory, and import the loader from there with a relative path.
3. Write assertions against the SPECIFIC concern from steering, not general coverage: pick the exact malformed/edge inputs named in the bug report (empty string, a documented sentinel value like -999, null, undefined, a real legitimate zero/boundary value that must NOT be treated as missing) and call the lowest-level pure function directly (e.g. normalizeAqi(''), overallAqi(row)) rather than only integration-testing through the full client.
4. Pull real rows straight out of the committed fixtures with a quick grep/python/node one-liner (e.g. inspecting a CSV or JSON fixture directly) to find an actual row in the recorded data matching the edge case (e.g. the one real station with PM25_AQI equal to the literal string zero, or a fixture where every hour is the missing-sentinel) — this proves the bug class against real recorded bytes, not just hand-invented fixtures, which is stronger evidence and catches cases the engineer's synthetic test inputs might have missed.
5. Chain multiple related checks (normalize -> category -> full parseReading -> full pipeline function like stationsForArea) in one script so you see the value survive or get lost at each layer, not just the final output.
6. Run the whole script once at the end and read every line — a script that prints one ok/FAIL line per assertion (not just a final pass/fail count) makes it obvious which specific layer broke a value, and self-corrects easily when a test's own setup (e.g. a mocked clock) is wrong rather than the code under test.
7. Clean up the scratch directory before finishing the tester turn.
