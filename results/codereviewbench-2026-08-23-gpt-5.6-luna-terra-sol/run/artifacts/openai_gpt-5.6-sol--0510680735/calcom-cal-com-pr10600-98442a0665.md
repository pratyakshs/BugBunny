# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR10600__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR10600__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `efa6d464a38e` → `54486a059cd2`
Coverage: 31/31 eligible hunks
Duration: 476.1s; model calls: 4

## Findings (4)

### 1. The same backup code can be accepted by concurrent login requests

`high` · `concurrency` · [packages/features/auth/lib/next-auth-options.ts:148 (RIGHT)](#)

Trigger: Two login requests using the same valid backup code reach authorization before either request persists its modified array.

Impact: Both requests read the original code, both pass indexOf, and both establish sessions, violating the documented exactly-once guarantee.

Evidence: `backupCodes[index] = null;`

Suggested direction: Consume codes atomically, for example by storing individually hashed codes in rows and deleting one with a conditional operation whose affected-row count must be one, or by using an optimistic version check and retry-safe transaction.

### 2. Uppercase backup codes are rejected during login

`medium` · `bug` · [packages/features/auth/lib/next-auth-options.ts:144 (RIGHT)](#)

Trigger: A user manually enters a generated hexadecimal backup code in uppercase, as suggested by the UI placeholder `XXXXX-XXXXX`.

Impact: The stored codes are lowercase hex strings, so a semantically identical uppercase code is reported as incorrect and the user remains locked out.

Evidence: `const index = backupCodes.indexOf(credentials.backupCode.replaceAll("-", ""));`

Suggested direction: Normalize submitted codes with trim(), hyphen removal, and toLowerCase() before lookup, and validate the resulting hexadecimal format.

### 3. The copy action reports success even when clipboard writing fails

`low` · `bug` · [apps/web/components/settings/EnableTwoFactorModal.tsx:278 (RIGHT)](#)

Trigger: Clipboard access is unavailable or denied, such as in an insecure self-hosted context, a restricted browser, or when permission is rejected.

Impact: The promise rejects while the UI still displays “Backup codes copied,” causing users to believe they saved recovery credentials that are not on the clipboard.

Evidence: `navigator.clipboard.writeText(backupCodes.map(formatBackupCode).join("\n"));
                    showToast(t("backup_codes_copied"), "success");`

Suggested direction: Await writeText, show the success toast only after it resolves, and show a failure message or fallback copy mechanism on rejection or when the API is unavailable.

### 4. Password visibility controls are removed from keyboard navigation globally

`low` · `bug` · [packages/ui/components/form/inputs/Input.tsx:49 (RIGHT)](#)

Trigger: A keyboard-only user tabs through any PasswordField and tries to activate its show/hide password control.

Impact: The native button can no longer receive sequential keyboard focus, making the visibility feature inaccessible without a pointer.

Evidence: `tabIndex={-1}`

Suggested direction: Remove tabIndex={-1}; if a particular dialog needs different initial focus, configure that dialog's autofocus rather than globally disabling focus on an interactive control.

## Audit trail

16 candidate(s) were retained in JSON but excluded from publication.
