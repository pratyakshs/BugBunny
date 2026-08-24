# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR10__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR10__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `913c3d6f636d` → `d1c69189f3c9`
Coverage: 39/39 eligible hunks
Duration: 230.9s; model calls: 4

## Findings (4)

### 1. Legacy host entries are migrated without normalization

`high` · `bug` · [db/migrate/20150818190757_create_embeddable_hosts.rb:25 (RIGHT)](#)

Trigger: Upgrade a site whose old embeddable_hosts setting contains entries such as http://eviltrout.com or https://example.com/1234, which the previous implementation accepted.

Impact: The database stores the scheme and path verbatim, while record_for_host compares only the parsed hostname, so migrated hosts no longer authorize embed requests.

Evidence: `execute "INSERT INTO embeddable_hosts (host, category_id, created_at, updated_at) VALUES ('#{h}', #{category_id}, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"`

Suggested direction: Normalize each legacy entry to its hostname (and supported port) before insertion, or insert through EmbeddableHost so its normalization callback runs.

### 2. Missing host input causes a 500 instead of a validation error

`medium` · `bug` · [app/models/embeddable_host.rb:6 (RIGHT)](#)

Trigger: Send an admin create or update request with embeddable_host[host] omitted or set to null.

Impact: before_validation calls sub! on nil and raises NoMethodError, returning a server error instead of the expected validation response.

Evidence: `self.host.sub!(/^https?:\/\//, '')`

Suggested direction: Guard normalization with a presence check and let the existing format validation reject a missing host.

### 3. Updating a nonexistent embeddable host returns a 500

`low` · `api` · [app/controllers/admin/embeddable_hosts_controller.rb:10 (RIGHT)](#)

Trigger: Send PUT /admin/embeddable_hosts/:id with an ID that does not exist.

Impact: host is nil and save_host dereferences it while assigning host.host, producing an internal server error instead of a not-found response.

Evidence: `host = EmbeddableHost.where(id: params[:id]).first`

Suggested direction: Use find or explicitly return a 404 when the record is absent before calling save_host.

### 4. Destroying a nonexistent embeddable host returns a 500

`low` · `api` · [app/controllers/admin/embeddable_hosts_controller.rb:15 (RIGHT)](#)

Trigger: Send DELETE /admin/embeddable_hosts/:id with an ID that does not exist.

Impact: host.destroy is called on nil and raises NoMethodError instead of returning a not-found response.

Evidence: `host = EmbeddableHost.where(id: params[:id]).first`

Suggested direction: Use find or explicitly handle a missing record before destroying it.

## Audit trail

7 candidate(s) were retained in JSON but excluded from publication.
