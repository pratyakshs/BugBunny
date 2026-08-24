# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR11059__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR11059__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-4-8`
Base/head: `bc89fe00ea84` → `9fde0e906897`
Coverage: 50/50 eligible hunks
Duration: 196.2s; model calls: 4

## Findings (1)

### 1. minimumTokenResponseSchema uses colliding computed keys that both stringify to "[object Object]"

`medium` · `bug` · [packages/app-store/_utils/oauth/parseRefreshTokenResponse.ts:8 (RIGHT)](#)

Trigger: Any call to parseRefreshTokenResponse while APP_CREDENTIAL_SHARING_ENABLED and CALCOM_CREDENTIAL_SYNC_ENDPOINT are set, so minimumTokenResponseSchema is used.

Impact: Both computed property keys evaluate to the string "[object Object]" (Object.prototype.toString on a ZodString), so the second definition overwrites the first and the schema only ever validates `access_token` plus a single optional `[object Object]` key; the intended "any numeric property is the expiry / allow other properties" validation never happens.

Evidence: `[z.string().toString()]: z.number(),`

Suggested direction: Use z.object with `.passthrough()` (or a `.catchall(z.unknown())`) and an explicitly named expiry field instead of computed keys built from `z.string().toString()`.

## Audit trail

5 candidate(s) were retained in JSON but excluded from publication.
