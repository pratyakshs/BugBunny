# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak__augment__PR37634__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak__augment__PR37634__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-4-8`
Base/head: `c22f76867f59` → `daffb05b5ad0`
Coverage: 41/41 eligible hunks
Duration: 149.9s; model calls: 4

## Findings (2)

### 1. isAccessTokenId matcher inverted comparison never validates grant shortcut

`medium` · `test_gap` · [testsuite/integration-arquillian/tests/base/src/test/java/org/keycloak/testsuite/AssertEvents.java:483 (RIGHT)](#)

Trigger: Any event assertion routed through expectCodeToToken/expectDeviceCodeToToken/expectRefresh/expectAuthReqIdToToken, which now use isAccessTokenId(...) to check Details.TOKEN_ID.

Impact: The matcher returns false only when the shortcut MATCHES (and reads the wrong 2 chars via substring(3,5) instead of the grant-type chars at substring(4,6)), so a valid token passes only because the compared substring never equals the expected shortcut; a token carrying the WRONG grant shortcut would also pass. The grant type is effectively never verified.

Evidence: `if (items[0].substring(3, 5).equals(expectedGrantShortcut)) return false;`

Suggested direction: Extract the grant shortcut with items[0].substring(4, 6) and invert the check: return false when it does NOT equal expectedGrantShortcut (i.e. `if (!items[0].substring(4,6).equals(expectedGrantShortcut)) return false;`).

### 2. AccessTokenContext rawTokenId null-check validates wrong argument

`low` · `bug` · [services/src/main/java/org/keycloak/protocol/oidc/encode/AccessTokenContext.java:73 (RIGHT)](#)

Trigger: Constructing AccessTokenContext with a non-null grantType but a null rawTokenId.

Impact: The intended non-null validation of rawTokenId is never performed (grantType is checked twice); a null rawTokenId is accepted and later surfaces as a NullPointerException during encodeTokenId string concatenation or as a malformed token ID instead of a clear IllegalArgument at construction.

Evidence: `Objects.requireNonNull(grantType, "Null rawTokenId not allowed");`

Suggested direction: Change the fourth check to Objects.requireNonNull(rawTokenId, "Null rawTokenId not allowed").
