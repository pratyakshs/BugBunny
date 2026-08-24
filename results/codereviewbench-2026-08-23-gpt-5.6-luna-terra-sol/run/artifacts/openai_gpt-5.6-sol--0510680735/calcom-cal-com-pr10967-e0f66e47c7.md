# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR10967__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR10967__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `a308075bc39b` → `de628295646d`
Coverage: 45/45 eligible hunks
Duration: 338.8s; model calls: 4

## Findings (5)

### 1. Google Meet fallback dereferences a missing destination calendar

`high` · `bug` · [packages/core/EventManager.ts:119 (RIGHT)](#)

Trigger: A booking selects Google Meet while the event has a null or empty destinationCalendar array, such as a user without a configured destination calendar.

Impact: Event creation throws while reading `integration` from undefined instead of falling back to Cal Video, so the booking flow fails.

Evidence: `if (evt.location === MeetLocationType && mainHostDestinationCalendar.integration !== "google_calendar") {`

Suggested direction: Use `mainHostDestinationCalendar?.integration !== "google_calendar"` or explicitly handle the empty-array case before accessing the property.

### 2. Only the first collective host calendar is persisted with the booking

`high` · `data` · [packages/features/bookings/lib/handleNewBooking.ts:1877 (RIGHT)](#)

Trigger: A collective booking has multiple host destination calendars and requires later calendar creation, for example because it requires confirmation or payment.

Impact: The database retains only the first destination calendar. Confirmation and payment handlers reconstruct the event from that singular relation, so calendar events are created only for the primary host and secondary hosts never receive their events.

Evidence: `connect: { id: evt.destinationCalendar[0].id },`

Suggested direction: Persist all destination-calendar identities needed by the booking, using an appropriate relation or booking metadata, and reconstruct the complete array in confirmation and payment flows.

### 3. Legacy Google destinations without credential IDs now ignore the selected calendar

`high` · `bug` · [packages/app-store/googlecalendar/lib/CalendarService.ts:147 (RIGHT)](#)

Trigger: An existing DestinationCalendar has a null `credentialId`, which the schema permits and the old EventManager branch explicitly supports, while its `externalId` points to a non-primary Google calendar.

Impact: No array entry matches the credential, so the event is created on `primary` instead of the configured destination calendar.

Evidence: `const selectedCalendar = calEventRaw.destinationCalendar?.find(
        (cal) => cal.credentialId === credentialId
      )?.externalId;`

Suggested direction: When no credential-ID match exists, fall back to the destination selected for the integration or pass the loop's destination external ID directly to GoogleCalendarService.

### 4. Google updates no longer fall back to the event destination calendar

`high` · `bug` · [packages/app-store/googlecalendar/lib/CalendarService.ts:256 (RIGHT)](#)

Trigger: An older booking reference has a null or missing `externalCalendarId`, while the CalendarEvent contains its configured destination calendar.

Impact: The fallback searches for an external ID equal to null/undefined and always returns undefined, so the Google update request has no calendar ID and fails instead of updating the configured calendar.

Evidence: `: event.destinationCalendar?.find((cal) => cal.externalId === externalCalendarId)?.externalId;`

Suggested direction: When the argument is absent, select the relevant destination calendar directly, preferably by credential ID, with a deliberate `primary` fallback if none exists.

### 5. Organization slug publication logic is reversed

`high` · `bug` · [packages/trpc/server/routers/viewer/organizations/create.handler.ts:151 (RIGHT)](#)

Trigger: An organization is created with either team billing enabled or disabled.

Impact: With billing enabled, the organization receives a live slug even though it should remain unpublished under `requestedSlug`; with billing disabled, the slug is omitted and the organization cannot be reached at the requested URL.

Evidence: `...(IS_TEAM_BILLING_ENABLED ? { slug } : {}),`

Suggested direction: Restore the negated condition: include `slug` only when `IS_TEAM_BILLING_ENABLED` is false, while keeping `requestedSlug` for the enabled case.

## Audit trail

13 candidate(s) were retained in JSON but excluded from publication.
