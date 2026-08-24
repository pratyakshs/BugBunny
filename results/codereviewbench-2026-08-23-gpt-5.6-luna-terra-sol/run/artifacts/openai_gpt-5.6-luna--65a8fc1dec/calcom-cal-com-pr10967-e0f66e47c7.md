# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR10967__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR10967__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `a308075bc39b` → `de628295646d`
Coverage: 45/45 eligible hunks
Duration: 206.6s; model calls: 4

## Findings (3)

### 1. Avoid dereferencing a missing main destination calendar

`high` · `bug` · [packages/core/EventManager.ts:119 (RIGHT)](#)

Trigger: Create or update an event using the Google Meet location when `destinationCalendar` is null or an empty array.

Impact: The event flow throws a TypeError before it can fall back to the Daily integration, so bookings without a destination calendar fail.

Evidence: `if (evt.location === MeetLocationType && mainHostDestinationCalendar.integration !== "google_calendar") {`

Suggested direction: Use optional chaining or an explicit presence check, for example `mainHostDestinationCalendar?.integration !== "google_calendar"`.

### 2. Google updates lose the configured calendar when no reference external ID exists

`high` · `bug` · [packages/app-store/googlecalendar/lib/CalendarService.ts:256 (RIGHT)](#)

Trigger: Update a Google event when `externalCalendarId` is null or undefined but `event.destinationCalendar` contains the configured calendar.

Impact: The lookup compares every external ID with `undefined` and produces an undefined `calendarId`, causing the Google update request to fail instead of updating the configured calendar.

Evidence: `: event.destinationCalendar?.find((cal) => cal.externalId === externalCalendarId)?.externalId;`

Suggested direction: Use the first destination calendar’s external ID as the no-reference fallback, for example `event.destinationCalendar?.[0]?.externalId`, with an appropriate primary-calendar fallback.

### 3. Only the first destination calendar is persisted for multi-host bookings

`high` · `data` · [packages/features/bookings/lib/handleNewBooking.ts:1877 (RIGHT)](#)

Trigger: Create a collective booking whose event contains destination calendars for multiple hosts, especially when calendar creation is deferred until payment confirmation or a later booking operation reloads the booking.

Impact: The database booking retains only the first destination calendar, so later webhook, confirmation, reschedule, or retry flows reconstruct the event with only the main host and omit the other hosts’ calendars.

Evidence: `connect: { id: evt.destinationCalendar[0].id },`

Suggested direction: Persist all destination-calendar associations in a relation that supports multiple records, or persist the complete destination list in the booking data and load it in every event reconstruction path.

## Audit trail

8 candidate(s) were retained in JSON but excluded from publication.
