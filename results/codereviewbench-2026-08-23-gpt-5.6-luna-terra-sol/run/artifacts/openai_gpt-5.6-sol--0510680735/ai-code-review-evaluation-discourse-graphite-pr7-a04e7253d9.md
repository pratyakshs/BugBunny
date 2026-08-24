# BugBunny review

PR: [code-review-benchmark/discourse__discourse-graphite__augment__PR7__20260122#1](https://github.com/code-review-benchmark/discourse__discourse-graphite__augment__PR7__20260122/pull/1)
Status: `completed`
Model: `openai/gpt-5.6-sol`
Base/head: `7554b5e3c458` → `d38c4d5f7443`
Coverage: 72/72 eligible hunks
Duration: 255.2s; model calls: 4

## Findings (4)

### 1. Preserve the original 30% metadata-link shade

`medium` · `bug` · [app/assets/stylesheets/desktop/topic-post.scss:291 (RIGHT)](#)

Trigger: A desktop user views an embedded or expanded reply whose .topic-meta-data h5 contains a link, under either the standard light scheme or a dark scheme.

Impact: The light branch changes the prior 30% shade to 70%, producing a very pale link on a light background; the dark branch is correspondingly too dark. The metadata link can lose contrast and become difficult to read.

Evidence: `color: dark-light-choose(scale-color($primary, $lightness: 70%), scale-color($secondary, $lightness: 30%));`

Suggested direction: Use dark-light-choose(scale-color($primary, $lightness: 30%), scale-color($secondary, $lightness: 70%)).

### 2. Desktop group-member names use the title shade instead of the original name shade

`medium` · `bug` · [app/assets/stylesheets/desktop/user.scss:522 (RIGHT)](#)

Trigger: A desktop user opens a group-member listing that renders .group-member-info .name.

Impact: Names that previously used the stronger 30% primary shade now use exactly the same 50% shade as the title at line 527, removing the intended visual hierarchy and reducing name contrast on both light and dark schemes.

Evidence: `color: dark-light-choose(scale-color($primary, $lightness: 50%), scale-color($secondary, $lightness: 50%));`

Suggested direction: Use dark-light-choose(scale-color($primary, $lightness: 30%), scale-color($secondary, $lightness: 70%)).

### 3. Mobile topic-map heading loses its stronger heading shade

`medium` · `bug` · [app/assets/stylesheets/mobile/topic-post.scss:182 (RIGHT)](#)

Trigger: A mobile user views a topic map containing an h3 heading under either a light or dark color scheme.

Impact: The h3 previously used a strong 20% shade but now uses the same 50%/50% color as the h4 immediately below it. This removes heading hierarchy and can reduce the h3's contrast against the topic-map background.

Evidence: `color: dark-light-choose(scale-color($primary, $lightness: 50%), scale-color($secondary, $lightness: 50%));`

Suggested direction: Use dark-light-choose(scale-color($primary, $lightness: 20%), scale-color($secondary, $lightness: 80%)), matching the original mobile value and the desktop topic-map h3 rule.

### 4. Mobile group-member names use the title shade instead of the original name shade

`medium` · `bug` · [app/assets/stylesheets/mobile/user.scss:497 (RIGHT)](#)

Trigger: A mobile user opens a group-member listing that renders .group-member-info .name.

Impact: Names that previously used the stronger 30% primary shade now use exactly the same 50% shade as the title at line 503, removing the intended visual hierarchy and reducing name contrast on both light and dark schemes.

Evidence: `color: dark-light-choose(scale-color($primary, $lightness: 50%), scale-color($secondary, $lightness: 50%));`

Suggested direction: Use dark-light-choose(scale-color($primary, $lightness: 30%), scale-color($secondary, $lightness: 70%)).

## Audit trail

3 candidate(s) were retained in JSON but excluded from publication.
