# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak-greptile__augment__PR1__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak-greptile__augment__PR1__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-4-8`
Base/head: `30f804af450d` → `3214b188de80`
Coverage: 16/16 eligible hunks
Duration: 367.4s; model calls: 4

## Findings (1)

### 1. UsernameForm calls isConditionalPasskeysEnabled() with no arguments

`critical` · `bug` · [services/src/main/java/org/keycloak/authentication/authenticators/browser/UsernameForm.java:47 (RIGHT)](#)

Trigger: Compiling the services module after this patch is applied.

Impact: Compilation fails: there is no no-argument isConditionalPasskeysEnabled() method; the only definition introduced is isConditionalPasskeysEnabled(UserModel) in the superclass UsernamePasswordForm, so the call cannot be resolved.

Evidence: `if (context.getUser() != null && !isConditionalPasskeysEnabled()) {`

Suggested direction: Pass the user, e.g. `!isConditionalPasskeysEnabled(context.getUser())`, to match the declared signature isConditionalPasskeysEnabled(UserModel).

## Audit trail

1 candidate(s) were retained in JSON but excluded from publication.
