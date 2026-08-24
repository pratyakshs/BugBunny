# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR7232__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR7232__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `d1440bb5d2f2` → `6048e2a86b50`
Coverage: 21/21 eligible hunks
Duration: 179.5s; model calls: 4

## Findings (3)

### 1. Cancelled reminders are deleted before SendGrid cancellation runs

`high` · `bug` · [packages/features/ee/workflows/api/scheduleEmailReminders.ts:44 (RIGHT)](#)

Trigger: A cancelled scheduled email has a scheduledDate at or before the current time when the cron handler runs.

Impact: The earlier cleanup deletes the WorkflowReminder row before this query can find it, so no cancellation request is sent to SendGrid and the scheduled email can still be delivered.

Evidence: `const remindersToCancel = await prisma.workflowReminder.findMany({`

Suggested direction: Process cancelled reminders before the past-date cleanup, or exclude cancelled reminders from the cleanup until the SendGrid cancellation succeeds.

### 2. One failed cancellation aborts processing of all later reminders

`medium` · `bug` · [packages/features/ee/workflows/api/scheduleEmailReminders.ts:57 (RIGHT)](#)

Trigger: The SendGrid cancellation request for any reminder in remindersToCancel fails while other cancelled reminders remain in the same cron batch.

Impact: The outer catch exits the loop, leaving every subsequent reminder unprocessed; reminders close to their send time can therefore be delivered before a later cron run retries them.

Evidence: `await client.request({`

Suggested direction: Handle each reminder in its own try/catch, continue processing the remaining reminders, and retain failed rows for retry.

### 3. Verification controls introduce untranslated English text

`low` · `style` · [packages/features/ee/workflows/components/WorkflowStepContainer.tsx:439 (RIGHT)](#)

Trigger: A user edits a workflow while the application locale is not English and the phone-number verification controls are displayed.

Impact: The new verification input and its Verify button display English strings instead of the localized workflow UI text.

Evidence: `placeholder="Verification code"`

Suggested direction: Add translation keys for the placeholder and button label and render them through t(), consistent with the surrounding controls.

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
