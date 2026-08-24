# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak__augment__PR37634__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak__augment__PR37634__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-5`
Base/head: `c22f76867f59` → `daffb05b5ad0`
Coverage: 41/41 eligible hunks
Duration: 340.9s; model calls: 5

## Findings (2)

### 1. rawTokenId null-check duplicates grantType check

`medium` · `bug` · [services/src/main/java/org/keycloak/protocol/oidc/encode/AccessTokenContext.java:73 (RIGHT)](#)

Trigger: Any caller constructing AccessTokenContext with a null rawTokenId (e.g. a custom TokenContextEncoderProvider or a future call site passing a not-yet-generated id).

Impact: The intended precondition is not enforced: a context with null rawTokenId is accepted and later encodeTokenId() produces a token id ending in ':null' instead of failing fast with a clear message.

Evidence: `Objects.requireNonNull(grantType, "Null rawTokenId not allowed");`

Suggested direction: Change the fourth check to Objects.requireNonNull(rawTokenId, "Null rawTokenId not allowed").

### 2. isAccessTokenId inverts the grant shortcut comparison

`medium` · `test_gap` · [testsuite/integration-arquillian/tests/base/src/test/java/org/keycloak/testsuite/AssertEvents.java:483 (RIGHT)](#)

Trigger: Running any test that uses expectCodeToToken/expectRefresh/expectDeviceCodeToToken/expectAuthReqIdToToken with the new matcher.

Impact: The matcher rejects ids whose extracted shortcut equals the expected one and accepts all others, i.e. the grant-type check is logically reversed and cannot detect a wrong grant shortcut.

Evidence: `if (items[0].substring(3, 5).equals(expectedGrantShortcut)) return false;`

Suggested direction: Negate the condition: if (!items[0].substring(4, 6).equals(expectedGrantShortcut)) return false;

## Audit trail

8 candidate(s) were retained in JSON but excluded from publication.
