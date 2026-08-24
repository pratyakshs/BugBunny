# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR10967__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR10967__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-terra`
Base/head: `a308075bc39b` → `de628295646d`
Coverage: 45/45 eligible hunks
Duration: 190.9s; model calls: 4

## Findings (3)

### 1. Avoid dereferencing an absent destination calendar for Meet fallback

`high` · `bug` · [packages/core/EventManager.ts:119 (RIGHT)](#)

Trigger: A booking selects Google Meet while its event type and organizer have no destination calendar, leaving `evt.destinationCalendar` null or empty.

Impact: Event creation throws when reading `integration` from `undefined`, so the booking flow fails instead of falling back to Cal Video.

Evidence: `if (evt.location === MeetLocationType && mainHostDestinationCalendar.integration !== "google_calendar") {`

Suggested direction: Use optional chaining (for example, `mainHostDestinationCalendar?.integration !== "google_calendar"`) or explicitly handle an absent first destination before evaluating the fallback.

### 2. Do not drop collective-host calendars when the primary host has none

`high` · `bug` · [packages/features/bookings/lib/handleNewBooking.ts:1078 (RIGHT)](#)

Trigger: A collective event has no event-type destination calendar and the first/organizer host has no destination calendar, while one or more other collective hosts do have one.

Impact: The optional push is skipped because the initial value is `null`, so the other hosts' destination calendars are never sent to `EventManager` and no events are created for them.

Evidence: `evt.destinationCalendar?.push(...teamDestinationCalendars);`

Suggested direction: Initialize `destinationCalendar` to an empty array before appending collective-host destinations, or assign `teamDestinationCalendars` when no primary destination exists.

### 3. Do not omit organization slugs when team billing is disabled

`high` · `bug` · [packages/trpc/server/routers/viewer/organizations/create.handler.ts:151 (RIGHT)](#)

Trigger: An organization is created in an environment where `IS_TEAM_BILLING_ENABLED` is false.

Impact: The organization is created without the requested `slug`, whereas the previous branch stored the slug in this mode. The organization cannot be addressed by its requested slug and downstream slug-based organization behavior receives a missing value.

Evidence: `...(IS_TEAM_BILLING_ENABLED ? { slug } : {}),`

Suggested direction: Restore the original disabled-billing condition for assigning `slug`; retain the requested-slug metadata behavior for the enabled-billing path.

## Audit trail

11 candidate(s) were retained in JSON but excluded from publication.
