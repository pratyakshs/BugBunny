# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR10__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR10__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `913c3d6f636d` → `d1c69189f3c9`
Coverage: 39/39 eligible hunks
Duration: 454.4s; model calls: 5

## Findings (7)

### 1. Migration crashes when `embed_category` has no persisted override

`high` · `bug` · [db/migrate/20150818190757_create_embeddable_hosts.rb:11 (RIGHT)](#)

Trigger: Upgrade an installation that uses the default blank `embed_category`, so no matching `site_settings` row/category join result exists.

Impact: The query returns zero rows, `[0]` is nil, and the migration raises before reaching its uncategorized-category fallback, blocking deployment.

Evidence: `WHERE s.name = 'embed_category'")[0]['id'].to_i`

Suggested direction: Store the query result first, safely read its first row with `try`/presence checks, and use `SiteSetting.uncategorized_category_id` when no category row is returned.

### 2. Missing host input raises in the normalization callback

`medium` · `bug` · [app/models/embeddable_host.rb:6 (RIGHT)](#)

Trigger: A create or update request omits `embeddable_host[host]` or supplies null.

Impact: `sub!` is invoked on nil before format validation can produce a normal validation error, resulting in a 500 response.

Evidence: `self.host.sub!(/^https?:\/\//, '')`

Suggested direction: Add a presence validation and guard normalization with `if host.present?`, or normalize via a nil-safe assignment.

### 3. Hostname lookup is accidentally case-sensitive

`medium` · `bug` · [app/models/embeddable_host.rb:17 (RIGHT)](#)

Trigger: A referer or imported URL uses uppercase characters in its hostname, or an allowed host was saved with uppercase characters.

Impact: The lowercased database value is compared with an unlowercased parameter, so a DNS-equivalent allowed hostname is rejected or cannot be selected for category routing.

Evidence: `where("lower(host) = ?", host).first`

Suggested direction: Compare against `host.downcase` and normalize stored hosts to lowercase before validation.

### 4. Valid long top-level domains are rejected

`low` · `api` · [app/models/embeddable_host.rb:2 (RIGHT)](#)

Trigger: An administrator adds or edits a valid hostname whose top-level domain has more than five characters, such as `example.museum` or `example.technology`.

Impact: The relationship cannot be saved, and migrated records using such domains become impossible to edit successfully.

Evidence: `validates_format_of :host, :with => /\A[a-z0-9]+([\-\.]{1}[a-z0-9]+)*\.[a-z]{2,5}(:[0-9]{1,5})?(\/.*)?\Z/i`

Suggested direction: Replace the fixed 2–5 character TLD restriction with URI/hostname validation that supports current DNS labels.

### 5. Updating a nonexistent host returns a server error

`low` · `api` · [app/controllers/admin/embeddable_hosts_controller.rb:10 (RIGHT)](#)

Trigger: A staff client sends PUT/PATCH for an ID that was deleted or never existed.

Impact: `save_host(nil)` attempts to assign `host.host`, producing a 500 response instead of a not-found response.

Evidence: `host = EmbeddableHost.where(id: params[:id]).first`

Suggested direction: Use `EmbeddableHost.find(params[:id])` so normal not-found handling applies, or explicitly render a 404 when `.first` returns nil.

### 6. Deleting a nonexistent host returns a server error

`low` · `api` · [app/controllers/admin/embeddable_hosts_controller.rb:15 (RIGHT)](#)

Trigger: A staff client sends DELETE for an ID that was already deleted or does not exist.

Impact: The next line invokes `destroy` on nil, resulting in a 500 response rather than a 404.

Evidence: `host = EmbeddableHost.where(id: params[:id]).first`

Suggested direction: Use `EmbeddableHost.find(params[:id])` or explicitly return not found before calling `destroy`.

### 7. The category and embeddable-host fabricators were placed in opposite files

`low` · `style` · [spec/fabricators/category_fabricator.rb:1 (RIGHT)](#)

Trigger: A developer or test tool locates or selectively loads fabricators according to the repository's `<model>_fabricator.rb` naming convention.

Impact: `category_fabricator.rb` defines only `embeddable_host`, while `embeddable_host_fabricator.rb` defines categories, making targeted loading and maintenance misleading and fragile.

Evidence: `Fabricator(:embeddable_host) do`

Suggested direction: Move the embeddable-host definition into `embeddable_host_fabricator.rb` and restore the category definitions to `category_fabricator.rb`.

## Audit trail

16 candidate(s) were retained in JSON but excluded from publication.
