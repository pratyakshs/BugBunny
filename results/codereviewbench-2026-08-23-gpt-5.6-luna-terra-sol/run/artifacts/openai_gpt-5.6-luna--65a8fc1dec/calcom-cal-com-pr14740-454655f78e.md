# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR14740__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR14740__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `b004587262e8` → `92f44dcea7ff`
Coverage: 22/22 eligible hunks
Duration: 177.2s; model calls: 4

## Findings (4)

### 1. Team admins who are not owners are denied access

`medium` · `api` · [packages/trpc/server/routers/viewer/bookings/addGuests.handler.ts:47 (RIGHT)](#)

Trigger: A team member with the ADMIN role but without the OWNER role invokes addGuests for a booking belonging to that team.

Impact: The request is rejected because both predicates must be true, so team admins cannot use the feature despite isTeamAdmin accepting either ADMIN or OWNER.

Evidence: `(await isTeamAdmin(user.id, booking.eventType?.teamId ?? 0)) &&
    (await isTeamOwner(user.id, booking.eventType?.teamId ?? 0));`

Suggested direction: Use the team-admin predicate alone, or combine the intended admin and owner checks with the appropriate OR logic instead of requiring both.

### 2. The API accepts duplicate guest addresses in one request

`medium` · `data` · [packages/trpc/server/routers/viewer/bookings/addGuests.schema.ts:5 (RIGHT)](#)

Trigger: A caller sends a valid tRPC request containing the same email more than once, such as guests: ["guest@example.com", "guest@example.com"].

Impact: The handler passes both entries to createMany, creating duplicate attendee records and causing duplicate calendar and email processing.

Evidence: `guests: z.array(z.string().email()),`

Suggested direction: Enforce uniqueness in the server schema or normalize and deduplicate the guest list in the handler before createMany.

### 3. Email comparisons allow case-variant duplicate attendees

`medium` · `data` · [packages/trpc/server/routers/viewer/bookings/addGuests.handler.ts:76 (RIGHT)](#)

Trigger: A booking already contains Guest@example.com and a caller adds guest@example.com, optionally alongside another new guest.

Impact: The case-variant address is treated as new and another attendee row is inserted for the same mailbox, leading to duplicate invitations and attendee data.

Evidence: `!booking.attendees.some((attendee) => guest === attendee.email) &&`

Suggested direction: Compare normalized email addresses, for example by lowercasing both existing and incoming addresses, and deduplicate the normalized request list before insertion.

### 4. Uppercase variants bypass the guest email blacklist

`medium` · `security` · [packages/trpc/server/routers/viewer/bookings/addGuests.handler.ts:77 (RIGHT)](#)

Trigger: BLACKLISTED_GUEST_EMAILS contains blocked@example.com and an authorized user submits Blocked@example.com.

Impact: The blocked mailbox is inserted as an attendee and receives the booking invitation, bypassing the configured blacklist.

Evidence: `!blacklistedGuestEmails.includes(guest)`

Suggested direction: Normalize the incoming guest address before comparison, or use a case-insensitive predicate against the normalized blacklist entries.

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
