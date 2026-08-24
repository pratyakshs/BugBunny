# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak__augment__PR37634__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak__augment__PR37634__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-fable-5`
Base/head: `c22f76867f59` → `daffb05b5ad0`
Coverage: 41/41 eligible hunks
Duration: 317.6s; model calls: 4

## Findings (2)

### 1. Duplicated null-check validates grantType twice instead of rawTokenId

`low` · `bug` · [services/src/main/java/org/keycloak/protocol/oidc/encode/AccessTokenContext.java:73 (RIGHT)](#)

Trigger: Constructing an AccessTokenContext with a null rawTokenId (e.g. a custom TokenContextEncoderProvider or caller passing null as the generated ID).

Impact: A null rawTokenId silently passes construction; encodeTokenId then concatenates it as the literal string "null" (e.g. "onrtac:null"), producing a corrupt token ID instead of failing fast with the intended NullPointerException message.

Evidence: `Objects.requireNonNull(grantType, "Null rawTokenId not allowed");`

Suggested direction: Change the second duplicated check to Objects.requireNonNull(rawTokenId, "Null rawTokenId not allowed");

### 2. isAccessTokenId matcher condition is inverted, making the grant-shortcut assertion vacuous

`medium` · `test_gap` · [testsuite/integration-arquillian/tests/base/src/test/java/org/keycloak/testsuite/AssertEvents.java:483 (RIGHT)](#)

Trigger: Any test using expectCodeToToken/expectRefresh/expectDeviceCodeToToken/expectAuthReqIdToToken; a token ID whose grant shortcut does NOT match the expected shortcut still satisfies the matcher.

Impact: The matcher returns false when the shortcut equals the expectation and true when it differs, so tokens encoded with the wrong grant type pass the assertion (and a correctly encoded token would fail it), removing the regression coverage this matcher was added to provide.

Evidence: `if (items[0].substring(3, 5).equals(expectedGrantShortcut)) return false;`

Suggested direction: Negate the comparison: if (!items[0].substring(4, 6).equals(expectedGrantShortcut)) return false;

## Audit trail

4 candidate(s) were retained in JSON but excluded from publication.
