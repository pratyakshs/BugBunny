# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR4__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR4__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `62db063e1e1a` → `4f8aed295a29`
Coverage: 32/32 eligible hunks
Duration: 250.5s; model calls: 4

## Findings (7)

### 1. Handle RSS items that provide description instead of content

`medium` · `bug` · [app/jobs/scheduled/poll_feed.rb:35 (RIGHT)](#)

Trigger: The configured feed is a standard RSS 2.0 feed whose items contain description but no content field.

Impact: i.content is nil, so the hourly poll raises NoMethodError and imports no items from that feed.

Evidence: `content = CGI.unescapeHTML(i.content.scrub)`

Suggested direction: Use the feed parser's content when present and fall back to description, with an empty-string fallback before unescaping or scrubbing.

### 2. Do not render imported feed HTML as trusted cooked HTML

`high` · `security` · [app/models/topic_embed.rb:22 (RIGHT)](#)

Trigger: An RSS item contains a script, event-handler attribute, or dangerous URL in its HTML content; the content is decoded and passed to TopicEmbed.import.

Impact: The untrusted feed HTML is stored as post.cooked without the normal sanitization pipeline and can execute JavaScript for visitors to the forum or embed.

Evidence: `creator = PostCreator.new(user, title: title, raw: absolutize_urls(url, contents), skip_validations: true, cook_method: Post.cook_methods[:raw_html])`

Suggested direction: Sanitize imported HTML with the existing sanitizer and enforce safe URL protocols/attributes before storing it, or use the regular cooking method instead of raw_html.

### 3. Escape the source URL before appending it to imported HTML

`high` · `security` · [app/models/topic_embed.rb:13 (RIGHT)](#)

Trigger: A feed item link or embed_url starts with http:// or https:// but contains a quote and HTML payload, such as https://source.example/?' onmouseover='...'.

Impact: The generated attribution markup breaks out of the href or text context and stores an XSS payload in the raw HTML post.

Evidence: `contents << "\n<hr>\n<small>#{I18n.t('embed.imported_from', link: "<a href='#{url}'>#{url}</a>")}</small>\n"`

Suggested direction: Escape the URL for both attribute and text contexts, or construct the attribution element through a sanitizer-safe HTML builder before storing it.

### 4. Resolve all relative imported URLs against the source document

`low` · `bug` · [app/models/topic_embed.rb:64 (RIGHT)](#)

Trigger: Imported content contains a path-relative link such as href="article/2" or href="../article/2" rather than a root-relative link.

Impact: The link remains relative and is resolved against the Discourse embed endpoint, so it points to the wrong location instead of the source site.

Evidence: `if href.present? && href.start_with?('/')`

Suggested direction: Use URI.join with the source URL for every relative href, including path-relative and protocol-relative forms, while preserving absolute URLs.

### 5. Do not send an invalid X-Frame-Options value

`medium` · `api` · [app/controllers/embed_controller.rb:28 (RIGHT)](#)

Trigger: The embed response is loaded in a browser that enforces the X-Frame-Options grammar rather than ignoring unknown directives.

Impact: The invalid header can be treated as a frame denial, causing the newly added iframe embed to fail in affected browsers.

Evidence: `response.headers['X-Frame-Options'] = "ALLOWALL"`

Suggested direction: Remove X-Frame-Options for this action or use a standards-supported framing policy appropriate for the supported browser versions.

### 6. Preserve the Disqus import category option

`medium` · `api` · [lib/tasks/disqus.thor:117 (LEFT)](#)

Trigger: An existing operator runs the Disqus import task with the documented --category or -c option.

Impact: Thor rejects the now-unknown option and the import cannot run; imports without it also lose the prior ability to assign imported topics to a category.

Evidence: `method_option :category, aliases: '-c', desc: "The category to post in"`

Suggested direction: Retain the CLI option and pass the resolved category through TopicEmbed.import_remote into the created topic.

### 7. Compare postMessage origins exactly

`low` · `security` · [app/assets/javascripts/embed.js:17 (RIGHT)](#)

Trigger: The configured discourseUrl is https://forum.example.com and a window from the distinct origin https://forum.example.co sends a discourse-resize message to the embedding page.

Impact: The unrelated origin passes the substring check and can control the iframe height, allowing layout spoofing or denial of the embedded content's visible area.

Evidence: `if (discourseUrl.indexOf(e.origin) === -1) { return; }`

Suggested direction: Parse discourseUrl and compare e.origin to its exact serialized origin using strict equality.

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
