# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR11059__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR11059__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-terra`
Base/head: `bc89fe00ea84` → `9fde0e906897`
Coverage: 50/50 eligible hunks
Duration: 180.9s; model calls: 4

## Findings (7)

### 1. Import Prisma before using it in Salesforce token refresh

`high` · `bug` · [packages/app-store/salesforce/lib/CalendarService.ts:96 (RIGHT)](#)

Trigger: Any build, type-check, or runtime compilation that includes the Salesforce calendar service.

Impact: Compilation fails with `Cannot find name 'prisma'`, preventing the app-store package/application from building.

Evidence: `await prisma.credential.update({`

Suggested direction: Add `import prisma from "@calcom/prisma";` alongside the other imports in this file.

### 2. Do not return a Fetch Response to callers expecting provider token objects

`high` · `bug` · [packages/app-store/_utils/oauth/refreshOAuthTokens.ts:15 (RIGHT)](#)

Trigger: Credential syncing is enabled and a HubSpot, Zoho Bigin, or Zoho CRM credential refresh invokes the configured HTTP sync endpoint.

Impact: Those callers receive a native `Response` where they expect an SDK result or Axios result with token fields/`.data`; token fields become undefined or `.data` access throws, so refresh and subsequent integration operations fail.

Evidence: `return response;`

Suggested direction: Define a normalized sync-endpoint result contract and adapt the fetch response (for example, parse `await response.json()`) before returning it to SDK/Axios callers, while preserving native-response handling for callers that require it.

### 3. Preserve expiry fields from minimal sync responses

`medium` · `bug` · [packages/app-store/_utils/oauth/parseRefreshTokenResponse.ts:8 (RIGHT)](#)

Trigger: The credential-sync endpoint returns the documented minimal payload containing `access_token` and an expiry field such as `expiry_date` or `expires_in`.

Impact: Zod treats the computed key as the literal `"[object Object]"` rather than an index signature and strips unknown expiry fields, so refreshed credentials lose their expiry and are repeatedly refreshed or persisted incomplete.

Evidence: `[z.string().toString()]: z.number(),`

Suggested direction: Use an explicit minimal schema such as `z.object({ access_token: z.string() }).passthrough()` (and validate supported expiry keys explicitly if required) so dynamic token fields are retained.

### 4. Store Google credential data instead of the SafeParse wrapper

`high` · `bug` · [packages/app-store/googlecalendar/lib/CalendarService.ts:97 (RIGHT)](#)

Trigger: A Google Calendar credential reaches its refresh path, with or without credential syncing enabled.

Impact: The database update stores `{ success: true, data: ... }` as the credential key instead of the token object. The next Google Calendar initialization cannot parse the expected top-level credential fields, disabling the credential.

Evidence: `const key = parseRefreshTokenResponse(googleCredentials, googleCredentialSchema);`

Suggested direction: Store the parsed payload (`parseRefreshTokenResponse(...).data`) after validating it, rather than the `safeParse` result object.

### 5. Pass the Zoho Bigin user ID to the sync endpoint

`high` · `bug` · [packages/app-store/zoho-bigin/lib/CalendarService.ts:93 (RIGHT)](#)

Trigger: Credential syncing is enabled and an expired Zoho Bigin user credential is refreshed.

Impact: The outbound payload's `calcomUserId` is the credential record primary key rather than the Cal.com user ID, causing the remote app store to reject the request or return credentials for the wrong user.

Evidence: `credentialId`

Suggested direction: Carry `credential.userId` into `refreshAccessToken` and pass that value to `refreshOAuthTokens`; retain `credentialId` only for the database update.

### 6. Route Salesforce refreshes through the configured sync endpoint

`high` · `bug` · [packages/app-store/salesforce/lib/CalendarService.ts:75 (RIGHT)](#)

Trigger: Credential syncing is enabled and a Salesforce calendar service is created for a credential that should be sourced from the configured external app store.

Impact: Salesforce always sends the locally stored refresh token directly to Salesforce and never invokes `CALCOM_CREDENTIAL_SYNC_ENDPOINT`; self-hosted source-of-truth synchronization therefore does not work for this integration and fails if the local credential has no usable refresh token.

Evidence: `const response = await fetch("https://login.salesforce.com/services/oauth2/token", {`

Suggested direction: Wrap the Salesforce provider refresh in `refreshOAuthTokens` and adapt the configured endpoint response using the same minimal-token contract as the other supported integrations.

### 7. Avoid replacing missing refresh tokens with a sentinel value

`medium` · `data` · [packages/app-store/_utils/oauth/parseRefreshTokenResponse.ts:26 (RIGHT)](#)

Trigger: A sync endpoint returns its documented minimal access-token-and-expiry payload without a refresh token, and a caller merges the parsed result into persisted credentials (for example Office 365 calendar).

Impact: The valid previously stored refresh token is overwritten with the literal `"refresh_token"`; disabling sync later or falling back to the provider refresh leaves the user with an unrecoverable credential.

Evidence: `refreshTokenResponse.data.refresh_token = "refresh_token";`

Suggested direction: Treat `refresh_token` as optional for minimal sync results and preserve the existing credential's refresh token at each persistence site instead of injecting a placeholder.

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
