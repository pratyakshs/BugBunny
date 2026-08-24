# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR3__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR3__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `e25638dab0d4` → `5f8a130277db`
Coverage: 13/13 eligible hunks
Duration: 417.1s; model calls: 4

## Findings (2)

### 1. Normalize addresses before querying the blocked-email table

`high` · `security` · [app/models/blocked_email.rb:12 (RIGHT)](#)

Trigger: A blocked address is submitted with casing different from the corresponding `blocked_emails.email` row, such as `User@Example.com` versus `user@example.com`.

Impact: The lookup returns no record and the account passes the new blocklist check, allowing a trivial case-change bypass of the anti-abuse control.

Evidence: `record = BlockedEmail.where(email: email).first`

Suggested direction: Canonicalize both stored and queried addresses with the same normalization used for user emails, and enforce uniqueness on that canonical representation.

### 2. Nest the blocked translation under the email key

`medium` · `bug` · [config/locales/server.en.yml:734 (RIGHT)](#)

Trigger: Validation rejects an address because `BlockedEmail.should_block?` returns true and requests `user.email.blocked`.

Impact: The configured key is actually `user.blocked`, so users receive a missing-translation message instead of the intended validation error.

Evidence: `blocked: "is not allowed."`

Suggested direction: Indent `blocked` to the same level as `not_allowed` so the resulting key is `user.email.blocked`, and assert the exact error text in the validator spec.

## Audit trail

4 candidate(s) were retained in JSON but excluded from publication.
