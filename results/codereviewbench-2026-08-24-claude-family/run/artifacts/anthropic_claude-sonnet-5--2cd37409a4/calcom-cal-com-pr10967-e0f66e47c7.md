# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR10967__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR10967__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-sonnet-5`
Base/head: `a308075bc39b` → `de628295646d`
Coverage: 45/45 eligible hunks
Duration: 621.0s; model calls: 7

## Findings (3)

### 1. Unsafe access to destinationCalendar array element causes TypeError when no calendar is set

`critical` · `bug` · [packages/core/EventManager.ts:119 (RIGHT)](#)

Trigger: A booking whose evt.destinationCalendar is null/undefined/empty (e.g. no eventType or organizer destinationCalendar configured) uses MeetLocationType as the location

Impact: Destructuring an empty array leaves mainHostDestinationCalendar as undefined, and accessing .integration on it throws an uncaught TypeError, crashing EventManager.create() and the whole booking-creation flow

Evidence: `if (evt.location === MeetLocationType && mainHostDestinationCalendar.integration !== "google_calendar") {`

Suggested direction: Use optional chaining: mainHostDestinationCalendar?.integration !== "google_calendar"

### 2. updateEvent calendarId lookup can never match, always resolving to undefined

`high` · `bug` · [packages/app-store/googlecalendar/lib/CalendarService.ts:256 (RIGHT)](#)

Trigger: updateEvent is invoked without an externalCalendarId (falsy), causing this fallback branch to execute

Impact: The find() compares every destinationCalendar entry's externalId against the same falsy externalCalendarId, which never matches a real externalId, so selectedCalendar is always undefined and the Google Calendar API update call receives an undefined calendarId, failing the update

Evidence: `: event.destinationCalendar?.find((cal) => cal.externalId === externalCalendarId)?.externalId;`

Suggested direction: Select the destination calendar using a valid key (e.g. credentialId) instead of matching externalId against the already-known falsy externalCalendarId, e.g. fall back to event.destinationCalendar?.[0]?.externalId

### 3. Inverted condition sets organization slug only when team billing is enabled

`high` · `bug` · [packages/trpc/server/routers/viewer/organizations/create.handler.ts:151 (RIGHT)](#)

Trigger: Creating an organization, with IS_TEAM_BILLING_ENABLED either true or false

Impact: Previously slug was set directly on the organization only when billing was disabled (free orgs get an immediate slug, billed orgs wait for approval via requestedSlug in metadata). The flipped condition now sets slug directly only when billing IS enabled and omits it when disabled, bypassing the intended slug-approval flow for billed orgs and leaving free orgs without a slug

Evidence: `...(IS_TEAM_BILLING_ENABLED ? { slug } : {}),`

Suggested direction: Revert to the original condition: ...(!IS_TEAM_BILLING_ENABLED && { slug })

## Audit trail

7 candidate(s) were retained in JSON but excluded from publication.
