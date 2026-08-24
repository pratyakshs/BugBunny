# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR14740__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR14740__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `b004587262e8` → `92f44dcea7ff`
Coverage: 22/22 eligible hunks
Duration: 514.8s; model calls: 5

## Findings (4)

### 1. Team administrators are rejected because the permission checks are combined with AND

`medium` · `bug` · [packages/trpc/server/routers/viewer/bookings/addGuests.handler.ts:46 (RIGHT)](#)

Trigger: A user with the ADMIN role, but not OWNER, opens a team booking they do not personally organize or attend and invokes addGuests.

Impact: The booking list exposes the action to team administrators, but the mutation returns FORBIDDEN even though the variable and existing listing authorization treat administrators and owners equivalently.

Evidence: `const isTeamAdminOrOwner =
    (await isTeamAdmin(user.id, booking.eventType?.teamId ?? 0)) &&
    (await isTeamOwner(user.id, booking.eventType?.teamId ?? 0));`

Suggested direction: Use the result of isTeamAdmin directly because it already accepts ADMIN and OWNER, or combine separate role checks with OR rather than AND.

### 2. Calendar updates use the caller's credentials instead of the booking host's credentials

`high` · `data` · [packages/trpc/server/routers/viewer/bookings/addGuests.handler.ts:158 (RIGHT)](#)

Trigger: An authorized attendee or team owner who is not the booking organizer adds a guest.

Impact: EventManager cannot authenticate against the organizer's referenced calendars, so the attendee is stored in Cal.com while the organizer's external calendar event may remain unchanged or the mutation may fail after persisting the attendee.

Evidence: `const credentials = await getUsersCredentials(ctx.user);`

Suggested direction: Resolve credentials for the booking organizer and any assigned collective hosts referenced by the booking, then construct EventManager from those hosts rather than ctx.user.

### 3. Email classification uses the unfiltered request instead of the guests actually added

`medium` · `bug` · [packages/trpc/server/routers/viewer/bookings/addGuests.handler.ts:168 (RIGHT)](#)

Trigger: A request contains one existing attendee and at least one genuinely new guest.

Impact: The existing attendee is still present in the raw guests array and is incorrectly sent a normal booking confirmation instead of the guest-added update email.

Evidence: `await sendAddGuestsEmails(evt, guests);`

Suggested direction: Pass the normalized, deduplicated uniqueGuests list to sendAddGuestsEmails.

### 4. The error-toast fallback is unreachable and can show an undefined prefix

`low` · `bug` · [apps/web/components/dialog/AddGuestsDialog.tsx:43 (RIGHT)](#)

Trigger: A network-level tRPC failure has no err.data code or lacks a useful translated message.

Impact: The template literal remains truthy and produces text such as "undefined: Failed to fetch", so unable_to_add_guests is never used.

Evidence: `const message = `${err.data?.code}: ${t(err.message)}`;
      showToast(message || t("unable_to_add_guests"), "error");`

Suggested direction: Build the detailed message only when the relevant fields exist; otherwise pass t("unable_to_add_guests") directly.

## Audit trail

18 candidate(s) were retained in JSON but excluded from publication.
