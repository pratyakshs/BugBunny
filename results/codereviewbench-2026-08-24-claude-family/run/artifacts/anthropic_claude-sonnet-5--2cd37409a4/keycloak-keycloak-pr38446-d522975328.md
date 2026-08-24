# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak__augment__PR38446__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak__augment__PR38446__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-sonnet-5`
Base/head: `85737f52b59a` → `ce2dcc537b4b`
Coverage: 20/20 eligible hunks
Duration: 406.8s; model calls: 4

## Findings (1)

### 1. getCredentials() dereferences MyUser without the null-guard used elsewhere in the class

`medium` · `bug` · [testsuite/integration-arquillian/servers/auth-server/services/testsuite-providers/src/main/java/org/keycloak/testsuite/federation/BackwardsCompatibilityUserStorage.java:235 (RIGHT)](#)

Trigger: getCredentials(realm, user) is invoked (e.g. via credentialManager().getFederatedCredentialsStream()/getCredentials()) for a user whose entry is not present in the internal `users` map (getMyUser returns null), a situation the sibling methods isConfiguredFor and getDisableableCredentialTypesStream both explicitly guard against.

Impact: NullPointerException is thrown from getCredentials(), breaking any caller that enumerates federated credentials (e.g. account REST credential listing) for the affected user.

Evidence: `if (myUser.recoveryCodes != null) {`

Suggested direction: Add the same `if (myUser == null) return Stream.empty();` guard used in getDisableableCredentialTypesStream/isConfiguredFor before dereferencing myUser.

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
