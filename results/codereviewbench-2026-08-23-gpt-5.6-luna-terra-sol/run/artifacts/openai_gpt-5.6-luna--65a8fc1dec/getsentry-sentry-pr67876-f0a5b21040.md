# BugBunny review

PR: [code-review-benchmark/sentry__sentry__augment__PR67876__20260122#1](https://github.com/code-review-benchmark/sentry__sentry__augment__PR67876__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `344aa102e781` → `bb75657fc8f1`
Coverage: 11/11 eligible hunks
Duration: 182.5s; model calls: 4

## Findings (1)

### 1. Missing sender metadata causes an installation request to crash

`medium` · `bug` · [src/sentry/integrations/github/integration.py:503 (RIGHT)](#)

Trigger: An active, unassociated GitHub Integration has no sender entry in its metadata, such as a legacy or incompletely created installation.

Impact: The request raises KeyError and returns an internal server error instead of the installation failure response.

Evidence: `!= integration.metadata["sender"]["login"]`

Suggested direction: Read sender metadata defensively and return the invalid-installation error when sender or login is missing before comparing it with the authenticated user.

## Audit trail

5 candidate(s) were retained in JSON but excluded from publication.
