# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR8087__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR8087__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-sonnet-5`
Base/head: `ba9688a04a83` → `820d7fa87e0c`
Coverage: 21/21 eligible hunks
Duration: 252.9s; model calls: 4

## Findings (2)

### 1. forEach(async) callback breaks try/catch and skips awaiting calendar cancellation

`high` · `concurrency` · [packages/app-store/vital/lib/reschedule.ts:125 (RIGHT)](#)

Trigger: Rescheduling a booking whose bookingRefsFiltered contains a `_calendar` reference where the newly-async `getCalendar(...)` call throws or rejects (e.g. malformed credential type, failed dynamic app import) or `calendar?.deleteEvent(...)` rejects.

Impact: Because Array.prototype.forEach ignores the promise returned by its async callback, any rejection from getCalendar/deleteEvent becomes an unhandled promise rejection instead of being caught by the surrounding try/catch, so the error is never logged via logger.error and the outer function returns before the calendar/meeting cancellation has actually completed.

Evidence: `bookingRefsFiltered.forEach(async (bookingRef) => {`

Suggested direction: Replace `bookingRefsFiltered.forEach(async (bookingRef) => {...})` with `await Promise.all(bookingRefsFiltered.map(async (bookingRef) => {...}))` inside the try block so rejections are awaited and caught.

### 2. forEach(async) callback breaks try/catch and skips awaiting calendar cancellation

`high` · `concurrency` · [packages/app-store/wipemycalother/lib/reschedule.ts:125 (RIGHT)](#)

Trigger: Rescheduling a booking whose bookingRefsFiltered contains a `_calendar` reference where the newly-async `getCalendar(...)` call throws or rejects (e.g. malformed credential type, failed dynamic app import) or `calendar?.deleteEvent(...)` rejects.

Impact: Because Array.prototype.forEach ignores the promise returned by its async callback, any rejection from getCalendar/deleteEvent becomes an unhandled promise rejection instead of being caught by the surrounding try/catch, so the error is never logged via logger.error and the outer function returns before the calendar/meeting cancellation has actually completed.

Evidence: `bookingRefsFiltered.forEach(async (bookingRef) => {`

Suggested direction: Replace `bookingRefsFiltered.forEach(async (bookingRef) => {...})` with `await Promise.all(bookingRefsFiltered.map(async (bookingRef) => {...}))` inside the try block so rejections are awaited and caught.

## Audit trail

2 candidate(s) were retained in JSON but excluded from publication.
