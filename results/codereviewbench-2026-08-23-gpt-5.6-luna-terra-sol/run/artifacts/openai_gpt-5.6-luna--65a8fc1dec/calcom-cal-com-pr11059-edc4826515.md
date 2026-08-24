# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR11059__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR11059__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `bc89fe00ea84` → `9fde0e906897`
Coverage: 50/50 eligible hunks
Duration: 202.3s; model calls: 4

## Findings (2)

### 1. The shared-token schema does not match its intended dynamic-key behavior

`high` · `bug` · [packages/app-store/_utils/oauth/parseRefreshTokenResponse.ts:8 (RIGHT)](#)

Trigger: Credential sharing is enabled and the sync endpoint returns a normal payload such as { access_token, expiry_date } or { access_token, expires_in }.

Impact: Both expressions stringify the Zod schema object into a literal property name rather than matching arbitrary keys; Zod therefore strips the actual expiry and other token fields. Downstream integrations receive only access_token, causing lost refresh metadata and invalid expiry calculations or stored credentials.

Evidence: `[z.string().toString()]: z.number(),
  //   Allow other properties in the token response
  [z.string().optional().toString()]: z.unknown().optional(),`

Suggested direction: Use an explicit schema for the supported expiry field(s), or use a passthrough/record schema and validate the presence and numeric type of the intended expiry field without stripping the response.

### 2. Google stores the safe-parse wrapper instead of the credential key

`high` · `data` · [packages/app-store/googlecalendar/lib/CalendarService.ts:97 (RIGHT)](#)

Trigger: Any Google credential refresh reaches this line, including when credential sharing is disabled.

Impact: parseRefreshTokenResponse returns a SafeParseResult, but that wrapper is stored as credential.key. The next Google auth attempt parses { success, data } as a Google credential and fails, permanently breaking the credential until it is reconnected.

Evidence: `const key = parseRefreshTokenResponse(googleCredentials, googleCredentialSchema);`

Suggested direction: Store the parsed payload (`key.data`) after checking success, or change the helper to return the parsed data rather than the SafeParseResult.

## Audit trail

12 candidate(s) were retained in JSON but excluded from publication.
