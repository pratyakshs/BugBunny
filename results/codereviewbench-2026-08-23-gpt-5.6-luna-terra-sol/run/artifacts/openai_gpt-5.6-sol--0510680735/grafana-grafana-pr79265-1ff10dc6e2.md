# BugBunny review

PR: [code-review-benchmark/grafana__grafana__augment__PR79265__20260122#1](https://github.com/code-review-benchmark/grafana__grafana__augment__PR79265__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `50f4e78a3991` → `bbd8c507cdf5`
Coverage: 19/19 eligible hunks
Duration: 359.0s; model calls: 4

## Findings (3)

### 1. The count-then-insert quota check is not atomic

`medium` · `concurrency` · [pkg/services/anonymous/anonimpl/anonstore/database.go:115 (RIGHT)](#)

Trigger: Two distinct anonymous devices are admitted concurrently when the active count is one below the configured limit, including requests handled by different Grafana instances sharing the database.

Impact: Both requests can observe the same below-limit count and both insert successfully, causing the active device count to exceed the configured limit.

Evidence: `if count >= s.deviceLimit {`

Suggested direction: Perform quota reservation and insertion in one serialized database transaction using an appropriate lock or atomic quota row, and retry or reject a request that loses the race.

### 2. A rejected device is cached and admitted on its next request

`high` · `bug` · [pkg/services/anonymous/anonimpl/impl.go:147 (RIGHT)](#)

Trigger: A new device requests anonymous access after the limit is reached, then retries with the same device ID within the local cache's 29-minute TTL.

Impact: The first request returns ErrDeviceLimitReached, but tagDeviceUI already cached the device before the failed store operation. The retry hits the cache, returns nil without checking the database, and authenticates successfully despite the limit.

Evidence: `return err`

Suggested direction: Populate the local cache only after CreateOrUpdateDevice succeeds, or explicitly remove the cache entry whenever the store operation returns an error.

### 3. Omitting the optional device header bypasses the limit

`high` · `security` · [pkg/services/anonymous/anonimpl/client.go:44 (RIGHT)](#)

Trigger: An anonymous request omits X-Grafana-Device-Id, either deliberately or because FingerprintJS has not completed before the frontend sends its first requests.

Impact: TagDevice returns nil immediately for a missing header, so Authenticate returns an anonymous identity even when the configured device capacity is full.

Evidence: `if err := a.anonDeviceService.TagDevice(ctx, httpReqCopy, anonymous.AnonDeviceUI); err != nil {`

Suggested direction: When a positive limit is enabled, require a valid server-managed device credential before anonymous authentication. Provide a controlled bootstrap flow for initial navigation rather than treating a missing client header as successful admission.

## Audit trail

5 candidate(s) were retained in JSON but excluded from publication.
