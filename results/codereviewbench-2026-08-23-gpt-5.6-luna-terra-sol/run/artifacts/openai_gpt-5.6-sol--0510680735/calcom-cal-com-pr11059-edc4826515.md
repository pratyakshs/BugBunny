# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR11059__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR11059__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `bc89fe00ea84` → `9fde0e906897`
Coverage: 50/50 eligible hunks
Duration: 472.4s; model calls: 5

## Findings (6)

### 1. Import Prisma before using it in the Salesforce refresh path

`high` · `bug` · [packages/app-store/salesforce/lib/CalendarService.ts:96 (RIGHT)](#)

Trigger: Type-checking or building the app-store package after this file is compiled.

Impact: Compilation fails with `Cannot find name 'prisma'`, blocking the build and all Salesforce calendar functionality.

Evidence: `await prisma.credential.update({`

Suggested direction: Add `import prisma from "@calcom/prisma";` with the other imports.

### 2. Persist the parsed Google credential data, not the SafeParse result wrapper

`high` · `data` · [packages/app-store/googlecalendar/lib/CalendarService.ts:97 (RIGHT)](#)

Trigger: Any Google Calendar access-token refresh, including when credential sharing is disabled.

Impact: The database receives an object shaped like `{ success: true, data: ... }` instead of the Google credential itself; the next service construction fails `googleCredentialSchema.parse(credential.key)` and disables the integration.

Evidence: `const key = parseRefreshTokenResponse(googleCredentials, googleCredentialSchema);`

Suggested direction: Persist `parseRefreshTokenResponse(...).data`, or change the helper to return parsed data rather than a SafeParse result and update its other callers consistently.

### 3. Replace the computed Zod properties with a real expiry schema

`high` · `data` · [packages/app-store/_utils/oauth/parseRefreshTokenResponse.ts:8 (RIGHT)](#)

Trigger: Credential sharing is enabled and the synchronization endpoint returns an access token plus `expires_in`, `expiry_date`, or another numeric expiry property.

Impact: The computed key is the literal string representation of a Zod object, not a wildcard property declaration. It also collides with the following computed key, so Zod strips the actual expiry and other token fields from the parsed response.

Evidence: `[z.string().toString()]: z.number(),`

Suggested direction: Use an explicit normalized schema such as `z.object({ access_token: z.string(), expires_in: z.number().optional(), expiry_date: z.number().optional() }).passthrough()` and refine it to require one supported expiry field.

### 4. Do not replace a valid refresh token with a literal placeholder

`high` · `data` · [packages/app-store/_utils/oauth/parseRefreshTokenResponse.ts:26 (RIGHT)](#)

Trigger: The synchronization endpoint follows the documented contract and returns only an access token and expiry, then synchronization is later disabled or unavailable.

Impact: Callers such as Office 365 merge the literal `"refresh_token"` over the credential's real refresh token. A subsequent provider-side refresh sends that placeholder and fails, so the integration cannot fall back to its stored OAuth credential.

Evidence: `refreshTokenResponse.data.refresh_token = "refresh_token";`

Suggested direction: Leave an absent refresh token absent and have each caller preserve its existing token, or pass the existing refresh token to the helper as an explicit fallback.

### 5. Pass the Bigin user's ID instead of the credential ID

`high` · `data` · [packages/app-store/zoho-bigin/lib/CalendarService.ts:93 (RIGHT)](#)

Trigger: A Zoho Bigin credential refresh uses the synchronization endpoint and the credential row ID differs from its owner user ID, as it normally will.

Impact: The request sends the credential primary key as `calcomUserId`, causing the external source to return credentials for the wrong user or reject the request.

Evidence: `credentialId`

Suggested direction: Carry the credential's `userId` into this method and pass that value to `refreshOAuthTokens`; continue using `credentialId` only for the Prisma update.

### 6. Use the refreshed Salesforce token for the connection being returned

`high` · `bug` · [packages/app-store/salesforce/lib/CalendarService.ts:98 (RIGHT)](#)

Trigger: The stored Salesforce access token has expired and the newly added refresh request succeeds.

Impact: The refreshed data is written only to the database; the connection returned later in the same function is still built from the stale `credentialKey.access_token`, so the current calendar operation is unauthorized and only a later reconstruction can see the new token.

Evidence: `data: { key: { ...accessTokenParsed.data, refresh_token: credentialKey.refresh_token } },`

Suggested direction: Create a merged refreshed credential object, persist it, and use that same object for `instanceUrl`, `accessToken`, and `refreshToken` when constructing the connection.

## Audit trail

17 candidate(s) were retained in JSON but excluded from publication.
