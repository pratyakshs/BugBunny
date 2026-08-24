# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR1__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR1__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-fable-5`
Base/head: `3f71fa15c93b` → `ffbaf8c54269`
Coverage: 4/4 eligible hunks
Duration: 178.6s; model calls: 4

## Findings (2)

### 1. Duplicate `self.downsize` definition shadows the 4-arg variant, breaking existing 5-argument callers

`critical` · `bug` · [app/models/optimized_image.rb:149 (RIGHT)](#)

Trigger: The ResizeEmoji job runs and calls `OptimizedImage.downsize(path, path, 100, 100, opts)` (app/jobs/regular/resize_emoji.rb:14), which passes 5 arguments.

Impact: In Ruby the later method definition replaces the earlier one, so the 4-arg `downsize(from, to, max_width, max_height, opts)` defined at R145-147 is dead code. Any 5-argument caller such as the emoji resize job raises `ArgumentError: wrong number of arguments (given 5, expected 3..4)`, so custom emoji are never downsized and the job crashes.

Evidence: `def self.downsize(from, to, dimensions, opts={})
    optimize("downsize", from, to, dimensions, opts)
  end`

Suggested direction: Remove one of the two definitions; e.g. keep a single `downsize(from, to, dimensions, opts={})` and update `resize_emoji.rb` to pass "100x100", or give the new dimension-string variant a distinct name (e.g. `downsize_to`).

### 2. Client-side upload size check hardcodes 10MB, ignoring max_image_size_kb / max_attachment_size_kb site settings

`high` · `bug` · [app/assets/javascripts/discourse/lib/utilities.js:182 (RIGHT)](#)

Trigger: An admin configures `max_attachment_size_kb` (default 3072 kB) or `max_image_size_kb` to a value other than 10240 kB, and a user uploads a file between the configured limit and 10MB (or above 10MB when the admin allowed larger uploads).

Impact: The client validation no longer matches server enforcement: attachments up to 10MB pass client validation, are fully uploaded, then rejected by `Validators::UploadValidator` (which still uses `SiteSetting.max_#{type}_size_kb`), wasting bandwidth and showing a late/confusing error; conversely files above 10MB are rejected client-side even when the admin configured a larger limit. Existing JS tests that set `Discourse.SiteSettings.max_image_size_kb` (test/javascripts/lib/utilities-test.js.es6:55,79) now exercise dead configuration and will fail.

Evidence: `var maxSizeKB = 10 * 1024; // 10MB`

Suggested direction: Keep reading the per-type setting (`Discourse.SiteSettings['max_' + type + '_size_kb']`); if images should be allowed to exceed it for auto-downsizing, only relax the check for `type === 'image'` rather than hardcoding 10MB for all types.

## Audit trail

5 candidate(s) were retained in JSON but excluded from publication.
