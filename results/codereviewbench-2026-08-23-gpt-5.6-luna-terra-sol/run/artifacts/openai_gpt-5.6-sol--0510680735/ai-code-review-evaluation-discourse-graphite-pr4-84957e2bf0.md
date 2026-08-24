# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR4__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR4__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `62db063e1e1a` → `4f8aed295a29`
Coverage: 32/32 eligible hunks
Duration: 1425.3s; model calls: 5

## Findings (10)

### 1. The message-origin check accepts substring origins

`low` · `security` · [app/assets/javascripts/embed.js:17 (RIGHT)](#)

Trigger: An attacker-controlled origin is a textual prefix or other substring of discourseUrl, such as an origin ending in `.co` when the configured forum hostname continues with `m`.

Impact: The attacker can send accepted `discourse-resize` messages and arbitrarily manipulate the iframe's displayed height on the embedding page.

Evidence: `if (discourseUrl.indexOf(e.origin) === -1) { return; }`

Suggested direction: Parse discourseUrl once and require `e.origin === parsedUrl.origin`; also require `e.source === iframe.contentWindow`.

### 2. Standard RSS items without content:encoded crash feed polling

`medium` · `bug` · [app/jobs/scheduled/poll_feed.rb:35 (RIGHT)](#)

Trigger: The configured RSS feed provides the standard `description` field but no `content`/`content:encoded` field.

Impact: `i.content` is nil, so the hourly poll raises NoMethodError and imports none of the feed's items.

Evidence: `content = CGI.unescapeHTML(i.content.scrub)`

Suggested direction: Select a present value such as `i.content || i.description`, handle a missing body explicitly, and only then normalize the string.

### 3. Imported feed HTML bypasses the sanitizer and becomes stored XSS

`high` · `security` · [app/models/topic_embed.rb:22 (RIGHT)](#)

Trigger: A configured feed contains an item body with a script tag, event-handler attribute, unsafe URL, or other active HTML.

Impact: The HTML is stored as a raw_html post and rendered under the Discourse origin, allowing script execution with forum credentials when users view the imported topic or embed.

Evidence: `creator = PostCreator.new(user, title: title, raw: absolutize_urls(url, contents), skip_validations: true, cook_method: Post.cook_methods[:raw_html])`

Suggested direction: Sanitize imported HTML with a strict tag, attribute, and URL-scheme allowlist before assigning raw_html, and add malicious-feed regression tests.

### 4. The imported-source footer interpolates an unescaped URL into HTML

`high` · `security` · [app/models/topic_embed.rb:13 (RIGHT)](#)

Trigger: A feed item supplies an HTTP URL containing a quote and attribute payload, which passes the prefix check and is imported directly.

Impact: The URL can break out of the single-quoted href and add executable HTML attributes to a raw_html post, producing stored XSS independently of the item body.

Evidence: `contents << "\n<hr>\n<small>#{I18n.t('embed.imported_from', link: "<a href='#{url}'>#{url}</a>")}</small>\n"`

Suggested direction: Parse and validate the URL, then construct the anchor with an escaping helper or Nokogiri rather than string interpolation; escape the displayed URL as text.

### 5. Title-only feed changes are never propagated to the topic

`medium` · `data` · [app/models/topic_embed.rb:34 (RIGHT)](#)

Trigger: A publisher corrects or changes an item's title while leaving its body unchanged, or changes both title and body after initial import.

Impact: The corresponding Discourse topic keeps its old title permanently; the update branch only revises the first post's body.

Evidence: `if content_sha1 != embed.content_sha1`

Suggested direction: Compare and revise the topic title independently of the body SHA, using the existing topic revision/update path.

### 6. Document-relative and protocol-relative links are not absolutized correctly

`medium` · `bug` · [app/models/topic_embed.rb:64 (RIGHT)](#)

Trigger: An imported article contains `href="next.html"`, `src="images/a.png"`, or a protocol-relative URL such as `//cdn.example/a.png`.

Impact: Document-relative references remain relative to the Discourse topic URL, while protocol-relative references are incorrectly placed under the source host, so links and images point to the wrong resources.

Evidence: `if href.present? && href.start_with?('/')`

Suggested direction: Resolve every relative href and src with `URI.join(url, value)` while preserving already absolute safe URLs, and apply the same logic to both anchors and images.

### 7. The best template contains invalid ERB-generated Ruby

`high` · `bug` · [app/views/embed/best.html.erb:6 (RIGHT)](#)

Trigger: The embed controller renders the `best` template for any existing TopicEmbed mapping.

Impact: Template compilation raises a Ruby syntax error instead of rendering the embedded discussion.

Evidence: `<%- end if %>`

Suggested direction: Replace the tag with `<%- end %>` and add a controller/view rendering test that does not suppress template rendering.

### 8. Removing the Disqus category option breaks the existing CLI contract

`medium` · `api` · [lib/tasks/disqus.thor:117 (LEFT)](#)

Trigger: An existing import command or automation invokes `thor disqus:import --category ...` or `-c ...`.

Impact: Thor rejects the formerly supported option or imported topics lose their requested category and are created uncategorized.

Evidence: `method_option :category, aliases: '-c', desc: "The category to post in"`

Suggested direction: Retain the option and pass the resolved category through TopicEmbed.import_remote into PostCreator.

### 9. Disqus import now depends on every source article still being online

`high` · `data` · [lib/tasks/disqus.thor:148 (RIGHT)](#)

Trigger: A Disqus export points to a deleted, moved, authenticated, TLS-incompatible, or temporarily unavailable source page.

Impact: The uncaught remote-fetch error aborts the importer, and comments that are fully present in the export are not migrated.

Evidence: `post = TopicEmbed.import_remote(user, t[:link], title: t[:title])`

Suggested direction: Keep remote article retrieval optional and rescued; create the topic from export data or a permalink fallback when fetching fails, then continue importing comments.

### 10. Disqus topics no longer retain their original creation dates

`medium` · `data` · [lib/tasks/disqus.thor:148 (RIGHT)](#)

Trigger: Any Disqus thread is imported through the changed command.

Impact: The first post and topic use the current import time instead of `t[:created_at]`, corrupting chronological ordering and historical metadata.

Evidence: `post = TopicEmbed.import_remote(user, t[:link], title: t[:title])`

Suggested direction: Add `created_at` support to import_remote/import and pass the parsed export timestamp through to PostCreator.

## Audit trail

14 candidate(s) were retained in JSON but excluded from publication.
