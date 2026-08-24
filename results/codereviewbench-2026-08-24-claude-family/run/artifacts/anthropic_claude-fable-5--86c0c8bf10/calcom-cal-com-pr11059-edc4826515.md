# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR11059__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR11059__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-fable-5`
Base/head: `bc89fe00ea84` → `9fde0e906897`
Coverage: 50/50 eligible hunks
Duration: 405.6s; model calls: 4

## Findings (1)

### 1. Google credential key stored as Zod SafeParse wrapper object, corrupting the credential in DB

`critical` · `bug` · [packages/app-store/googlecalendar/lib/CalendarService.ts:97 (RIGHT)](#)

Trigger: Any Google Calendar access-token refresh (expired token) causes this code path to run and persist `key` to the Credential row.

Impact: `parseRefreshTokenResponse` returns the SafeParseReturnType (`{ success, data }`), not the parsed data as the replaced `googleCredentialSchema.parse(...)` did. The wrapper object is written to `credential.key`, so the next `googleCredentialSchema.parse(credential.key)` at service construction throws, permanently breaking the user's Google Calendar integration until reconnection.

Evidence: `const key = parseRefreshTokenResponse(googleCredentials, googleCredentialSchema);`

Suggested direction: Store the parsed payload, e.g. `const parsed = parseRefreshTokenResponse(...); const key = parsed.data;` (or change parseRefreshTokenResponse to return the data directly).

## Audit trail

16 candidate(s) were retained in JSON but excluded from publication.
