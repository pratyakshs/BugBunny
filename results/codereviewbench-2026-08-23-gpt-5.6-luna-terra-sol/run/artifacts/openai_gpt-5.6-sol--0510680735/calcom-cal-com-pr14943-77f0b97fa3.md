# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR14943__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR14943__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `917c7b0764f4` → `c790227e0cda`
Coverage: 4/4 eligible hunks
Duration: 266.7s; model calls: 4

## Findings (2)

### 1. Retry cleanup deletes reminders for non-SMS methods

`medium` · `data` · [packages/features/ee/workflows/api/scheduleSMSReminders.ts:39 (RIGHT)](#)

Trigger: The SMS cron runs while any EMAIL or WHATSAPP WorkflowReminder has retryCount greater than 1, such as after another worker or data operation uses the generic retryCount field on the shared model.

Impact: The SMS cron permanently deletes the unrelated email or WhatsApp reminder, so it cannot be scheduled or canceled through its normal worker.

Evidence: `retryCount: {`

Suggested direction: Place `method: WorkflowMethods.SMS` outside the `OR`, so both the expired-date and exhausted-retry predicates are restricted to SMS reminders.

### 2. Failure bookkeeping can abort the entire cron handler

`medium` · `bug` · [packages/features/ee/workflows/api/scheduleSMSReminders.ts:190 (RIGHT)](#)

Trigger: The retry update fails, for example because booking cancellation deleted the reminder after findMany or because the database write is temporarily unavailable.

Impact: The catch block throws a new unhandled error, the endpoint returns an error, and all later reminders in the loop are left unprocessed until a subsequent cron run.

Evidence: `await prisma.workflowReminder.update({`

Suggested direction: Guard retry bookkeeping with its own try/catch and use a conditional `updateMany` when a concurrently deleted row should be treated as a no-op, while logging both the scheduling and bookkeeping failures.

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
