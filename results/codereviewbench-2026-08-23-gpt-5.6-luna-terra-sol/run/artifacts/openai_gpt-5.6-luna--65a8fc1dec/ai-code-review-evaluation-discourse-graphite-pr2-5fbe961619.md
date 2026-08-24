# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR2__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR2__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `56abd247e182` → `6669a2d94d76`
Coverage: 24/24 eligible hunks
Duration: 269.8s; model calls: 4

## Findings (3)

### 1. Global unsubscribe translation requires an argument unavailable to other mailers

`high` · `bug` · [config/locales/server.en.yml:1855 (RIGHT)](#)

Trigger: A mailer such as the rejection mailer opts into `add_unsubscribe_link` without supplying a topic-specific `unsubscribe_url`.

Impact: MessageBuilder raises a missing interpolation argument while generating the email, so that email is not delivered.

Evidence: `To stop receiving notifications about this particular topic, [click here](%{unsubscribe_url}).`

Suggested direction: Keep the generic `unsubscribe_link` translation free of topic-only placeholders and add the topic link through a notification-specific translation or only when `unsubscribe_url` is present.

### 2. Unsubscribe crashes when the user has no TopicUser record

`medium` · `bug` · [app/controllers/topics_controller.rb:105 (RIGHT)](#)

Trigger: An authenticated user follows the link for a notification received from a topic where no `topic_users` row was previously created, such as a mention without a prior topic visit.

Impact: The next access to `tu.notification_level` raises `NoMethodError`, and the unsubscribe page returns an error instead of changing the preference.

Evidence: `tu = TopicUser.find_by(user_id: current_user.id, topic_id: params[:topic_id])`

Suggested direction: Use `TopicUser.change` with the desired notification level, or create a TopicUser row when the lookup returns nil before reading its level.

### 3. The unsubscribe controller property is misspelled

`low` · `style` · [app/assets/javascripts/discourse/controllers/topic-unsubscribe.js.es6:5 (RIGHT)](#)

Trigger: A future template or controller lookup uses the correctly spelled `stopNotificationsText`, or a maintainer searches for the notification text property.

Impact: The current template must preserve the typo and correctly named consumers fail to find the property, reducing discoverability and making refactoring error-prone.

Evidence: `stopNotificiationsText: function() {`

Suggested direction: Rename the property to `stopNotificationsText` and update the unsubscribe template reference.

## Audit trail

6 candidate(s) were retained in JSON but excluded from publication.
