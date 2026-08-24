# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR8__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR8__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `4975fc28903a` → `060cda77729c`
Coverage: 18/18 eligible hunks
Duration: 220.1s; model calls: 4

## Findings (3)

### 1. Removing a member passes an integer to the association

`high` · `bug` · [app/controllers/admin/groups_controller.rb:90 (RIGHT)](#)

Trigger: An administrator sends the new DELETE `/admin/groups/:group_id/members` request with a valid numeric `user_id`.

Impact: ActiveRecord's collection association expects a User record here and can raise a type-mismatch exception, so member removal fails with a server error.

Evidence: `group.users.delete(user_id)`

Suggested direction: Load the user record before deleting it, or delete the matching GroupUser directly, for example `group.group_users.where(user_id: user_id).destroy_all`.

### 2. Exact page-size groups expose a nonexistent extra page

`medium` · `bug` · [app/assets/javascripts/admin/controllers/admin-group.js.es6:13 (RIGHT)](#)

Trigger: An administrator opens a group whose member count is an exact multiple of the page size, such as 50 members with a limit of 50, and clicks next.

Impact: The UI reports an extra page, requests an empty offset, and displays an empty member list for that page.

Evidence: `return Math.floor(this.get("user_count") / this.get("limit")) + 1;`

Suggested direction: Calculate the ceiling of `user_count / limit` rather than always adding one after flooring.

### 3. The automatic-group removal test uses the wrong HTTP verb

`low` · `test_gap` · [spec/controllers/admin/groups_controller_spec.rb:115 (RIGHT)](#)

Trigger: The test suite runs the new automatic-group removal example.

Impact: The controller spec invokes the action directly with PUT even though the route is DELETE, so it does not verify that the configured DELETE endpoint reaches the protection logic.

Evidence: `xhr :put, :remove_member, group_id: 1, user_id: 42`

Suggested direction: Use `xhr :delete, :remove_member` and add route/request coverage if necessary to verify the actual endpoint verb.

## Audit trail

5 candidate(s) were retained in JSON but excluded from publication.
