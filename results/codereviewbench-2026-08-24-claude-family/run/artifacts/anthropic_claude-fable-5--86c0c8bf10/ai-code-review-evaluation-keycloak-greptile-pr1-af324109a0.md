# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak-greptile__augment__PR1__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak-greptile__augment__PR1__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-fable-5`
Base/head: `30f804af450d` → `3214b188de80`
Coverage: 16/16 eligible hunks
Duration: 236.0s; model calls: 4

## Findings (1)

### 1. UsernameForm calls non-existent no-arg isConditionalPasskeysEnabled(), breaking compilation

`critical` · `bug` · [services/src/main/java/org/keycloak/authentication/authenticators/browser/UsernameForm.java:47 (RIGHT)](#)

Trigger: Compiling the services module; the only declaration in the class hierarchy is UsernamePasswordForm.isConditionalPasskeysEnabled(UserModel user) (one argument), and a repository-wide search shows no zero-argument overload anywhere.

Impact: javac fails with 'method isConditionalPasskeysEnabled in class UsernamePasswordForm cannot be applied to given types', so the whole services module and any dependent build fail; nothing in this PR can ship.

Evidence: `if (context.getUser() != null && !isConditionalPasskeysEnabled()) {`

Suggested direction: Call the existing overload with the current user, e.g. `!isConditionalPasskeysEnabled(context.getUser())`, or add a matching no-arg helper in UsernamePasswordForm.

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
