# BugBunny review

PR: [code-review-benchmark/cal_dot_com__cal.com__augment__PR10600__20260122#1](https://github.com/code-review-benchmark/cal_dot_com__cal.com__augment__PR10600__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-luna`
Base/head: `efa6d464a38e` → `54486a059cd2`
Coverage: 31/31 eligible hunks
Duration: 208.4s; model calls: 4

## Findings (3)

### 1. Keep the settings state synchronized when the backup-code dialog is dismissed

`medium` · `bug` · [apps/web/components/settings/EnableTwoFactorModal.tsx:135 (RIGHT)](#)

Trigger: A user successfully enables 2FA and then dismisses the dialog through its close control, Escape key, backdrop, or another onOpenChange path instead of clicking the custom backup-codes close button.

Impact: The server has enabled 2FA, but the parent settings component never receives onEnable, so it continues showing 2FA as disabled and offers an enable action that subsequently fails with TwoFactorAlreadyEnabled.

Evidence: `setStep(SetupStep.DisplayBackupCodes);`

Suggested direction: Prevent generic dialog dismissal after successful enable or route every post-enable dismissal through the same completion callback that updates the parent state, while preserving the backup-code display until the user confirms completion.

### 2. Do not remove the password visibility control from keyboard navigation

`low` · `bug` · [packages/ui/components/form/inputs/Input.tsx:49 (RIGHT)](#)

Trigger: A keyboard-only user tabs through a PasswordField and needs to reveal or hide the password.

Impact: The visibility button is skipped by keyboard focus, so the control cannot be used through normal keyboard navigation.

Evidence: `tabIndex={-1}`

Suggested direction: Remove tabIndex={-1} and keep the button in the tab order, or provide an equivalent keyboard-accessible control.

### 3. Associate the backup-code label with its input

`low` · `bug` · [apps/web/components/auth/BackupCode.tsx:13 (RIGHT)](#)

Trigger: A user clicks the visible Backup Code label or navigates the form with assistive technology.

Impact: The label is not associated with the input having id backup-code, so clicking it does not focus the field and screen readers may not announce the field relationship correctly.

Evidence: `<Label className="mt-4">{t("backup_code")}</Label>`

Suggested direction: Pass htmlFor="backup-code" to Label, or render the label through the field component so it is programmatically associated with the input.

## Audit trail

5 candidate(s) were retained in JSON but excluded from publication.
