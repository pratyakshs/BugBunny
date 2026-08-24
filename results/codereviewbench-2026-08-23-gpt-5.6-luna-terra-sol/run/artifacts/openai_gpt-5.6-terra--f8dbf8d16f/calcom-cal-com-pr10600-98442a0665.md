# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR10600__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR10600__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-terra`
Base/head: `efa6d464a38e` → `54486a059cd2`
Coverage: 31/31 eligible hunks
Duration: 172.5s; model calls: 4

## Findings (2)

### 1. Require the primary credential when accepting a backup code

`high` · `security` · [packages/features/auth/lib/next-auth-options.ts:131 (RIGHT)](#)

Trigger: An attacker submits the credentials callback with a victim's email, a valid leaked backup code, and any non-empty `totpCode` value, while omitting or supplying an incorrect password.

Impact: The pre-existing password check is skipped whenever `totpCode` is non-empty, and this new backup-code branch accepts the backup code without validating the password, allowing account login with only the backup code.

Evidence: `if (user.twoFactorEnabled && credentials.backupCode) {`

Suggested direction: Do not use the presence of an untrusted `totpCode` request field to skip primary authentication; always verify the CAL-account password before accepting either TOTP or a backup code, or use a server-side pre-authentication state for flows that legitimately lack a password.

### 2. Keep the password-visibility button keyboard reachable

`low` · `bug` · [packages/ui/components/form/inputs/Input.tsx:49 (RIGHT)](#)

Trigger: A keyboard-only user tabs through any `PasswordField` and needs to reveal the password to verify or correct it.

Impact: The eye toggle is removed from the tab order, so keyboard users cannot activate this interactive password-visibility control while pointer users can.

Evidence: `tabIndex={-1}`

Suggested direction: Remove `tabIndex={-1}` so the native button remains keyboard focusable.

## Audit trail

6 candidate(s) were retained in JSON but excluded from publication.
