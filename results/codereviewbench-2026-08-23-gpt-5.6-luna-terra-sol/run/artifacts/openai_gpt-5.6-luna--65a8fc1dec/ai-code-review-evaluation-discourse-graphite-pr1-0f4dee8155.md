# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR1__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR1__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `3f71fa15c93b` → `ffbaf8c54269`
Coverage: 4/4 eligible hunks
Duration: 150.1s; model calls: 4

## Findings (2)

### 1. Client-side validation ignores the configured attachment limit

`medium` · `bug` · [app/assets/javascripts/discourse/lib/utilities.js:182 (RIGHT)](#)

Trigger: A non-image attachment is selected while max_attachment_size_kb is below 10240, such as a 5 MB attachment on a site configured for a 3 MB limit.

Impact: The browser accepts and submits the attachment instead of rejecting it with the configured limit, so the upload fails later on the server; attachments on sites allowing more than 10 MB are also rejected prematurely.

Evidence: `var maxSizeKB = 10 * 1024; // 10MB`

Suggested direction: Select the limit by upload type, using max_attachment_size_kb for attachments while applying the intended source-image limit only to images.

### 2. The new downsize overload breaks emoji resizing jobs

`medium` · `bug` · [app/models/optimized_image.rb:149 (RIGHT)](#)

Trigger: The existing ResizeEmoji job calls OptimizedImage.downsize(path, path, 100, 100, opts) with five arguments.

Impact: Ruby resolves the later definition and raises ArgumentError for the five-argument call, so emoji resizing jobs abort and oversized emoji are not reduced.

Evidence: `def self.downsize(from, to, dimensions, opts={})`

Suggested direction: Keep one downsize method that supports the legacy width/height call shape and the new geometry-string shape, or update every caller before removing the old signature.

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
