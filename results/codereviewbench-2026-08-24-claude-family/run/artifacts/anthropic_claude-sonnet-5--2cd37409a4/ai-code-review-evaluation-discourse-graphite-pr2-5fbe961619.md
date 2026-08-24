# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR2__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR2__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-sonnet-5`
Base/head: `56abd247e182` → `6669a2d94d76`
Coverage: 24/24 eligible hunks
Duration: 284.6s; model calls: 4

## Findings (1)

### 1. TopicUser lookup in unsubscribe action is not nil-checked before use

`high` · `bug` · [app/controllers/topics_controller.rb:105 (RIGHT)](#)

Trigger: A logged-in user who was notified about a topic (e.g. mentioned or invited to a PM) but never visited/tracked it clicks the unsubscribe link in the email, so no TopicUser row exists for that user/topic pair.

Impact: TopicUser.find_by returns nil and the following `tu.notification_level` / `tu.save!` calls raise NoMethodError, causing a 500 error instead of unsubscribing the user.

Evidence: `tu = TopicUser.find_by(user_id: current_user.id, topic_id: params[:topic_id])`

Suggested direction: Guard against a missing record, e.g. `tu ||= TopicUser.new(user_id: current_user.id, topic_id: params[:topic_id])` or return a friendly error/redirect when tu is nil before mutating it.

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
