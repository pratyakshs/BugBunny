# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR10967__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR10967__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-5`
Base/head: `a308075bc39b` → `de628295646d`
Coverage: 45/45 eligible hunks
Duration: 462.0s; model calls: 7

## Findings (3)

### 1. Unguarded property access on empty destinationCalendar array in EventManager.create

`critical` · `bug` · [packages/core/EventManager.ts:119 (RIGHT)](#)

Trigger: Booking an event whose location is Google Meet (MeetLocationType) for a user/event type with no destination calendar, so `evt.destinationCalendar` is `null` or `[]` and destructuring yields `undefined`.

Impact: `mainHostDestinationCalendar.integration` throws `TypeError: Cannot read properties of undefined`, aborting event creation for the whole booking instead of falling back to Cal Video.

Evidence: `if (evt.location === MeetLocationType && mainHostDestinationCalendar.integration !== "google_calendar") {`

Suggested direction: Use optional chaining: `mainHostDestinationCalendar?.integration !== "google_calendar"`.

### 2. Organization slug/requestedSlug billing condition inverted

`high` · `bug` · [packages/trpc/server/routers/viewer/organizations/create.handler.ts:151 (RIGHT)](#)

Trigger: Creating an organization in any deployment: with `IS_TEAM_BILLING_ENABLED=true` both `slug` and `metadata.requestedSlug` are now written; with billing disabled (self-hosted default) no `slug` is written at all.

Impact: Self-hosted organizations are created without a slug (org subdomain/routing broken), and billing-enabled orgs get a live slug before checkout, contradicting the requestedSlug flow.

Evidence: `...(IS_TEAM_BILLING_ENABLED ? { slug } : {}),`

Suggested direction: Restore the original polarity: `...(!IS_TEAM_BILLING_ENABLED ? { slug } : {})` while keeping `requestedSlug` under `IS_TEAM_BILLING_ENABLED`.

### 3. Google updateEvent fallback compares destinationCalendar against the falsy externalCalendarId

`high` · `bug` · [packages/app-store/googlecalendar/lib/CalendarService.ts:256 (RIGHT)](#)

Trigger: Updating/rescheduling a booking where `externalCalendarId` is not supplied (booking reference without externalCalendarId), so the ternary takes the else branch.

Impact: The `find` compares each destination calendar's externalId to the falsy `externalCalendarId`, never matches, so `selectedCalendar` is `undefined` and `calendar.events.update` is called with `calendarId: undefined`, failing or targeting the wrong calendar instead of the configured destination calendar.

Evidence: `: event.destinationCalendar?.find((cal) => cal.externalId === externalCalendarId)?.externalId;`

Suggested direction: In the fallback branch pick the destination calendar by credential/first entry, e.g. `event.destinationCalendar?.find((cal) => cal.credentialId === this.credential.id)?.externalId ?? event.destinationCalendar?.[0]?.externalId`.

## Audit trail

16 candidate(s) were retained in JSON but excluded from publication.
