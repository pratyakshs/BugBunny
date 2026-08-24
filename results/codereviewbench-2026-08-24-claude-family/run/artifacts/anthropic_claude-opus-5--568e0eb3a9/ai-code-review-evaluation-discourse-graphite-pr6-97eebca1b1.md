# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR6__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR6__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-5`
Base/head: `7a5fbae060bd` → `267d8be1f556`
Coverage: 6/6 eligible hunks
Duration: 178.9s; model calls: 4

## Findings (2)

### 1. website_name bypasses untrusted_attributes and leaks TL0 users' website to anonymous

`high` · `security` · [app/serializers/user_serializer.rb:43 (RIGHT)](#)

Trigger: An anonymous visitor requests /u/<username>.json for a trust-level-0 user who has filled in their profile website (the exact case exercised by the existing spec at spec/serializers/user_serializer_spec.rb:11-15).

Impact: `website` is suppressed by `untrusted_attributes` for TL0 users seen anonymously, but the new `website_name` attribute is still serialized and rendered by `{{#if model.website_name}}` in user.hbs, so the spam-prone host and now also the full URL path of a TL0 user's site is exposed to anonymous visitors, defeating the existing restriction.

Evidence: `:website_name,`

Suggested direction: Add `:website_name` to the `untrusted_attributes` list (next to `:website`) so `include_website_name?` also returns false when `scope.restrict_user_fields?(object)` is true, and extend the `untrusted_attributes` spec array to include `website_name`.

### 2. include_website_name lacks the `?` suffix so the attribute is always serialized

`medium` · `api` · [app/serializers/user_serializer.rb:153 (RIGHT)](#)

Trigger: Serializing any user whose profile website is blank (e.g. GET /u/<username>.json for a user that never set a website).

Impact: ActiveModel::Serializers only consults `include_<attr>?`, so this method is never called; `website_name` is unconditionally emitted (as `null`) instead of being omitted, contradicting the intended conditional inclusion and the surrounding convention (`include_card_image_badge_id?`, `include_email?`).

Evidence: `def include_website_name`

Suggested direction: Rename the method to `include_website_name?` so the conditional-inclusion hook is actually invoked.

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
