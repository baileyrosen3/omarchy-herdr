# Contributing

Start with the [manual test guide](docs/ui-ux-audit.md#manual-test-guide).
Include your Herdr version, terminal, terminal dimensions, and reproduction
steps when reporting an issue.

## Local development

Keep the checkout linked through `./bin/herdr-shell install --apply`. Python
changes load on the next invocation. Close and reopen the menu after editing.

| Area | Source |
| --- | --- |
| Menu pages, state, and interaction | `herdr_shell/menu.py` |
| Terminal layout and action descriptions | `herdr_shell/interface.py` |
| Editors, choices, previews, and error dialogs | `herdr_shell/dialogs.py` |
| Action catalog, API calls, and manifest generation | `herdr_shell/actions.py` |
| Config transactions, validation, and undo | `herdr_shell/config.py` |
| Captured pane identity and socket requests | `herdr_shell/runtime.py` |
| CLI, installation, and deferred actions | `herdr_shell/cli.py` |
| Super-key profile and desktop setup | `herdr_shell/desktop.py` |
| Hyprland binding wrapper | `integrations/hyprland.lua` |

Keep menu actions, CLI behavior, and manifest entries consistent. After changing
the catalog or manifest generator:

```sh
python3 scripts/generate-manifest.py
./bin/herdr-shell install --apply
```

After changing the desktop profile or Lua wrapper, preview and reinstall it:

```sh
./bin/herdr-shell desktop install
./bin/herdr-shell desktop install --apply
```

## Checks

The current publication includes syntax/import checks and a source-level UI
audit. It does not claim that the latest UI changes passed live tests.

To run the checks yourself:

```sh
python3 -m unittest discover -s tests -v
python3 tests/probe.py
```

The unit checks include the Lua bridge check and require `lua` and `luac`.
The live probe requires `tmux` and Herdr. It creates disposable Herdr/tmux
servers under `/tmp` and drives their menus. Read a live probe before running
it; do not point it at a working session. Generated captures and results are
local artifacts and are excluded from version control.

For desktop behavior, follow the manual guide in a spare pane. Preserve original
desktop dispatchers, verify the actual foreground client, and keep actions pinned
to the captured socket and terminal identity. Do not turn API failures into a
fallback that closes or moves the desktop window.

## Pull requests

Explain the visible problem and resulting behavior. Include relevant validation
and its limits. For UI changes, cover search, empty/error states, back navigation,
and narrow terminals. Keep proposed changes focused and update the README when
shortcuts or installation behavior change.

The README banner is generated from the attributed upstream SVGs with
`python3 scripts/generate-cover.py`. It is a shortcut illustration, not a
runtime screenshot.
