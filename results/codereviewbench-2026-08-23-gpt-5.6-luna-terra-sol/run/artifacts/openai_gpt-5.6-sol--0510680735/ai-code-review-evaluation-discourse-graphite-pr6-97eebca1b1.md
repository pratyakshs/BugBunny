# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR6__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR6__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `7a5fbae060bd` → `267d8be1f556`
Coverage: 6/6 eligible hunks
Duration: 284.3s; model calls: 4

## Findings (2)

### 1. Derived website field bypasses restricted-profile filtering

`medium` · `security` · [app/serializers/user_serializer.rb:43 (RIGHT)](#)

Trigger: An anonymous visitor requests a trust-level-0 user's profile after that user has populated the website field, causing `scope.restrict_user_fields?(object)` to suppress the original `website` attribute.

Impact: The response and profile template still expose the website host and, for matching domains, its path through `website_name`, bypassing the deliberate restriction on untrusted profile fields.

Evidence: `:website_name,`

Suggested direction: Apply the same restriction to `website_name`, either by adding it to `untrusted_attributes` and removing the custom inclusion method, or by implementing `include_website_name?` with the `scope.restrict_user_fields?(object)` guard. Add a serializer test with a populated TL0 website.

### 2. Serializer inclusion callback is missing its predicate suffix

`low` · `api` · [app/serializers/user_serializer.rb:153 (RIGHT)](#)

Trigger: Any user without a website is serialized through `UserSerializer`.

Impact: ActiveModel Serializers does not recognize this as the `include_website_name?` callback, so the newly added API key is emitted unconditionally, typically as `website_name: null`, instead of being omitted as the method intends.

Evidence: `def include_website_name`

Suggested direction: Rename the callback to `include_website_name?` and make it both presence- and visibility-aware, or remove it and let `untrusted_attributes :website_name` generate the correctly named callback.

## Audit trail

4 candidate(s) were retained in JSON but excluded from publication.
