# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR10967__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR10967__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-fable-5`
Base/head: `a308075bc39b` → `de628295646d`
Coverage: 45/45 eligible hunks
Duration: 501.3s; model calls: 7

## Findings (3)

### 1. Missing optional chaining crashes booking creation when no destination calendar exists with Google Meet location

`high` · `bug` · [packages/core/EventManager.ts:119 (RIGHT)](#)

Trigger: A booking is created with Google Meet as the location while evt.destinationCalendar is null or an empty array (e.g., organizer and event type have no destination calendar configured), making the array destructure yield undefined.

Impact: TypeError: Cannot read properties of undefined (reading 'integration') is thrown, aborting EventManager.create and failing the booking instead of falling back to Cal Video, which is exactly the scenario this check was written to handle.

Evidence: `if (evt.location === MeetLocationType && mainHostDestinationCalendar.integration !== "google_calendar") {`

Suggested direction: Use optional chaining: `mainHostDestinationCalendar?.integration !== "google_calendar"` so an absent destination calendar triggers the Cal Video fallback instead of throwing.

### 2. IS_TEAM_BILLING_ENABLED condition inverted when spreading organization slug

`high` · `bug` · [packages/trpc/server/routers/viewer/organizations/create.handler.ts:151 (RIGHT)](#)

Trigger: An organization is created via the create handler in either billing mode: the original code set `slug` only when team billing was NOT enabled (`...(!IS_TEAM_BILLING_ENABLED && { slug })`), but the rewrite dropped the negation.

Impact: With billing disabled, new organizations are created without a slug (breaking org URLs); with billing enabled, the slug is set immediately, bypassing the requestedSlug/payment-gated flow.

Evidence: `...(IS_TEAM_BILLING_ENABLED ? { slug } : {}),`

Suggested direction: Restore the negation: `...(!IS_TEAM_BILLING_ENABLED ? { slug } : {})`.

### 3. Tautological find() in Google updateEvent makes calendarId always undefined when externalCalendarId is missing

`high` · `bug` · [packages/app-store/googlecalendar/lib/CalendarService.ts:256 (RIGHT)](#)

Trigger: updateEvent is called with a falsy externalCalendarId (booking references created without externalCalendarId, which the new reference-building code makes more common); the fallback branch searches for a destination calendar whose externalId equals the falsy externalCalendarId.

Impact: The find() can never match, so `calendarId` is undefined and the Google API call `calendar.events.update({ calendarId: undefined, ... })` fails, whereas previously it fell back to the destination calendar's externalId. Reschedules/updates of such bookings fail.

Evidence: `: event.destinationCalendar?.find((cal) => cal.externalId === externalCalendarId)?.externalId;`

Suggested direction: Fall back to the destination calendar matching the current credential (e.g., `event.destinationCalendar?.find((cal) => cal.credentialId === this.credential.id)?.externalId`) or to the first destination calendar's externalId.

## Audit trail

12 candidate(s) were retained in JSON but excluded from publication.
