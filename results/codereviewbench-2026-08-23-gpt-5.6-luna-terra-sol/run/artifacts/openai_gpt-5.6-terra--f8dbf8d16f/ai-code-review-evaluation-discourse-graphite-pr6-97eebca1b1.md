# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR6__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR6__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-terra`
Base/head: `7a5fbae060bd` → `267d8be1f556`
Coverage: 6/6 eligible hunks
Duration: 91.8s; model calls: 4

## Findings (2)

### 1. Do not expose a derived website field when website is restricted

`medium` · `security` · [app/serializers/user_serializer.rb:43 (RIGHT)](#)

Trigger: An anonymous visitor views a TL0 user's profile while `scope.restrict_user_fields?(object)` is true and that user has a website configured.

Impact: The existing untrusted-attribute policy suppresses `website`, but the new `website_name` is still serialized and reveals the website host (and, for matching domains, its path).

Evidence: `:website_name,`

Suggested direction: Apply the same restriction as `website` to `website_name`, for example add it to `untrusted_attributes` or implement `include_website_name?` that returns false when `scope.restrict_user_fields?(object)` is true; add an anonymous-TL0 serialization test with a populated website.

### 2. Use the serializer's conditional predicate naming convention

`low` · `api` · [app/serializers/user_serializer.rb:153 (RIGHT)](#)

Trigger: Any user profile without a website is serialized.

Impact: ActiveModel serializer conditional hooks use `include_<attribute>?`; this method is not invoked, so `website_name` is emitted as a null attribute instead of being omitted as the attempted predicate indicates.

Evidence: `def include_website_name`

Suggested direction: Rename the method to `include_website_name?` and retain the presence check (while also applying the website visibility restriction).

## Audit trail

1 candidate(s) were retained in JSON but excluded from publication.
