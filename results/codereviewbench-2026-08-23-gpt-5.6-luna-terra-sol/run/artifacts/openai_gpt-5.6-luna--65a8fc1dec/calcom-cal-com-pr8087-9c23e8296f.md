# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR8087__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR8087__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `ba9688a04a83` → `820d7fa87e0c`
Coverage: 21/21 eligible hunks
Duration: 139.1s; model calls: 4

## Findings (3)

### 1. Await integration cleanup during Vital rescheduling

`medium` · `bug` · [packages/app-store/vital/lib/reschedule.ts:125 (RIGHT)](#)

Trigger: A Vital reschedule contains a calendar or video booking reference whose asynchronous app import or deletion has not completed when the callback is invoked.

Impact: The reschedule flow proceeds to send its email and return success without waiting for the old calendar event or video meeting to be deleted; rejection from the callback is also not caught by the surrounding try/catch.

Evidence: `bookingRefsFiltered.forEach(async (bookingRef) => {`

Suggested direction: Replace the async forEach with an awaited Promise.all over map, or use a for...of loop with await, so every calendar/video deletion settles before sending the email and returning.

### 2. Await integration cleanup during WipeMyCalOther rescheduling

`medium` · `bug` · [packages/app-store/wipemycalother/lib/reschedule.ts:125 (RIGHT)](#)

Trigger: A WipeMyCalOther reschedule contains a calendar or video booking reference whose asynchronous app import or deletion has not completed when the callback is invoked.

Impact: The reschedule flow proceeds to send its email and return success without waiting for the old calendar event or video meeting to be deleted; rejection from the callback is also not caught by the surrounding try/catch.

Evidence: `bookingRefsFiltered.forEach(async (bookingRef) => {`

Suggested direction: Replace the async forEach with an awaited Promise.all over map, or use a for...of loop with await, so every calendar/video deletion settles before sending the email and returning.

### 3. Await integration cleanup before completing the reschedule mutation

`medium` · `bug` · [packages/trpc/server/routers/viewer/bookings.tsx:553 (RIGHT)](#)

Trigger: A viewer reschedule mutation contains a calendar or video booking reference and the newly asynchronous app lookup or deletion has not completed immediately.

Impact: The mutation sends the reschedule email and completes before the old external event or meeting is necessarily removed, while failures from the ignored callback promises bypass the mutation's error handling.

Evidence: `bookingRefsFiltered.forEach(async (bookingRef) => {`

Suggested direction: Replace the async forEach with an awaited Promise.all over map, or use a for...of loop with await, before sending the reschedule email.
