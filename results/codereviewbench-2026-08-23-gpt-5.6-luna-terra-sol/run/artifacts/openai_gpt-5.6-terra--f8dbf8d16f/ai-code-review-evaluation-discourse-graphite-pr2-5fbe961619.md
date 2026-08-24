# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR2__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR2__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-terra`
Base/head: `56abd247e182` → `6669a2d94d76`
Coverage: 24/24 eligible hunks
Duration: 207.2s; model calls: 4

## Findings (2)

### 1. Escape the topic title before rendering the translated message

`high` · `security` · [app/assets/javascripts/discourse/templates/topic/unsubscribe.hbs:3 (RIGHT)](#)

Trigger: A topic title contains HTML such as an element with an event handler, and a logged-in recipient opens that topic's unsubscribe URL.

Impact: The computed translation includes the user-controlled `model.fancyTitle` and is inserted with triple-stash rendering, allowing markup in the title to become executable HTML on the unsubscribe page.

Evidence: `{{{stopNotificiationsText}}}`

Suggested direction: Render the title through a normally escaped template expression and keep only trusted translation markup outside it, or explicitly HTML-escape the title before passing it to the translation.

### 2. Handle recipients that do not yet have a TopicUser row

`high` · `bug` · [app/controllers/topics_controller.rb:105 (RIGHT)](#)

Trigger: A recipient notified through a category-level setting or a mention opens the link before ever creating a `topic_users` row for that topic.

Impact: `find_by` returns nil and the following `tu.notification_level` dereference raises, returning a 500 instead of unsubscribing the recipient.

Evidence: `tu = TopicUser.find_by(user_id: current_user.id, topic_id: params[:topic_id])`

Suggested direction: Use `TopicUser.change` or create/find a TopicUser record before reading or updating its notification level; this also preserves the normal notification-change bookkeeping.

## Audit trail

5 candidate(s) were retained in JSON but excluded from publication.
