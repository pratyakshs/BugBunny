# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR2__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR2__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `56abd247e182` → `6669a2d94d76`
Coverage: 24/24 eligible hunks
Duration: 363.8s; model calls: 4

## Findings (3)

### 1. State-changing unsubscribe action is exposed through GET

`high` · `security` · [config/routes.rb:440 (RIGHT)](#)

Trigger: An authenticated user follows a forged top-level link, loads an attacker-controlled page that issues this request in a browser that sends cookies, or an authenticated mail/browser client prefetches the email link.

Impact: Merely requesting the URL changes the user's topic notification preference without confirmation or CSRF protection; HEAD requests and link scanners can also invoke Rails GET routes and cause the same mutation.

Evidence: `get "t/:slug/:topic_id/unsubscribe" => "topics#unsubscribe", constraints: {topic_id: /\d+/}`

Suggested direction: Make GET render a confirmation page without changing state, and perform the preference update through a CSRF-protected POST/PUT endpoint. Change both unsubscribe route variants consistently.

### 2. Unsubscribe crashes when no TopicUser row or level exists

`medium` · `bug` · [app/controllers/topics_controller.rb:107 (RIGHT)](#)

Trigger: A logged-in user opens an unsubscribe URL for a visible topic for which they have no TopicUser record, or a legacy TopicUser row has a NULL notification_level.

Impact: Dereferencing `tu` or comparing a nil notification level raises an exception, returning a 500 response instead of applying the requested preference.

Evidence: `if tu.notification_level > TopicUser.notification_levels[:regular]`

Suggested direction: Use `find_or_initialize_by` with an explicit regular default before comparing, or avoid the comparison and call `TopicUser.change` directly with the final muted level so a missing row is created safely.

### 3. Computed property name misspells notifications

`low` · `style` · [app/assets/javascripts/discourse/controllers/topic-unsubscribe.js.es6:5 (RIGHT)](#)

Trigger: A maintainer, plugin, or test refers to the new controller property using the conventional spelling `stopNotificationsText`.

Impact: The misspelled public property creates a brittle, non-discoverable interface and forces the template to repeat the same typo, increasing the likelihood of future lookup failures during refactoring.

Evidence: `stopNotificiationsText: function() {`

Suggested direction: Rename the property to `stopNotificationsText` and update the matching template reference.

## Audit trail

6 candidate(s) were retained in JSON but excluded from publication.
