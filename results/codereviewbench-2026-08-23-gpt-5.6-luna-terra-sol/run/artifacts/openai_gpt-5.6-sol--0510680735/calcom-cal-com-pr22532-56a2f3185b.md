# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR22532__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR22532__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `30a92a4d66b4` → `5fd11f9faa79`
Coverage: 21/21 eligible hunks
Duration: 440.5s; model calls: 4

## Findings (5)

### 1. The empty update does not refresh `SelectedCalendar.updatedAt`

`medium` · `data` · [packages/app-store/googlecalendar/lib/CalendarService.ts:1024 (RIGHT)](#)

Trigger: `fetchAvailabilityAndSetCache` completes after fetching and storing Google Calendar availability.

Impact: Prisma does not update an `@updatedAt` field when an update has an empty data clause, so the selected-calendar timestamps remain stale despite the stated refresh operation.

Evidence: `await SelectedCalendarRepository.updateManyByCredentialId(this.credential.id, {});`

Suggested direction: Pass an explicit timestamp, such as `{ updatedAt: new Date() }`, using an update-many-compatible input type.

### 2. The migration warning contradicts the SQL it documents

`low` · `doc_defect` · [packages/prisma/migrations/20250715160635_add_calendar_cache_updated_at/migration.sql:4 (RIGHT)](#)

Trigger: A maintainer reviews or troubleshoots this migration.

Impact: The warning claims the column has no default and cannot be applied to populated tables, while the actual statement supplies `DEFAULT NOW()`, giving reviewers incorrect deployment information.

Evidence: `- Added the required column `updatedAt` to the `CalendarCache` table without a default value. This is not possible if the table is not empty.`

Suggested direction: Remove or rewrite the warning to describe the actual backfill/default behavior and its implications for legacy timestamps.

### 3. Cache timestamps are always formatted with the US locale

`low` · `bug` · [packages/features/apps/components/CredentialActionsDropdown.tsx:89 (RIGHT)](#)

Trigger: A user whose locale is not `en-US` views a cache timestamp.

Impact: The date uses US ordering and 12-hour conventions even though the surrounding text is localized, producing inconsistent or ambiguous output for international users.

Evidence: `timestamp: new Intl.DateTimeFormat("en-US", {`

Suggested direction: Use the active application locale from the localization hook, or omit the locale argument so `Intl` uses the user's locale.

### 4. The webhook helper resolves `.env` relative to the caller's directory

`medium` · `bug` · [scripts/test-gcal-webhooks.sh:5 (RIGHT)](#)

Trigger: The executable is invoked conventionally from the repository root as `./scripts/test-gcal-webhooks.sh`, or from any directory other than `scripts`.

Impact: The script edits or creates `.env` one directory above the repository instead of the repository's environment file, while still reporting success.

Evidence: `ENV_FILE="../.env"`

Suggested direction: Resolve the repository root from the script location, such as `SCRIPT_DIR=$(cd -- "$(dirname -- "$0")" && pwd)` and `ENV_FILE="$SCRIPT_DIR/../.env"`.

### 5. The environment update uses macOS-only `sed -i` syntax

`medium` · `bug` · [scripts/test-gcal-webhooks.sh:68 (RIGHT)](#)

Trigger: The script runs on GNU/Linux and the environment file already contains `GOOGLE_WEBHOOK_URL`.

Impact: GNU `sed` interprets the empty argument differently and the replacement command fails, so the existing webhook URL is not updated.

Evidence: `sed -i '' -E "s|^GOOGLE_WEBHOOK_URL=.*|GOOGLE_WEBHOOK_URL=$TUNNEL_URL|" "$ENV_FILE"`

Suggested direction: Use a portable temporary-file replacement or branch between GNU `sed -i -E` and BSD `sed -i '' -E`.

## Audit trail

13 candidate(s) were retained in JSON but excluded from publication.
