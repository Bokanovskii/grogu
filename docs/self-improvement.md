# Self-improvement contract

Grogu improves through evidence, not by silently rewriting its own behavior.
The initial contract has three layers:

1. **Telemetry:** redacted events in the local SQLite trace database.
2. **Repository intelligence:** deterministic, versioned indexes in each target
   repository.
3. **Evaluation evidence:** task outcomes, verification results, failures, and
   user feedback linked to a repository, session, or task.

## Recording evidence

```sh
grogu telemetry record \
  --event verification \
  --outcome passed \
  --repository-id "$GROGU_REPOSITORY_ID" \
  --task-id "$TASK_ID" \
  --payload '{"tests":28,"command":"python3 -m unittest discover -s tests -q"}'

grogu telemetry list --limit 50
grogu telemetry summary
```

Payloads are recursively redacted for common secret keys and token formats
before storage. Conversation transcripts and source contents do not belong in
telemetry by default.

## Improvement loop

1. Capture a failure, regression, verification result, or explicit feedback.
2. Group repeated evidence by repository, task, workflow, and Grogu version.
3. Propose a narrowly scoped harness, instruction, skill, or test change.
4. Add a regression test or evaluation case that reproduces the weakness.
5. Implement on a branch, run the relevant repository checks, and open a pull
   request.
6. Record the result and keep the change only when the evidence improves.

Azure is not used automatically by this loop. Provider and model choices remain
explicit until a separately implemented budget ledger authorizes them.
