# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR4__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR4__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-terra`
Base/head: `62db063e1e1a` → `4f8aed295a29`
Coverage: 32/32 eligible hunks
Duration: 231.6s; model calls: 4

## Findings (4)

### 1. Imported feed HTML bypasses the post sanitizer

`high` · `security` · [app/models/topic_embed.rb:22 (RIGHT)](#)

Trigger: A configured RSS/Atom feed contains executable HTML such as a script tag or an event-handler attribute in an item body.

Impact: The item body is saved with the raw_html cook method, whose new cook path returns raw HTML without the normal rendering/sanitization pipeline, so visitors viewing the imported topic can execute attacker-supplied JavaScript in the forum origin.

Evidence: `creator = PostCreator.new(user, title: title, raw: absolutize_urls(url, contents), skip_validations: true, cook_method: Post.cook_methods[:raw_html])`

Suggested direction: Do not mark externally supplied feed content as raw_html; pass it through the normal cooker or sanitize it with the same strict HTML allowlist before persisting/rendering it.

### 2. The Referer header is interpolated into executable JavaScript

`high` · `security` · [app/views/layouts/embed.html.erb:11 (RIGHT)](#)

Trigger: An embed page is loaded from the configured host with a Referer URL containing a JavaScript string delimiter, such as a quote in an otherwise valid query string or path.

Impact: The unescaped Referer closes the JavaScript string in the embed response and executes attacker-controlled code in the Discourse iframe origin.

Evidence: `parent.postMessage({type: 'discourse-resize', height: document['body'].offsetHeight}, '<%= request.referer %>');`

Suggested direction: Derive the target origin from the parsed Referer and emit it with JSON/JavaScript escaping (for example, `origin.to_json`) rather than interpolating the raw header into a quoted script literal.

### 3. Disqus imports no longer preserve the source thread creation time

`medium` · `data` · [lib/tasks/disqus.thor:148 (RIGHT)](#)

Trigger: An administrator imports a Disqus export containing historical threads.

Impact: The initial post for every imported thread is created at import time instead of `t[:created_at]`, changing topic chronology, ordering, and historical metadata.

Evidence: `post = TopicEmbed.import_remote(user, t[:link], title: t[:title])`

Suggested direction: Pass the thread creation timestamp through `import_remote` and `TopicEmbed.import` to PostCreator's `created_at` option, as the previous importer did.

### 4. The documented Disqus category option was removed

`low` · `api` · [lib/tasks/disqus.thor:117 (LEFT)](#)

Trigger: Existing import automation invokes the Disqus Thor task with `--category` or `-c`.

Impact: Thor rejects the previously supported option, and operators can no longer place imported threads into a selected category.

Evidence: `method_option :category, aliases: '-c', desc: "The category to post in"`

Suggested direction: Retain the category option and propagate the resolved category into the new TopicEmbed/PostCreator import path.

## Audit trail

5 candidate(s) were retained in JSON but excluded from publication.
