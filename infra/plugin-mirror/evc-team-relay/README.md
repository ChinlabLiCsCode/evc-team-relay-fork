# EVC Team Relay plugin — vendored mirror

A complete, verified-working copy of the "EVC Team Relay" Obsidian plugin
(`main.js`, `manifest.json`, `styles.css`), version 1.1.41.

## Why this exists

As of 2026-08-24, the plugin's upstream distribution is in a broken/transitional
state:

- It's been removed from Obsidian's official Community Plugins store, so new
  installs can no longer use Settings → Community plugins → Browse.
- Its GitHub repo was renamed (`entire-vc/evc-team-relay-obsidian-plugin` →
  `entire-vc/evc-team-relay-plugin`), and the new repo is currently **empty** — no
  commits, no releases, nothing to install from.

Existing installs are unaffected (removal from the store doesn't touch devices
that already have the plugin), but onboarding a *new* device has no working
upstream source right now. This directory is that source in the meantime — see
`docs/manual-plugin-install.md` (or the vault's own onboarding note) for the
install steps that use it.

This is version 1.1.41 specifically because it's the version already confirmed
working against this deployment (this admin machine has run it the whole time).
It is the **unpatched, stock build** — none of the machine-specific hacks
documented in the vault's "Team Relay Plugin Changes" note (the upload-throttle
speed patch) are applied here; that stays a separate, per-machine, per-need step
done after install.

## Updating this mirror

If `entire-vc/evc-team-relay-plugin` (or wherever it lands) becomes a working
source again with a newer version, prefer that over this mirror going forward —
this only exists to cover the gap. If you do want to refresh this mirror to a
newer version once one is available, replace all three files with the new
version's release assets and update the version-related claims in this README.
