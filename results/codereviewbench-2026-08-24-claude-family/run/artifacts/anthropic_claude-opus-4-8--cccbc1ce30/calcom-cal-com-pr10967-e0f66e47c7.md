# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR10967__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR10967__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-opus-4-8`
Base/head: `a308075bc39b` → `de628295646d`
Coverage: 45/45 eligible hunks
Duration: 276.6s; model calls: 7

## Findings (2)

### 1. Null dereference on mainHostDestinationCalendar.integration when no destination calendar

`high` · `bug` · [packages/core/EventManager.ts:119 (RIGHT)](#)

Trigger: A booking whose location is Google Meet (MeetLocationType) but whose CalendarEvent has an empty or null destinationCalendar (e.g. organizer and eventType both lack a destination calendar, so handleNewBooking sets it to null/[]).

Impact: `evt.destinationCalendar ?? []` yields [], the destructured mainHostDestinationCalendar is undefined, and reading `.integration` throws a TypeError, aborting event creation.

Evidence: `if (evt.location === MeetLocationType && mainHostDestinationCalendar.integration !== "google_calendar") {`

Suggested direction: Use optional chaining as before, e.g. `mainHostDestinationCalendar?.integration !== "google_calendar"`.

### 2. updateEvent selectedCalendar fallback compares externalId against the falsy externalCalendarId

`high` · `bug` · [packages/app-store/googlecalendar/lib/CalendarService.ts:256 (RIGHT)](#)

Trigger: updateEvent invoked with a falsy externalCalendarId while relying on event.destinationCalendar to supply the target calendar.

Impact: The find compares each cal.externalId against the falsy externalCalendarId, so it never matches; selectedCalendar becomes undefined and is passed as calendarId to the Google update, causing the update to fail/target the wrong calendar.

Evidence: `: event.destinationCalendar?.find((cal) => cal.externalId === externalCalendarId)?.externalId;`

Suggested direction: When externalCalendarId is falsy, fall back to the first/main destinationCalendar's externalId (e.g. `event.destinationCalendar?.[0]?.externalId`) rather than filtering by the falsy value.

## Audit trail

4 candidate(s) were retained in JSON but excluded from publication.
