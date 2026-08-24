# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR14740__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR14740__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-terra`
Base/head: `b004587262e8` → `92f44dcea7ff`
Coverage: 22/22 eligible hunks
Duration: 154.4s; model calls: 4

## Findings (3)

### 1. Allow team admins to add guests

`medium` · `api` · [packages/trpc/server/routers/viewer/bookings/addGuests.handler.ts:47 (RIGHT)](#)

Trigger: An accepted member whose role is ADMIN, but not OWNER, adds guests to a booking for that team.

Impact: The request is rejected with FORBIDDEN even though the role is treated as a team administrator elsewhere and the variable is named isTeamAdminOrOwner.

Evidence: `(await isTeamAdmin(user.id, booking.eventType?.teamId ?? 0)) &&`

Suggested direction: Use an OR authorization check (or isTeamAdmin alone, since it already includes OWNER) instead of requiring both admin and owner membership checks.

### 2. Reject duplicate guest addresses at the API boundary

`medium` · `data` · [packages/trpc/server/routers/viewer/bookings/addGuests.schema.ts:5 (RIGHT)](#)

Trigger: A direct tRPC caller submits the same valid email address more than once in a single guests array.

Impact: Each duplicate passes the later existing-attendee filter and createMany inserts duplicate Attendee rows, leading to duplicate calendar recipients and email notifications.

Evidence: `guests: z.array(z.string().email()),`

Suggested direction: Add a schema refinement that rejects duplicate normalized email addresses before the handler performs createMany.

### 3. Normalize guest emails before applying the blacklist

`medium` · `security` · [packages/trpc/server/routers/viewer/bookings/addGuests.handler.ts:77 (RIGHT)](#)

Trigger: BLACKLISTED_GUEST_EMAILS contains a blocked lowercase address and a caller submits the same address using uppercase or mixed-case characters.

Impact: The blocked address is accepted, added as an attendee, and sent booking information despite the blacklist.

Evidence: `!blacklistedGuestEmails.includes(guest)`

Suggested direction: Normalize each submitted guest address, and preferably blacklist configuration entries, to the same case before comparison.

## Audit trail

4 candidate(s) were retained in JSON but excluded from publication.
