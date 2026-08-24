# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR14740__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR14740__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-5`
Base/head: `b004587262e8` → `92f44dcea7ff`
Coverage: 22/22 eligible hunks
Duration: 279.5s; model calls: 4

## Findings (1)

### 1. Team permission check requires admin AND owner instead of either

`high` · `bug` · [packages/trpc/server/routers/viewer/bookings/addGuests.handler.ts:46 (RIGHT)](#)

Trigger: A team ADMIN (not OWNER) who is neither the booking organizer nor an attendee calls viewer.bookings.addGuests for a team event booking.

Impact: The mutation throws FORBIDDEN ("you_do_not_have_permission") even though the variable and intent are "admin or owner", so team admins cannot add guests; only owners can.

Evidence: `const isTeamAdminOrOwner =
    (await isTeamAdmin(user.id, booking.eventType?.teamId ?? 0)) &&
    (await isTeamOwner(user.id, booking.eventType?.teamId ?? 0));`

Suggested direction: Change the `&&` between the `isTeamAdmin` and `isTeamOwner` checks to `||` (or just use `isTeamAdmin`, which already returns truthy for ADMIN and OWNER).

## Audit trail

14 candidate(s) were retained in JSON but excluded from publication.
