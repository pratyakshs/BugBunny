# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR2__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR2__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-fable-5`
Base/head: `56abd247e182` → `6669a2d94d76`
Coverage: 24/24 eligible hunks
Duration: 266.5s; model calls: 4

## Findings (1)

### 1. NoMethodError when no TopicUser record exists in new unsubscribe action

`high` · `bug` · [app/controllers/topics_controller.rb:107 (RIGHT)](#)

Trigger: A logged-in user visits /t/:slug/:topic_id/unsubscribe for a topic they have never visited, tracked, or been emailed about, so TopicUser.find_by returns nil.

Impact: tu is nil and calling nil.notification_level raises NoMethodError, producing a 500 error instead of unsubscribing or showing the topic.

Evidence: `if tu.notification_level > TopicUser.notification_levels[:regular]`

Suggested direction: Handle the nil case, e.g. use TopicUser.find_or_initialize_by(user_id: current_user.id, topic_id: params[:topic_id]) or guard with `if tu.nil?` and create a muted record via TopicUser.change.

## Audit trail

5 candidate(s) were retained in JSON but excluded from publication.
