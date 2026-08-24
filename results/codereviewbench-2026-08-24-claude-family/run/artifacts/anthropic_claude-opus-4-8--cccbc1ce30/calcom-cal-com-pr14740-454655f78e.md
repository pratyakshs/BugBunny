# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR14740__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR14740__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-4-8`
Base/head: `b004587262e8` → `92f44dcea7ff`
Coverage: 22/22 eligible hunks
Duration: 134.7s; model calls: 4

## Findings (2)

### 1. Team-admin permission check requires OWNER, blocking admins

`medium` · `bug` · [packages/trpc/server/routers/viewer/bookings/addGuests.handler.ts:47 (RIGHT)](#)

Trigger: A team ADMIN (not OWNER) who is neither the organizer nor an attendee attempts to add guests to a team booking.

Impact: The variable named isTeamAdminOrOwner is only true for OWNERs because isTeamAdmin already returns true for ADMIN/OWNER and is ANDed with isTeamOwner; a legitimate team admin is rejected with FORBIDDEN.

Evidence: `(await isTeamAdmin(user.id, booking.eventType?.teamId ?? 0)) &&
    (await isTeamOwner(user.id, booking.eventType?.teamId ?? 0));`

Suggested direction: Combine the checks with `||` (or just use isTeamAdmin, which already covers owners) so admins are permitted as the name implies.

### 2. Blacklist comparison is case-sensitive against lowercased list

`medium` · `security` · [packages/trpc/server/routers/viewer/bookings/addGuests.handler.ts:77 (RIGHT)](#)

Trigger: A blacklisted email is submitted with any uppercase characters (e.g. Blocked@Example.com) while BLACKLISTED_GUEST_EMAILS stores it lowercased.

Impact: blacklistedGuestEmails is lowercased but `guest` is not normalized, so mixed-case blacklisted addresses pass the filter and get added as attendees, defeating the blacklist.

Evidence: `!blacklistedGuestEmails.includes(guest)`

Suggested direction: Compare `guest.toLowerCase()` against the lowercased blacklist entries.

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
