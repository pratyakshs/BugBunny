# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR8__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR8__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-4-8`
Base/head: `4975fc28903a` → `060cda77729c`
Coverage: 18/18 eligible hunks
Duration: 314.4s; model calls: 4

## Findings (1)

### 1. totalPages off-by-one produces a spurious empty last page

`high` · `bug` · [app/assets/javascripts/admin/controllers/admin-group.js.es6:13 (RIGHT)](#)

Trigger: A group whose member count is an exact multiple of the page limit (e.g. 50 members with limit 50, or 100 with limit 50).

Impact: totalPages is computed as floor(count/limit)+1, one more than the real number of pages, so currentPage never equals totalPages, showingLast stays false, the 'next' control stays enabled, and clicking it navigates to an empty member page.

Evidence: `return Math.floor(this.get("user_count") / this.get("limit")) + 1;`

Suggested direction: Use Math.ceil(user_count / limit) (returning 0 only when user_count is 0) so an evenly divisible count yields the correct page total.

## Audit trail

1 candidate(s) were retained in JSON but excluded from publication.
