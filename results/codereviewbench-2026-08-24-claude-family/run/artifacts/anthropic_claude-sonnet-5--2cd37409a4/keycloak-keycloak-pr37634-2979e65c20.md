# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak__augment__PR37634__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak__augment__PR37634__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-sonnet-5`
Base/head: `c22f76867f59` → `daffb05b5ad0`
Coverage: 41/41 eligible hunks
Duration: 362.6s; model calls: 4

## Findings (2)

### 1. AccessTokenContext constructor checks grantType twice instead of validating rawTokenId

`medium` · `bug` · [services/src/main/java/org/keycloak/protocol/oidc/encode/AccessTokenContext.java:73 (RIGHT)](#)

Trigger: Constructing an AccessTokenContext with a null rawTokenId (e.g. from getTokenContextFromClientSessionContext/getTokenContextFromTokenId with an unexpected null id)

Impact: A null rawTokenId is never rejected at construction time; later, encodeTokenId concatenates it into the token id string (`':' + tokenContext.getRawTokenId()`), silently embedding the literal string "null" into the generated access token id instead of failing fast with a clear error.

Evidence: `Objects.requireNonNull(grantType, "Null rawTokenId not allowed");`

Suggested direction: Fix the duplicated check to validate rawTokenId: `Objects.requireNonNull(rawTokenId, "Null rawTokenId not allowed");`

### 2. isAccessTokenId test matcher never actually validates the grant-type shortcut

`medium` · `test_gap` · [testsuite/integration-arquillian/tests/base/src/test/java/org/keycloak/testsuite/AssertEvents.java:483 (RIGHT)](#)

Trigger: Any test using expectCodeToToken/expectDeviceCodeToToken/expectRefresh/expectAuthReqIdToToken, which internally use isAccessTokenId(...) to check the grant-type shortcut encoded in a token id

Impact: The substring range is off by one (grant type occupies chars 4-6 per DefaultTokenContextEncoderProvider's 6-char context prefix, not 3-5), and the condition additionally only returns false when the (wrong) substring equals the expected shortcut, so the matcher effectively always succeeds regardless of the actual grant-type shortcut, giving false confidence in these test assertions.

Evidence: `if (items[0].substring(3, 5).equals(expectedGrantShortcut)) return false;`

Suggested direction: Use the correct index range and invert the condition: `if (!items[0].substring(4, 6).equals(expectedGrantShortcut)) return false;`

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
