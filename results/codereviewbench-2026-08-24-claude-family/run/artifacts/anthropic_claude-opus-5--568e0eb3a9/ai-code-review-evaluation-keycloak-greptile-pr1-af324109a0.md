# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak-greptile__augment__PR1__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak-greptile__augment__PR1__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-5`
Base/head: `30f804af450d` → `3214b188de80`
Coverage: 16/16 eligible hunks
Duration: 250.7s; model calls: 4

## Findings (1)

### 1. UsernameForm calls isConditionalPasskeysEnabled() with no arguments, but only a UserModel overload exists

`critical` · `bug` · [services/src/main/java/org/keycloak/authentication/authenticators/browser/UsernameForm.java:47 (RIGHT)](#)

Trigger: Compiling the services module after this patch; the only definition introduced is `protected boolean isConditionalPasskeysEnabled(UserModel user)` in UsernamePasswordForm and no no-argument overload exists anywhere in the tree (repository-wide search returns only the four sites in UsernameForm/UsernamePasswordForm).

Impact: javac fails with "method isConditionalPasskeysEnabled in class UsernamePasswordForm cannot be applied to given types", so the whole build breaks and no runtime behavior of the fix is reachable.

Evidence: `if (context.getUser() != null && !isConditionalPasskeysEnabled()) {`

Suggested direction: Either call `isConditionalPasskeysEnabled(context.getUser())` here, or add the intended no-argument helper (e.g. `protected boolean isConditionalPasskeysEnabled() { return webauthnAuth != null && webauthnAuth.isPasskeysEnabled(); }`) to UsernamePasswordForm and keep both call shapes consistent.

## Audit trail

5 candidate(s) were retained in JSON but excluded from publication.
