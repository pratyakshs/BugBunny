# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR8__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR8__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `4975fc28903a` → `060cda77729c`
Coverage: 18/18 eligible hunks
Duration: 447.8s; model calls: 4

## Findings (2)

### 1. Exact page-size multiples produce a phantom extra page

`medium` · `bug` · [app/assets/javascripts/admin/controllers/admin-group.js.es6:13 (RIGHT)](#)

Trigger: Open an admin group whose member count is an exact positive multiple of the page limit, such as 50 members with a limit of 50.

Impact: The UI reports two pages instead of one, enables Next, and allows navigation to an empty phantom page.

Evidence: `return Math.floor(this.get("user_count") / this.get("limit")) + 1;`

Suggested direction: Compute the page count with `Math.ceil(user_count / limit)` while retaining the explicit zero-member case.

### 2. The automatic-removal test uses an HTTP verb unavailable in production

`low` · `test_gap` · [spec/controllers/admin/groups_controller_spec.rb:115 (RIGHT)](#)

Trigger: The remove-member routing or HTTP method contract regresses while this controller test continues dispatching the action directly with PUT.

Impact: The test can pass even though production exposes `remove_member` only through DELETE, so it does not accurately exercise the endpoint contract it claims to cover.

Evidence: `xhr :put, :remove_member, group_id: 1, user_id: 42`

Suggested direction: Change the request to `xhr :delete, :remove_member, ...` and add a routing/request assertion if route-level coverage is required.

## Audit trail

11 candidate(s) were retained in JSON but excluded from publication.
