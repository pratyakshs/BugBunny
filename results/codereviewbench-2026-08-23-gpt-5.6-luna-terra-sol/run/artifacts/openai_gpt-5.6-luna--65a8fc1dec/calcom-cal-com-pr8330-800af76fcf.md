# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR8330__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR8330__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `93cb21f55a2a` → `ee38fd295fd2`
Coverage: 9/9 eligible hunks
Duration: 216.2s; model calls: 4

## Findings (4)

### 1. Split working-hour ranges incorrectly reject later slots

`medium` · `bug` · [packages/trpc/server/routers/viewer/slots.ts:139 (RIGHT)](#)

Trigger: A user has multiple working-hour ranges on the same weekday, such as 09:00-10:00 and 14:00-17:00, and the generated slot falls in the second range.

Impact: The first range that does not contain the slot makes the predicate truthy, so the availability check returns false and valid slots from later ranges are omitted.

Evidence: `workingHours.find((workingHour) => {`

Suggested direction: Use a predicate that accepts the slot when any matching weekday range contains the entire slot, for example by using some() with an in-range condition and negating that result.

### 2. Date overrides bypass calendar busy-time checks

`high` · `bug` · [packages/trpc/server/routers/viewer/slots.ts:134 (RIGHT)](#)

Trigger: A host has a date override covering a slot that overlaps a busy calendar event or booking.

Impact: checkIfIsAvailable returns before executing busy.every, so the overlapping slot is returned as available and can be booked.

Evidence: `return true;`

Suggested direction: Use the date override only to select the applicable working-hours range, then continue through the busy-time checks before returning availability.

### 3. Date overrides are applied to users who do not own them

`high` · `bug` · [packages/trpc/server/routers/viewer/slots.ts:106 (RIGHT)](#)

Trigger: A multi-host event has a date override for one host, while another fixed or round-robin host has no override and is unavailable at the same time.

Impact: The global override list sets dateOverrideExist for the unrelated host, causing that host's normal-hours and busy checks to be skipped and allowing a slot where that host is unavailable.

Evidence: `dateOverrides.find((date) => {`

Suggested direction: Filter overrides by the schedule's userId before evaluating them, or pass the target user ID into checkIfIsAvailable and require matching override.userId.

### 4. Zero-length overrides are compared by object identity

`medium` · `bug` · [packages/trpc/server/routers/viewer/slots.ts:114 (RIGHT)](#)

Trigger: An unavailable or all-day date override is represented with identical start and end times, and another host's availability produces a candidate slot for that date.

Impact: The two separately-created Dayjs objects are never strictly equal, so the intended rejection branch is skipped and the zero-length override is treated as permitting the slot.

Evidence: `if (dayjs(date.start).add(utcOffset, "minutes") === dayjs(date.end).add(utcOffset, "minutes")) {`

Suggested direction: Compare the adjusted Dayjs values with isSame() or compare their millisecond timestamps, preserving the intended unavailable result for equal start and end times.

## Audit trail

2 candidate(s) were retained in JSON but excluded from publication.
