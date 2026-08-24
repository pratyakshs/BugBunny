# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak-greptile__augment__PR1__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak-greptile__augment__PR1__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-terra`
Base/head: `30f804af450d` → `3214b188de80`
Coverage: 16/16 eligible hunks
Duration: 114.3s; model calls: 4

## Findings (1)

### 1. Pass the user argument to the conditional-passkey predicate

`high` · `bug` · [services/src/main/java/org/keycloak/authentication/authenticators/browser/UsernameForm.java:47 (RIGHT)](#)

Trigger: Compiling the services module after this change.

Impact: The build fails because UsernamePasswordForm only defines isConditionalPasskeysEnabled(UserModel), so the zero-argument invocation cannot be resolved.

Evidence: `if (context.getUser() != null && !isConditionalPasskeysEnabled()) {`

Suggested direction: Call isConditionalPasskeysEnabled(context.getUser()), or add a deliberately named zero-argument overload if that is the intended predicate.

## Audit trail

2 candidate(s) were retained in JSON but excluded from publication.
