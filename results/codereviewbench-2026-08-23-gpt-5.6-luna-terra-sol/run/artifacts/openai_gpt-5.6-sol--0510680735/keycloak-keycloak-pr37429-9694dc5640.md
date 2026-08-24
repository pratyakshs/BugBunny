# BugBunny review

PR: [code-review-benchmark/keycloak__keycloak__augment__PR37429__20260122#1](https://github.com/code-review-benchmark/keycloak__keycloak__augment__PR37429__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `f3c8e8f34871` → `02f48f776f43`
Coverage: 52/52 eligible hunks
Duration: 407.6s; model calls: 4

## Findings (6)

### 1. Java MessageFormat syntax breaks account-ui interpolation

`medium` · `bug` · [js/apps/account-ui/maven-resources/theme/keycloak.v3/account/messages/messages_en.properties:188 (RIGHT)](#)

Trigger: The account UI translates `error-invalid-multivalued-size` with numeric interpolation arguments through its i18next resource bundle.

Impact: i18next no longer recognizes the former `{{0}}`, `{{1}}`, and `{{2}}` placeholders, and it does not evaluate Java `choice` syntax, so users see literal formatting expressions instead of the attribute limits.

Evidence: `error-invalid-multivalued-size=Attribute {0} must have at least {1} and at most {2} {2,choice,0#values|1#value|1<values}.`

Suggested direction: Keep i18next interpolation syntax in this JS resource and implement singular/plural wording using the frontend's supported pluralization mechanism rather than Java MessageFormat.

### 2. Java MessageFormat syntax breaks admin-ui interpolation

`medium` · `bug` · [js/apps/admin-ui/maven-resources/theme/keycloak.v2/admin/messages/messages_en.properties:3138 (RIGHT)](#)

Trigger: The admin UI translates `error-invalid-multivalued-size` with numeric interpolation arguments through its i18next resource bundle.

Impact: The placeholders and Java `choice` clause are displayed literally instead of being replaced with the attribute limits and pluralized text.

Evidence: `error-invalid-multivalued-size=Attribute {0} must have at least {1} and at most {2} {2,choice,0#values|1#value|1<values}.`

Suggested direction: Restore `{{0}}`, `{{1}}`, and `{{2}}`-style interpolation and use i18next-compatible pluralization for this frontend resource.

### 3. Lithuanian account text was replaced with Italian

`low` · `doc_defect` · [themes/src/main/resources-community/theme/base/account/messages/messages_lt.properties:101 (RIGHT)](#)

Trigger: A user selects Lithuanian while configuring TOTP through the account theme.

Impact: The first setup instruction is displayed in Italian in an otherwise Lithuanian interface.

Evidence: `totpStep1=Installa una delle seguenti applicazioni sul tuo cellulare:`

Suggested direction: Replace the value with a Lithuanian translation of “Install one of the following applications on your mobile.”

### 4. Lithuanian login text was replaced with Italian

`low` · `doc_defect` · [themes/src/main/resources-community/theme/base/login/messages/messages_lt.properties:71 (RIGHT)](#)

Trigger: A user selects Lithuanian while configuring TOTP during login.

Impact: The login flow displays an Italian setup instruction in the Lithuanian locale.

Evidence: `loginTotpStep1=Installa una delle seguenti applicazioni sul tuo cellulare:`

Suggested direction: Provide the corresponding Lithuanian translation instead of the Italian sentence.

### 5. Simplified Chinese bundle now contains Traditional Chinese text

`low` · `doc_defect` · [themes/src/main/resources-community/theme/base/account/messages/messages_zh_CN.properties:112 (RIGHT)](#)

Trigger: A user selects the `zh_CN` locale and opens the account-theme TOTP setup flow.

Impact: The instruction uses Traditional forms such as `手機`, `安裝`, and `應用程式`, making the Simplified Chinese locale inconsistent and incorrectly localized.

Evidence: `totpStep1=在您的手機上安裝以下應用程式之一：`

Suggested direction: Translate the sentence into Simplified Chinese, for example using `手机`, `安装`, and `应用程序`.

### 6. Anchor sanitizer method name is misspelled

`low` · `style` · [misc/theme-verifier/src/main/java/org/keycloak/themeverifier/VerifyMessageProperties.java:150 (RIGHT)](#)

Trigger: A maintainer searches for or modifies the sanitizer implementation and its call site.

Impact: The misspelled identifier (`santize`) obscures the method's purpose and propagates incorrect terminology in future references.

Evidence: `private String santizeAnchors(String key, String value, String englishValue) {`

Suggested direction: Rename the method and its call site to `sanitizeAnchors`.

## Audit trail

5 candidate(s) were retained in JSON but excluded from publication.
