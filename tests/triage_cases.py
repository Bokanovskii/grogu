"""Routing cases for `grogu plan triage`, kept as data rather than as tests.

The triage rule is the one piece of the pipeline that runs on every single
request, and it is deliberately not a model call — so the only way to know
whether a change to it is an improvement is to check it against a corpus of
things a user actually says. Each case is a real phrasing, not a synthetic one
built to satisfy the current patterns.

`plan` means the architect should be involved before anything is edited.
`direct` means answering or acting immediately is correct, and a planning cycle
would be waste.
"""

CASES = [
    # -- genuine build work, phrased with an explicit build verb -------------
    ("build a retry helper for the http client", "plan"),
    ("implement pagination on the search endpoint", "plan"),
    ("add support for webhooks so partners get delivery callbacks", "plan"),
    ("refactor the storage layer", "plan"),
    ("migrate the job queue off redis", "plan"),
    ("rewrite the importer to stream instead of loading everything", "plan"),

    # -- genuine build work phrased as a change request, no build verb -------
    # This is the shape that reads as small and is not.
    ("make the billing exporter round correctly for currencies with "
     "different minor units and stop float drift", "plan"),
    ("make the parser handle unicode names properly", "plan"),
    ("get the scheduler to stop double-firing jobs when two workers race",
     "plan"),
    ("the importer needs to cope with timezones properly, right now "
     "everything is stored as naive local time", "plan"),
    ("our session tokens never expire and they should", "plan"),
    ("fix the cache so it invalidates when the underlying row changes",
     "plan"),

    # -- multi-part requests -------------------------------------------------
    ("add a rate limiter to the api and then wire it into the gateway",
     "plan"),
    ("split the monolith config into per-service files as well as adding "
     "validation", "plan"),

    # -- explicitly requested ------------------------------------------------
    ("plan out how we should do offline sync", "plan"),
    ("give me a plan for the search index", "plan"),

    # -- genuinely small -----------------------------------------------------
    ("fix the typo in the readme", "direct"),
    ("rename the variable foo to bar in parser.py", "direct"),
    ("bump the version to 2.1.0", "direct"),
    ("add a docstring to convert()", "direct"),
    ("run the tests", "direct"),
    ("format the file", "direct"),

    # -- questions and retrieval ---------------------------------------------
    ("what does the retry helper do", "direct"),
    ("why is the build failing", "direct"),
    ("show me where we parse the config", "direct"),
    ("explain how the plan store locks", "direct"),
    ("is the api rate limited today?", "direct"),
    ("summarise what changed on this branch", "direct"),

    # -- steering and asides -------------------------------------------------
    ("fyi the staging box is down", "direct"),
    ("heads up, prefer the shared client for new http calls", "direct"),
    ("just check whether the migration ran", "direct"),
    ("quick question about the gate logic", "direct"),

    # -- explicit direct override on real work -------------------------------
    ("no plan needed, just add the index on user_id", "direct"),
    ("skip the plan and rewrite the parser", "direct"),

    # -- not software at all -------------------------------------------------
    ("plan a trip to japan for october", "direct"),
    ("help me plan my week", "direct"),
    ("draft a reply to the landlord about the lease", "direct"),
    ("research the best espresso machine under 1000 dollars", "direct"),
    ("what did sam say about dinner", "direct"),

    # -- words that are nouns here and verbs elsewhere -----------------------
    # Every one of these was routed wrongly before the corpus existed.
    ("build a search index for my notes", "plan"),
    ("add a status page", "plan"),
    ("implement the read replica failover", "plan"),
    ("i need to know why the build fails", "direct"),
    ("the build is broken", "direct"),
    ("why did the design change", "direct"),
    ("list the files that touch pagination", "direct"),
    ("add a comment explaining the retry logic", "direct"),
    ("fix the typo in the timezone docs", "direct"),
    ("we need to check whether the cache is stale", "direct"),
    ("we need proper multi-tenant isolation", "plan"),
    ("the exporter should never emit duplicate rows", "plan"),
    # Integration phrasing. These sound like plumbing and are not: each one
    # adds a failure surface, and most of them add a credential.
    ("wire up the notifier to actually send mail through our smtp account", "plan"),
    ("hook the exporter up to the new billing api", "plan"),
    ("switch us over to the new auth provider", "plan"),
    ("replace the csv writer with the parquet one", "plan"),
    ("integrate the search backend", "plan"),
    ("point the worker at the staging queue", "plan"),
    # ... but asking about an integration is still a question.
    ("what does the notifier hook up to", "direct"),
    ("show me where we integrate with stripe", "direct"),
]
