# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR1__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR1__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `3f71fa15c93b` → `ffbaf8c54269`
Coverage: 4/4 eligible hunks
Duration: 388.0s; model calls: 4

## Findings (3)

### 1. The fixed client limit rejects attachments permitted by site configuration

`medium` · `bug` · [app/assets/javascripts/discourse/lib/utilities.js:182 (RIGHT)](#)

Trigger: An administrator configures max_attachment_size_kb above 10240 and a user selects an attachment between 10 MB and that configured limit.

Impact: The browser rejects an attachment that the server-side upload validator is configured to accept.

Evidence: `var maxSizeKB = 10 * 1024; // 10MB`

Suggested direction: Use Discourse.SiteSettings.max_attachment_size_kb when type is "attachment"; apply any larger source-image allowance only to image uploads.

### 2. Failed conversions are retried five times without checking the result

`medium` · `performance` · [app/controllers/uploads_controller.rb:67 (RIGHT)](#)

Trigger: A logged-in user uploads an oversized corrupt image, unsupported image, or animated GIF for which OptimizedImage.downsize returns false without reducing tempfile.size.

Impact: The controller launches the same expensive external conversion command five times before rejecting the upload, enabling avoidable CPU amplification and increasing upload latency.

Evidence: `OptimizedImage.downsize(tempfile.path, tempfile.path, "80%", allow_animation: SiteSetting.allow_animated_thumbnails)`

Suggested direction: Capture the return value and stop retrying when conversion fails or when the file size does not decrease; return a conversion-specific upload error.

### 3. The second downsize definition replaces the existing five-argument API

`medium` · `api` · [app/models/optimized_image.rb:149 (RIGHT)](#)

Trigger: Jobs::ResizeEmoji or any existing caller invokes OptimizedImage.downsize(path, path, width, height, opts).

Impact: Ruby retains only the later method definition, so the existing five-argument call raises ArgumentError and emoji resizing jobs fail.

Evidence: `def self.downsize(from, to, dimensions, opts={})`

Suggested direction: Keep one backward-compatible method that accepts width and height, and add a distinctly named percentage/dimension-string method or normalize both supported argument forms inside a single variadic implementation.

## Audit trail

7 candidate(s) were retained in JSON but excluded from publication.
