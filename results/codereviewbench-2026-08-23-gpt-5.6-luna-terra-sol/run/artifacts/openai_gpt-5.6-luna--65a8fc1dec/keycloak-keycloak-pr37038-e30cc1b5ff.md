# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak__augment__PR37038__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak__augment__PR37038__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `7ea067aae9be` → `7355f05e6aaf`
Coverage: 30/30 eligible hunks
Duration: 192.6s; model calls: 4

## Findings (1)

### 1. View-only all-groups permission grants group management

`high` · `security` · [services/src/main/java/org/keycloak/services/resources/admin/permissions/GroupPermissionsV2.java:70 (RIGHT)](#)

Trigger: With ADMIN_FINE_GRAINED_AUTHZ_V2 enabled, an administrator has only the `view` scope on the all-groups resource and calls an endpoint that invokes `requireManage()` for groups, such as creating a top-level group.

Impact: The view-only administrator passes `canManage()` and can perform group-management operations despite having no `manage` permission.

Evidence: `return hasPermission(null, AdminPermissionsSchema.VIEW, AdminPermissionsSchema.MANAGE);`

Suggested direction: Check only `AdminPermissionsSchema.MANAGE` in `canManage()`; retain `VIEW` as a valid scope only for `canView()`.

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
