# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR7__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR7__20260122/pull/1)
Status: `completed`
Model: `anthropic/claude-sonnet-5`
Base/head: `7554b5e3c458` → `d38c4d5f7443`
Coverage: 72/72 eligible hunks
Duration: 126.7s; model calls: 4

## Findings (5)

### 1. Light-theme lightness value changed from 30% to 70% during dark-light-choose refactor

`medium` · `bug` · [app/assets/stylesheets/desktop/topic-post.scss:291 (RIGHT)](#)

Trigger: Page rendered with the default (light) theme where $primary is darker than $secondary, causing dark-light-choose to return the first (light-theme) argument for `.topic-meta-data h5 a`.

Impact: The link color inside .topic-meta-data h5 becomes noticeably lighter (scale-color($primary,70%) instead of the original scale-color($primary,30%)), an unintended visual regression for the default light theme.

Evidence: `color: dark-light-choose(scale-color($primary, $lightness: 70%), scale-color($secondary, $lightness: 30%));`

Suggested direction: Preserve the original light-theme value: use scale-color($primary, $lightness: 30%) as the first argument and pick an appropriate complementary secondary lightness for the dark-theme branch.

### 2. Light-theme lightness value changed from 30% to 50% in group-member-info .name (desktop)

`medium` · `bug` · [app/assets/stylesheets/desktop/user.scss:522 (RIGHT)](#)

Trigger: Page rendered with the default (light) theme where $primary is darker than $secondary, causing dark-light-choose to return the first (light-theme) argument for `.group-member-info .name`.

Impact: The name text color under .group-member-info becomes lighter than before (scale-color($primary,50%) instead of the original scale-color($primary,30%)), an unintended visual regression in the default light theme.

Evidence: `color: dark-light-choose(scale-color($primary, $lightness: 50%), scale-color($secondary, $lightness: 50%));`

Suggested direction: Preserve the original light-theme value: use scale-color($primary, $lightness: 30%) for the light-theme branch and derive an appropriate complementary value for the dark-theme branch.

### 3. Light-theme lightness value changed from 70% to 30% in .custom-message-length (mobile modal)

`medium` · `bug` · [app/assets/stylesheets/mobile/modal.scss:102 (RIGHT)](#)

Trigger: Page rendered with the default (light) theme where $primary is darker than $secondary, causing dark-light-choose to return the first (light-theme) argument for `.custom-message-length`.

Impact: The hint text color becomes noticeably darker than before (scale-color($primary,30%) instead of the original scale-color($primary,70%)), an unintended visual regression in the default light theme.

Evidence: `color: dark-light-choose(scale-color($primary, $lightness: 30%), scale-color($secondary, $lightness: 70%));`

Suggested direction: Preserve the original light-theme value: use scale-color($primary, $lightness: 70%) for the light-theme branch and derive the complementary secondary lightness for the dark-theme branch.

### 4. Light-theme lightness value changed from 20% to 50% in topic-map h3 (mobile)

`medium` · `bug` · [app/assets/stylesheets/mobile/topic-post.scss:182 (RIGHT)](#)

Trigger: Page rendered with the default (light) theme where $primary is darker than $secondary, causing dark-light-choose to return the first (light-theme) argument for the topic-map `h3`.

Impact: The h3 heading color under .topic-map becomes lighter than before (scale-color($primary,50%) instead of the original scale-color($primary,20%)), an unintended visual regression in the default light theme.

Evidence: `color: dark-light-choose(scale-color($primary, $lightness: 50%), scale-color($secondary, $lightness: 50%));`

Suggested direction: Preserve the original light-theme value: use scale-color($primary, $lightness: 20%) for the light-theme branch and pick an appropriate complementary secondary lightness for the dark-theme branch.

### 5. Light-theme lightness value changed from 30% to 50% in group-member-info .name (mobile)

`medium` · `bug` · [app/assets/stylesheets/mobile/user.scss:497 (RIGHT)](#)

Trigger: Page rendered with the default (light) theme where $primary is darker than $secondary, causing dark-light-choose to return the first (light-theme) argument for `.group-member-info .name` on mobile.

Impact: The name text color under .group-member-info becomes lighter than before (scale-color($primary,50%) instead of the original scale-color($primary,30%)), an unintended visual regression in the default light theme.

Evidence: `color: dark-light-choose(scale-color($primary, $lightness: 50%), scale-color($secondary, $lightness: 50%));`

Suggested direction: Preserve the original light-theme value: use scale-color($primary, $lightness: 30%) for the light-theme branch and derive an appropriate complementary value for the dark-theme branch.
