# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR8087__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR8087__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-terra`
Base/head: `ba9688a04a83` → `820d7fa87e0c`
Coverage: 21/21 eligible hunks
Duration: 98.8s; model calls: 4

## Findings (3)

### 1. Await calendar cleanup callbacks before continuing reschedule

`medium` · `concurrency` · [packages/app-store/vital/lib/reschedule.ts:125 (RIGHT)](#)

Trigger: A Vital reschedule has a calendar reference while its app module is still loading asynchronously.

Impact: The loop does not await the callback promises, so the reschedule flow can send its email and return success while calendar deletion has not yet started; failures from loading or deleting the calendar are also detached from the surrounding try/catch.

Evidence: `bookingRefsFiltered.forEach(async (bookingRef) => {`

Suggested direction: Replace the forEach with `await Promise.all(bookingRefsFiltered.map(async (bookingRef) => { ... }))` inside the try block so all cleanup operations settle before the email and return path.

### 2. Await calendar cleanup callbacks before continuing reschedule

`medium` · `concurrency` · [packages/app-store/wipemycalother/lib/reschedule.ts:125 (RIGHT)](#)

Trigger: A WipeMyCal reschedule has a calendar reference while its app module is still loading asynchronously.

Impact: The function proceeds to send the reschedule email and return without waiting for calendar deletion to be initiated or completed, and asynchronous module-load/delete failures bypass the local try/catch.

Evidence: `bookingRefsFiltered.forEach(async (bookingRef) => {`

Suggested direction: Replace the forEach with an awaited `Promise.all(bookingRefsFiltered.map(async (bookingRef) => { ... }))` within the try block.

### 3. Do not discard asynchronous cancellation work in the viewer reschedule mutation

`medium` · `concurrency` · [packages/trpc/server/routers/viewer/bookings.tsx:553 (RIGHT)](#)

Trigger: A user reschedules a booking with a calendar reference and the corresponding app import has not already settled.

Impact: The mutation immediately moves on to send the reschedule email and complete its remaining work while the callback is still awaiting `getCalendar`; calendar deletion can occur after the mutation's visible success path, and a rejected import or deletion becomes an unhandled callback rejection.

Evidence: `bookingRefsFiltered.forEach(async (bookingRef) => {`

Suggested direction: Collect the per-reference async operations with `map` and `await Promise.all(...)` before sending the reschedule email or completing the mutation.

## Audit trail

1 candidate(s) were retained in JSON but excluded from publication.
