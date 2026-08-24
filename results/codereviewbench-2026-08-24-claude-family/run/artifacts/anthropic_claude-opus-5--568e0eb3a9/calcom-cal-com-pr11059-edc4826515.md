# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR11059__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR11059__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-5`
Base/head: `bc89fe00ea84` → `9fde0e906897`
Coverage: 50/50 eligible hunks
Duration: 253.4s; model calls: 4

## Findings (2)

### 1. Duplicate computed keys make minimumTokenResponseSchema validate nothing but access_token

`high` · `bug` · [packages/app-store/_utils/oauth/parseRefreshTokenResponse.ts:8 (RIGHT)](#)

Trigger: Any refresh with APP_CREDENTIAL_SHARING_ENABLED and CALCOM_CREDENTIAL_SYNC_ENDPOINT set, where the self-hosted sync endpoint returns a payload with no expiry field or a non-numeric expiry.

Impact: Both computed keys stringify a ZodType object to the same literal property name ("[object Object]"), so the second definition overwrites the first and the schema only ever requires `access_token`; the intended "expiry must be a number" validation never runs and malformed sync responses are accepted and persisted.

Evidence: `[z.string().toString()]: z.number(),
  //   Allow other properties in the token response
  [z.string().optional().toString()]: z.unknown().optional(),`

Suggested direction: Replace the computed-key trick with an explicit shape plus passthrough, e.g. `z.object({ access_token: z.string(), expires_in: z.number().optional(), expiry_date: z.number().optional() }).passthrough()`, or use `z.record(z.unknown())` refinement to check the expiry field by name.

### 2. Google Calendar stores the zod SafeParse result object as the credential key

`critical` · `data` · [packages/app-store/googlecalendar/lib/CalendarService.ts:97 (RIGHT)](#)

Trigger: Any Google Calendar access-token refresh after this change.

Impact: `parseRefreshTokenResponse` returns a SafeParse result (`{ success: true, data: {...} }`), so `prisma.credential.update({ data: { key } })` writes that wrapper object instead of the token fields; subsequent `googleCredentialSchema.parse(credential.key)` fails and the Google Calendar integration breaks for the user.

Evidence: `const key = parseRefreshTokenResponse(googleCredentials, googleCredentialSchema);`

Suggested direction: Store the parsed payload, e.g. `const key = parseRefreshTokenResponse(googleCredentials, googleCredentialSchema).data;` (after checking `.success`).

## Audit trail

11 candidate(s) were retained in JSON but excluded from publication.
