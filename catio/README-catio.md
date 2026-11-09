# catio-custom — the Catio fork branch

`catio-custom` = upstream pin `6bb377a51d` + the five open-PR replays
(#1 clarify numbered options, #3 terminal previews, #4 progress captions,
#5 branch-from-message-link, #6 quiet group chats) + the catio local layer
(droplet working-tree remainder, committed 2026-10-30). PR branches are
inputs only — never merged/rebased/pushed; their content is cherry-picked here.

## Updating a host on this fork

Run `catio/update.sh` (fetch upstream, merge onto catio-custom, py_compile
sweep, rebuild env). Do NOT run `hermes update` on fork-seated hosts — the
stock updater assumes an upstream checkout and can strand the custom layer.

## Contents

- `catio/update.sh` — fork-aware update flow
- `catio/patches/` — archived hermes-update autostashes (2026-09-10,
  2026-10-26). Review before applying; not auto-applied anywhere.

## Conflict rules (from experience)

Locale YAML: union both sides. Code files owned wholly by one PR: take that
PR's exact tip content. After any replay: py_compile every changed .py,
YAML-parse every locale, diff wholly-owned files vs the PR tip.
