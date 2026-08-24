# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak__augment__PR38446__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak__augment__PR38446__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-terra`
Base/head: `85737f52b59a` → `ce2dcc537b4b`
Coverage: 20/20 eligible hunks
Duration: 141.4s; model calls: 4

## Findings (2)

### 1. The recovery-code test provider accepts the same recovery code indefinitely

`medium` · `test_gap` · [testsuite/integration-arquillian/servers/auth-server/services/testsuite-providers/src/main/java/org/keycloak/testsuite/federation/BackwardsCompatibilityUserStorage.java:340 (RIGHT)](#)

Trigger: The same generated recovery code is submitted in two separate authentication attempts against the added federated-storage test provider.

Impact: Both attempts authenticate because validation only checks membership and never removes the matched code, so the new integration test does not exercise the one-time-use recovery-code behavior.

Evidence: `return generatedKeys.stream().anyMatch(key -> key.equals(input.getChallengeResponse()));`

Suggested direction: On a successful match, remove the matched code from the persisted recovery-code list before returning true, and add a test that verifies replay is rejected.

### 2. The new recovery login test expects zero for a one-based recovery-code number

`medium` · `test_gap` · [testsuite/integration-arquillian/tests/base/src/test/java/org/keycloak/testsuite/federation/storage/BackwardsCompatibilityUserStorageTest.java:259 (RIGHT)](#)

Trigger: testRecoveryKeysSetupAndLogin reaches its first recovery-code prompt.

Impact: The test asserts that the displayed recovery-code number is 0, but recovery codes are created with numbers starting at 1, causing the newly added test to fail before submitting a code.

Evidence: `enterRecoveryCodes(enterRecoveryAuthnCodePage, driver, 0, recoveryKeys);`

Suggested direction: Pass 1 as the expected first displayed code number (and retain the number as a one-based display value).

## Audit trail

4 candidate(s) were retained in JSON but excluded from publication.
