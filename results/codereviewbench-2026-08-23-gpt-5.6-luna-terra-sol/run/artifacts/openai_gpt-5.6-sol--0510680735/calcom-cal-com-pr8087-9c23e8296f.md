# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR8087__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR8087__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `ba9688a04a83` → `820d7fa87e0c`
Coverage: 21/21 eligible hunks
Duration: 360.3s; model calls: 4

## Findings (3)

### 1. Async cleanup errors escape the reschedule error handler

`medium` · `bug` · [packages/app-store/vital/lib/reschedule.ts:125 (RIGHT)](#)

Trigger: A Vital reschedule request whose calendar service constructor or video-adapter initialization throws, such as when a stored credential has malformed key data.

Impact: Array.forEach discards the async callback's promise, so the surrounding try/catch cannot log the failure. The rejection becomes unhandled and can terminate the request or process while the reschedule flow continues without completing integration cleanup.

Evidence: `bookingRefsFiltered.forEach(async (bookingRef) => {`

Suggested direction: Replace the forEach with `await Promise.all(bookingRefsFiltered.map(async (...) => ...))`, or use an awaited for...of loop, keeping it inside the existing try/catch.

### 2. Async cleanup errors escape the reschedule error handler

`medium` · `bug` · [packages/app-store/wipemycalother/lib/reschedule.ts:125 (RIGHT)](#)

Trigger: A Wipe My Cal reschedule request whose calendar service constructor or video-adapter initialization throws, such as when a stored credential has malformed key data.

Impact: Array.forEach discards the async callback's promise, so the surrounding try/catch cannot log the failure. The rejection becomes unhandled and can terminate the request or process while the reschedule flow continues without completing integration cleanup.

Evidence: `bookingRefsFiltered.forEach(async (bookingRef) => {`

Suggested direction: Replace the forEach with `await Promise.all(bookingRefsFiltered.map(async (...) => ...))`, or use an awaited for...of loop, keeping it inside the existing try/catch.

### 3. Reschedule RPC ignores asynchronous integration cleanup failures

`medium` · `bug` · [packages/trpc/server/routers/viewer/bookings.tsx:553 (RIGHT)](#)

Trigger: A request-reschedule RPC where calendar service construction or video-adapter initialization throws, for example because the selected credential contains malformed key data.

Impact: The callback rejection is not awaited or propagated, so the RPC can continue sending notifications and report success despite failed integration cleanup, while also producing an unhandled rejection. Before the async conversion, a synchronous construction error propagated out of the procedure.

Evidence: `bookingRefsFiltered.forEach(async (bookingRef) => {`

Suggested direction: Await all cleanup callbacks with `await Promise.all(bookingRefsFiltered.map(async (...) => ...))`, or use an awaited for...of loop so failures propagate through the RPC.
