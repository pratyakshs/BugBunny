# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR3__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR3__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-terra`
Base/head: `e25638dab0d4` → `5f8a130277db`
Coverage: 13/13 eligible hunks
Duration: 140.1s; model calls: 4

## Findings (2)

### 1. Normalize email before checking the blocked-email table

`medium` · `security` · [app/models/blocked_email.rb:12 (RIGHT)](#)

Trigger: A blocked entry is stored as `person@example.com`, and a signup submits the same mailbox with a differently cased domain such as `person@EXAMPLE.COM`.

Impact: The equality lookup misses the blocked entry, so the new user validation accepts an email address that should be blocked. Domain names are case-insensitive, and this path does not normalize the submitted value before querying.

Evidence: `record = BlockedEmail.where(email: email).first`

Suggested direction: Canonicalize blocked-email values on write and canonicalize the lookup value using the application's email normalization (for example, `Email.downcase`) before the equality query; add coverage for mixed-case domains.

### 2. Make blocked-email match counters atomic

`low` · `concurrency` · [app/models/blocked_email.rb:14 (RIGHT)](#)

Trigger: Two signup validations for the same blocked email run concurrently and both load the record before either saves its increment.

Impact: Both requests can save the same incremented value, losing one or more matches and making `match_count` inaccurate under concurrent attempts.

Evidence: `record.match_count += 1`

Suggested direction: Perform the increment and timestamp update with an atomic SQL update (or lock the row in a transaction) rather than incrementing an in-memory record and saving it.

## Audit trail

2 candidate(s) were retained in JSON but excluded from publication.
