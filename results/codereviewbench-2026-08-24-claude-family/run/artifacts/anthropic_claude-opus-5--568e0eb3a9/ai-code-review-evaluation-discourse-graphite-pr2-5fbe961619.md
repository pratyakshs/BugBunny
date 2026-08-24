# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR2__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR2__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-5`
Base/head: `56abd247e182` → `6669a2d94d76`
Coverage: 24/24 eligible hunks
Duration: 281.5s; model calls: 4

## Findings (1)

### 1. unsubscribe action crashes when no TopicUser row exists

`high` · `bug` · [app/controllers/topics_controller.rb:105 (RIGHT)](#)

Trigger: A logged-in user opens /t/<slug>/<id>/unsubscribe for a topic that has no topic_users row for them (never opened the topic, row pruned, or notification delivered without creating the row).

Impact: `find_by` returns nil and `nil.notification_level` raises NoMethodError, producing a 500 error page instead of unsubscribing the user.

Evidence: `tu = TopicUser.find_by(user_id: current_user.id, topic_id: params[:topic_id])

    if tu.notification_level > TopicUser.notification_levels[:regular]`

Suggested direction: Guard the nil case, e.g. `tu = TopicUser.find_by(...)` then `return redirect_to @topic_view.topic.relative_url` / create the record via `TopicUser.change(current_user.id, topic.id, notification_level: ...)` instead of mutating a possibly nil record.

## Audit trail

8 candidate(s) were retained in JSON but excluded from publication.
