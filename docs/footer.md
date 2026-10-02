# Single-row footer support

The plugin supplies shortcut hints; Herdr owns the row. The footer occupies one
terminal row below both the sidebar and panes. Tabs keep their existing position
(default: top). It has no pane identity, cursor, input capture, or navigation
participation, and uses Herdr's current palette.

## Current availability

Stock Herdr 0.9.3 has no separate footer slot. This repository includes an
**experimental native extension**, based on Herdr commit
`7b116c05bfda646af39d2524c54e70c751f57ee8`, in
[`integrations/herdr-client-footer-v1.patch`](../integrations/herdr-client-footer-v1.patch).
It is a proposed client capability, not a released upstream API. No custom Herdr
binary is installed by the plugin. Upstream acceptance is not guaranteed.

The extension adds client-local `ui.footer` text/command entries and a read-only
`herdr --client-capabilities` probe returning `{"footer":1}`. It changes no session
protocol, endpoint codec, or server snapshot. Commands run only while the local
endpoint is connected; remote sessions do not receive desktop shortcut hints.

## Plugin behavior

On a supported build, installation adds its own provider only if `ui.footer` is
absent. An existing footer, including an explicit empty list, is preserved.
Updates are idempotent. Disable, removal, and downgrade remove only entries whose
command exactly matches this installation's provider. Personal additions,
separator, theme, and tab placement stay intact. Configuration uses the existing
atomic apply/reload/rollback mechanism.

`herdr-shell footer [--width N]` emits plain text on one line. Native rendering
passes the available width as `HERDR_FOOTER_WIDTH`; explicit `--width` overrides
it. The provider checks live desktop ownership, keeps the menu route first, and
adds complete action hints while they fit. Unavailable keys are omitted. Without
an active profile it shows the available native menu shortcut or CLI fallback.
The menu remains the complete shortcut sheet.

## Native extension boundaries

The client reserves a row from configured presence, even when a command fails,
so pane heights remain stable. Extremely short terminals suppress the footer to
preserve terminal space. Text is sanitized and clipped by display cells without
wrapping. Command output is bounded, time-limited, and polled away from rendering;
reload, endpoint changes, and shutdown cancel owned process groups.

The patch is for review and isolated testing. Applying it to a source checkout
requires a compatible Rust toolchain and Zig 0.16.0. Do not replace a working
Herdr binary merely to preview this feature. Native compilation and interactive
acceptance must be verified separately from plugin tests.

## Validation in this build

- 479 plugin unit tests passed (one existing environment-dependent skip).
- The isolated Omarchy add/update/disable/resume/remove probe passed with stock
  Herdr, including conflict handling and preservation of personal settings.
- The layout preview stayed on one row at 1048, 704, and 328 pixels, with Menu
  visible and tabs above panes. This verifies the preview, not native rendering.
- Native formatting, config-reference, and client hot-path architecture checks
  passed. Native Rust tests and interactive acceptance remain pending; no native
  binary was built or installed.
