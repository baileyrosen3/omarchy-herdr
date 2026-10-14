# Contributing

Start with the [manual test guide](docs/ui-ux-audit.md#manual-test-guide).
Include your Herdr version, terminal, terminal dimensions, and reproduction
steps when reporting an issue.

## Local development

Use a developer checkout when changing the plugin. Disable any Omarchy-managed
installation before switching to it. Run setup from a local Herdr pane:

```sh
git clone https://github.com/baileyrosen3/omarchy-herdr.git
cd omarchy-herdr
./bin/herdr-shell install
./bin/herdr-shell install --apply
./bin/herdr-shell desktop install
./bin/herdr-shell desktop install --apply
```

Keep the checkout linked: Python changes load on the next invocation. Close
and reopen the menu after editing. Reinstallation preserves customized native
fallback keys; explicit native key edits still use the configuration editor.

| Area | Source |
| --- | --- |
| Menu pages, state, and interaction | `herdr_shell/menu.py` |
| Terminal layout and action descriptions | `herdr_shell/interface.py` |
| Editors, choices, previews, and error dialogs | `herdr_shell/dialogs.py` |
| Action catalog, API calls, and manifest generation | `herdr_shell/actions.py` |
| Config transactions, validation, and undo | `herdr_shell/config.py` |
| Captured pane identity and socket requests | `herdr_shell/runtime.py` |
| CLI, installation, and deferred actions | `herdr_shell/cli.py` |
| Omarchy companion, offline setup, and cleanup | `omarchy/Service.qml`, `herdr_shell/omarchy.py`, `herdr_shell/managed.py` |
| Super+Alt profile and desktop setup | `herdr_shell/desktop.py` |
| Explicit Hyprland bindings and collision checks | `integrations/hyprland.lua` |
| Hands-on walkthrough and timed Speed Run | `herdr_shell/trainer.py`, `herdr_shell/practice.py`, `herdr_shell/game_input.py` |
| Welcome unlock state and read-only walkthrough | `herdr_shell/onboarding.py` |
| Native pane-spacing extension and builder | `integrations/herdr-native-spacing-v1.patch`, `scripts/build-native-padding.py` |

Keep menu actions, CLI behavior, and manifest entries consistent. After changing
the catalog or manifest generator:

```sh
python3 scripts/generate-manifest.py
./bin/herdr-shell install --apply
```

After changing the desktop profile or Lua integration, preview and reinstall it:

```sh
./bin/herdr-shell desktop install
./bin/herdr-shell desktop install --apply
```

### Developer updates and removal

From a Herdr pane with this checkout as the current directory:

```sh
./scripts/update.sh                       # Preview
./scripts/update.sh --apply               # Clean Git, fast-forward pull, refresh
./scripts/update.sh --local --apply       # Refresh current code without pulling
./scripts/remove.sh                       # Preview
./scripts/remove.sh --apply               # Remove only this installation's integration
```

These scripts keep the checkout, settings, history, and practice workspaces.
Update preserves custom fallback keys and a disabled desktop profile. Outside
Herdr, select a session with `--session default` or `--socket PATH`. The normal
user workflow uses Omarchy's add/update/remove commands, as shown in the README.

## Checks

Validate behavior in isolated sessions before installing into your working
desktop. Report which checks passed and which live interactions remain unverified.
GitHub Actions runs the unit suite on Python 3.11 and 3.13, the isolated Omarchy lifecycle probe, and the live and trainer probes against pinned stock Herdr 0.9.3.

To run the checks yourself:

```sh
python3 -m unittest discover -s tests -v
python3 tests/probe.py
python3 tests/trainer_probe.py
python3 tests/actions_probe.py
python3 tests/omarchy_probe.py --run
```

The unit checks include the Lua bridge check and require `lua` and `luac`.
The live probe requires `tmux` and Herdr. It creates disposable Herdr/tmux
servers under `/tmp` and drives their menus. Read a live probe before running
it; do not point it at a working session. Generated captures and results are
local artifacts and are excluded from version control.

The live, trainer, and action probes share `tests/probe_support.py`. Send terminal
keys only after its focus helper confirms the attached client's rendered pane
title (and popup absence when needed); RPC replies and server focus snapshots
alone do not acknowledge the keyboard-input target. Their JSON results record
the resolved Herdr executable path and explicit native pane-spacing capability.

The trainer probe exercises the read-only walkthrough, ordered Hands-on, and
shuffled Speed Run in a temporary plugin copy and real Herdr/curses session.
Both live modes progress automatically through all 26 actions and 34 presses:
**68 verified presses**, using 65 real private-broker events and three F4 demos.
It checks the real menu, fresh practice workspace transition, typed-letter
rejection, wrong-chord penalty, paused/hinted clocks, saved scoreboard accuracy,
assisted-run personal-best exclusion, and simulator cleanup. Active-profile
status and desktop-focus checks are explicit headless fixtures; the probe
neither installs a host bridge nor verifies compositor input. Practice agents
are harmless simulations; Lazygit uses a temporary repository with a labeled
simulator fallback when unavailable.
Unit timing checks use a controlled monotonic clock so fixture preparation and
verification cannot count against player reaction time.
Check that welcome initially offers only the two walkthroughs, completing either
preserves a private unlock marker, and the completed choice shows all three plus
Exit welcome. Menu/CLI access must remain available before that unlock.

The opt-in Omarchy probe runs the packaged plugin manager and real offline Herdr
registration under a temporary HOME/XDG tree. Local Git transport and shell IPC
are fixtures, and a simulated compositor registry supplies binding reads and
registration. Collision detection, reconciliation, bridge files, and native
configuration validation use the real plugin code. It checks reserved recovery
keys, partial desktop conflicts, Python-only updates, disabled profiles,
disable/resume, and removal after source deletion. QML loading and real compositor
input need the separate QML/desktop checks.

The opt-in desktop check, `python3 tests/desktop_probe.py`, additionally requires
`foot`, a running Hyprland session with all 26 profile shortcuts active, and
write access to the existing `/dev/uinput` device through permitted device or
input-group permissions. Its Python standard-library fixture creates a temporary
keyboard named `herdr-shell-qa-<probe PID>`, sends keys only while its own
disposable foot window is focused, releases held keys and destroys the keyboard
on exit, and restores the pointer position and original window focus when that
window still exists. The fixture floats only its own window on the largest
logical monitor and moves the pointer inside it so mouse-following focus stays
predictable. It respects the saved pane-close confirmation preference while
always checking tab/workspace close protection. It creates no service or
persistent desktop configuration. If device access is unavailable, report desktop input checks as
unverified and use the manual guide. Compositor shortcuts must be checked with
this input fixture; terminal key injection alone does not verify them.

The full desktop probe includes launch and agent-cycle checks. Add
`--launches-only` to check only A, V, Q, and Shift+Q after input sanity checks.
A invokes the normal agent entrypoint through a harmless argument-recording
Omarchy stub on the disposable server's PATH; V runs Lazygit in a temporary Git
repository; Q/Shift+Q use reports belonging only to that server. These cases do
not launch your configured agent or use a working project.

Footer work has two verification boundaries: `tests/test_footer.py` and
`tests/test_footer_setup.py` check the provider and owned configuration lifecycle;
[Footer support](docs/footer.md) documents the separate native extension. The
plugin must never infer footer support from a version number or a successful
unknown-field config check. Native compile/render/process tests require an
isolated patched Herdr build; provider tests do not prove native rendering.

For desktop behavior, follow the manual guide in a spare pane. Refuse occupied
chords, including physical-key aliases, and never override desktop bindings.
Verify the actual foreground client and keep actions pinned to the captured
socket and terminal identity. API failures must not close or move the desktop
window. A preference flag alone does not prove the bridge is active.

Keep Ctrl+Alt directional pane focus strict, with separate tab/workspace chords.
Agent sweeps must preserve their blocked → done → working → idle order during a
cycle. Menu and shortcut closes share the running-work confirmation policy.

## Native pane spacing

The native extension targets Herdr v0.9.3, commit
`7b116c05bfda646af39d2524c54e70c751f57ee8`, with Rust 1.96.1 and Zig 0.16.0.
`scripts/build-native-padding.py` clones that tag, verifies its commit, applies
the tracked patch, builds with `--locked`, and checks the explicit
`{"pane_spacing":1}` capability. It advertises no footer support and changes
neither protocol 22 nor the endpoint codecs or handoff format.

Spacing defaults are 2 columns / 1 row between panes and 1 column / 1 row at
tab edges. Horizontal padding is half the gap; vertical padding stays at one
row rather than disappearing when the one-row gap is halved.

The disposable `tests/probe.py` waits for the native popup frame to disappear
and for the attached client's focused pane title to render bold before sending
the next shortcut. TUI text disappearing alone does not establish popup teardown.

```sh
# Build an isolated candidate, without changing the Herdr command:
python3 scripts/build-native-padding.py --output /tmp/herdr-padding-candidate

# Or install the user-owned binary and ~/.local/bin/herdr link:
python3 scripts/build-native-padding.py --install

# Reuse an already-patched checkout at the exact pinned revision:
python3 scripts/build-native-padding.py --source /path/to/patched-herdr --install
```

The builder discovers an installed mise Zig 0.16.0 without activating a global
shim; `ZIG=/absolute/path/to/zig` explicitly overrides it. It refuses unrelated
executables, unowned directories, symlinked artifacts, changed managed binaries,
and existing `--output` files. Native installation is separate from the normal
Omarchy plugin lifecycle; plugin removal does not remove this opted-in binary.
The patch must be rebased and revalidated before changing the native base version.

If the system PATH precedes `~/.local/bin`, the link alone is not activation.
Prepend only `~/.local/share/herdr-shell/native-spacing` in the shell and desktop
environment; that directory contains no other executable names. On Omarchy's
Lua compositor, use `hl.env("PATH", ...)` after its default environment setup:
`hyprctl setenv` does not update keybind dispatchers. Keep Omarchy's own bin
directory and the remaining PATH entries intact. New Fish shells can use
`fish_add_path --prepend --move --path "$HOME/.local/share/herdr-shell/native-spacing"`.
Check actual launcher and shell resolution before starting a new server.

Installing alone leaves running servers unchanged. For a working session,
first prove an old-to-candidate handoff on a disposable server with real
foreground work. The JSON API method is `server.live_handoff` with an absolute
`import_exe` path and `expected_protocol=22`, `expected_version="0.9.3"`.
Use a bounded long-timeout API connection, not a stop/restart or blind retry.
After success, reopen the terminal view using the installed candidate and check
public workspace/tab/pane IDs, shell/foreground process PIDs, PTYs, retained
output, and input. **Upstream handoff allocates new internal terminal IDs**:
reopen menus and reacquire any stored terminal-ID handles. This is not a promise
that encoded invocation contexts survive handoff.

Validation needs the actual native surface, not just Python provider tests:
four live menu edits, zero/reset, Gaps Off/On, lone/zoomed panes, mouse focus,
and terminal dimensions. Keep the stock capability gate and stock-client
compatibility checks. The patch includes layout/range/split-hit tests; native
hot-path architecture and fixed-geometry 1/15-pane profiling cover the shared
geometry path. `pane.layout` retains the underlying split topology while zoomed;
check the rendered zoomed pane for its edge margins.

## Pull requests

Explain the visible problem and resulting behavior. Include relevant validation
and its limits. For UI changes, cover search, empty/error states, back navigation,
and narrow terminals. Keep proposed changes focused and update the README when
shortcuts or installation behavior change.

The README banner is generated from the attributed upstream SVGs with
`python3 scripts/generate-cover.py`. It is a shortcut illustration, not a
runtime screenshot.
