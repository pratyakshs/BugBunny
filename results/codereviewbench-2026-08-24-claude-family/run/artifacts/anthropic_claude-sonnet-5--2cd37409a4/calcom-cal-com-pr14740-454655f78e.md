# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR14740__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR14740__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-sonnet-5`
Base/head: `b004587262e8` → `92f44dcea7ff`
Coverage: 22/22 eligible hunks
Duration: 293.8s; model calls: 4

## Findings (1)

### 1. Team admin/owner permission check uses AND instead of OR

`high` · `bug` · [packages/trpc/server/routers/viewer/bookings/addGuests.handler.ts:46 (RIGHT)](#)

Trigger: A team ADMIN (not OWNER) who is not the booking organizer or an attendee attempts to add guests to a team booking.

Impact: The variable name and downstream usage (`!isTeamAdminOrOwner && !isOrganizer && !isAttendee`) imply team admins should be authorized, but requiring both isTeamAdmin AND isTeamOwner to be true means only owners (a subset already covered by isTeamAdmin's OR-role query) ever pass, so legitimate team admins are incorrectly forbidden from adding guests (FORBIDDEN error).

Evidence: `const isTeamAdminOrOwner =
    (await isTeamAdmin(user.id, booking.eventType?.teamId ?? 0)) &&
    (await isTeamOwner(user.id, booking.eventType?.teamId ?? 0));`

Suggested direction: Change the `&&` to `||` so a user who is either a team admin or a team owner is authorized: `(await isTeamAdmin(...)) || (await isTeamOwner(...))`.

## Audit trail

7 candidate(s) were retained in JSON but excluded from publication.
