# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR1__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR1__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-terra`
Base/head: `3f71fa15c93b` → `ffbaf8c54269`
Coverage: 4/4 eligible hunks
Duration: 138.2s; model calls: 4

## Findings (2)

### 1. Preserve the five-argument downsize API used by emoji resizing

`medium` · `bug` · [app/models/optimized_image.rb:149 (RIGHT)](#)

Trigger: When the ResizeEmoji job processes an uploaded emoji, it calls OptimizedImage.downsize(path, path, 100, 100, opts).

Impact: Ruby replaces the preceding downsize definition with this later definition rather than overloading it, so the job raises ArgumentError for five arguments and emoji files are not resized.

Evidence: `def self.downsize(from, to, dimensions, opts={})`

Suggested direction: Use a distinct method name for the percentage/dimensions variant, or implement one compatible downsize signature that accepts both width/height and a geometry string while retaining the existing five-argument call contract.

### 2. Keep attachment validation tied to the attachment size setting

`medium` · `bug` · [app/assets/javascripts/discourse/lib/utilities.js:182 (RIGHT)](#)

Trigger: A site sets max_attachment_size_kb below 10 MB (the default is 3 MB) and a user selects an attachment, such as a 5 MB PDF.

Impact: The browser accepts and uploads an attachment that the server-side UploadValidator will reject for exceeding max_attachment_size_kb, wasting the upload and replacing the immediate configured-limit error with a later failure.

Evidence: `var maxSizeKB = 10 * 1024; // 10MB`

Suggested direction: Use the 10 MB transport limit only for image uploads that can be downsized; continue to read Discourse.SiteSettings['max_' + type + '_size_kb'] for attachments.

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
