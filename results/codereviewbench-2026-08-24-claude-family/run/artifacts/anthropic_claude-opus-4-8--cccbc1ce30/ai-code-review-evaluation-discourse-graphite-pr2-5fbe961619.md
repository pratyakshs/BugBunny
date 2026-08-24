# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR2__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR2__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-4-8`
Base/head: `56abd247e182` → `6669a2d94d76`
Coverage: 24/24 eligible hunks
Duration: 152.5s; model calls: 4

## Findings (1)

### 1. unsubscribe dereferences possibly-nil TopicUser

`high` · `bug` · [app/controllers/topics_controller.rb:107 (RIGHT)](#)

Trigger: A logged-in user opens the unsubscribe URL for a topic for which no TopicUser row exists (e.g. they never viewed/tracked it, or the row was deleted).

Impact: `TopicUser.find_by` returns nil, so `tu.notification_level` raises NoMethodError and the request 500s instead of unsubscribing.

Evidence: `if tu.notification_level > TopicUser.notification_levels[:regular]`

Suggested direction: Guard for a nil `tu` (create/return early or use TopicUser.change) before reading/writing `notification_level`.

## Audit trail

4 candidate(s) were retained in JSON but excluded from publication.
