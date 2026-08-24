# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR8087__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR8087__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-fable-5`
Base/head: `ba9688a04a83` → `820d7fa87e0c`
Coverage: 21/21 eligible hunks
Duration: 219.1s; model calls: 4

## Findings (2)

### 1. Async forEach callback makes surrounding try/catch dead code and turns errors into unhandled rejections

`high` · `bug` · [packages/app-store/vital/lib/reschedule.ts:125 (RIGHT)](#)

Trigger: A Vital reschedule runs for a booking whose calendar/video references exist and `getCalendar` (now async, backed by dynamic `import()`) rejects, or `deleteEvent`/`deleteMeeting` throws inside the callback.

Impact: Previously a synchronous `getCalendar` throw was caught by the enclosing try/catch and logged via `logger.error`. Now the callback returns a promise that `Array.prototype.forEach` ignores, so any rejection escapes the try/catch entirely, producing an unhandled promise rejection (process crash under Node's default `--unhandled-rejections=throw`), and the cancellation work is fire-and-forget so the function proceeds before deletions complete.

Evidence: `bookingRefsFiltered.forEach(async (bookingRef) => {`

Suggested direction: Replace `forEach(async ...)` with `await Promise.all(bookingRefsFiltered.map(async (bookingRef) => {...}))` (or a for...of loop with await) inside the existing try block so errors are caught and deletions complete before continuing.

### 2. Async forEach callback makes surrounding try/catch dead code and turns errors into unhandled rejections

`high` · `bug` · [packages/app-store/wipemycalother/lib/reschedule.ts:125 (RIGHT)](#)

Trigger: A wipemycalother Reschedule runs (e.g., via /api/integrations/wipemycalother/wipe) for a booking with calendar/video references and the now-async `getCalendar` rejects (dynamic import failure) or `deleteEvent`/`deleteMeeting` rejects.

Impact: The enclosing try/catch that calls `logger.error` can no longer catch these errors because `forEach` discards the returned promise; rejections become unhandled (crashing Node under default settings) and cancellations are fire-and-forget, so the API can return success while third-party events remain undeleted.

Evidence: `bookingRefsFiltered.forEach(async (bookingRef) => {`

Suggested direction: Use `await Promise.all(bookingRefsFiltered.map(async (bookingRef) => {...}))` or a for...of loop with await inside the try block so rejections are caught and awaited.

## Audit trail

4 candidate(s) were retained in JSON but excluded from publication.
