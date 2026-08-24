# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak__augment__PR37634__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak__augment__PR37634__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-terra`
Base/head: `c22f76867f59` → `daffb05b5ad0`
Coverage: 41/41 eligible hunks
Duration: 130.4s; model calls: 4

## Findings (4)

### 1. Preserve binary compatibility for existing OAuth2 grant factories

`medium` · `api` · [server-spi-private/src/main/java/org/keycloak/protocol/oidc/grants/OAuth2GrantTypeFactory.java:33 (RIGHT)](#)

Trigger: A deployment contains an OAuth2GrantTypeFactory implementation compiled against the previous SPI version, before this abstract method existed.

Impact: During TokenContextEncoderProviderFactory.postInit, invoking getShortcut on that provider throws AbstractMethodError, preventing server startup; recompiling the extension also fails until it implements the new method.

Evidence: `String getShortcut();`

Suggested direction: Add a backward-compatible default getShortcut implementation and make the encoder treat factories without a shortcut as the unknown grant type, or introduce a versioned/optional capability rather than adding an abstract SPI method.

### 2. Restore the removed OAuth2GrantType.Context copy constructor

`medium` · `api` · [server-spi-private/src/main/java/org/keycloak/protocol/oidc/grants/OAuth2GrantType.java:102 (LEFT)](#)

Trigger: An extension compiled against the prior server SPI clones a grant Context with new OAuth2GrantType.Context(existingContext).

Impact: The extension fails with NoSuchMethodError at runtime (and source extensions fail to compile), even though this public constructor was previously available.

Evidence: `public Context(Context context) {`

Suggested direction: Restore the copy constructor and copy the new grantType field as well as the existing fields.

### 3. Validate rawTokenId instead of validating grantType twice

`low` · `bug` · [services/src/main/java/org/keycloak/protocol/oidc/encode/AccessTokenContext.java:73 (RIGHT)](#)

Trigger: A caller constructs AccessTokenContext with a non-null grantType and a null rawTokenId, such as a custom TokenContextEncoderProvider implementation assembling context from incomplete metadata.

Impact: Construction incorrectly succeeds and encodeTokenId serializes the missing identifier as the literal suffix "null", producing a misleading token ID rather than rejecting invalid context.

Evidence: `Objects.requireNonNull(grantType, "Null rawTokenId not allowed");`

Suggested direction: Replace the second argument with rawTokenId so the constructor enforces its stated non-null invariant.

### 4. Fix the access-token event matcher so it verifies the grant shortcut

`low` · `test_gap` · [testsuite/integration-arquillian/tests/base/src/test/java/org/keycloak/testsuite/AssertEvents.java:483 (RIGHT)](#)

Trigger: An access token is issued with an incorrect grant shortcut while an event assertion calls isAccessTokenId with the expected shortcut.

Impact: The assertion normally still passes: it extracts characters 3-4 rather than the grant characters 4-5 and rejects equality instead of requiring it, so grant-context regressions are not detected by the updated integration tests.

Evidence: `if (items[0].substring(3, 5).equals(expectedGrantShortcut)) return false;`

Suggested direction: Validate the prefix length and require items[0].substring(4, 6).equals(expectedGrantShortcut), returning false when it does not match.

## Audit trail

2 candidate(s) were retained in JSON but excluded from publication.
