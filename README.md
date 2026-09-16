<p align="center">
  <img src="docs/assets/cover.svg" alt="Herdr × Omarchy — your desktop shortcuts, inside your terminal" width="100%">
</p>

<h1 align="center">Herdr × Omarchy</h1>

<p align="center">
  <strong>One set of shortcuts. From your desktop to your terminal workspace.</strong><br>
  A community Herdr plugin with focused Omarchy controls, a searchable command menu,<br>
  agent and tool panes, and configuration you can preview and undo.
</p>

<p align="center">
  <a href="#install"><img alt="Status: early preview" src="https://img.shields.io/badge/status-early_preview-e0af68?style=flat-square"></a>
  <a href="https://herdr.dev/"><img alt="Herdr 0.9.0 or newer" src="https://img.shields.io/badge/Herdr-0.9.0%2B-7dcfff?style=flat-square"></a>
  <img alt="Python 3.11 or newer" src="https://img.shields.io/badge/Python-3.11%2B-7aa2f7?style=flat-square">
  <a href="LICENSE"><img alt="Code license: MIT" src="https://img.shields.io/badge/code-MIT-9ece6a?style=flat-square"></a>
</p>

<p align="center">
  <a href="#install">Install</a> ·
  <a href="#shortcuts">Shortcuts</a> ·
  <a href="#the-control-menu">Control menu</a> ·
  <a href="#update">Update</a> ·
  <a href="#troubleshooting">Troubleshooting</a> ·
  <a href="docs/ui-ux-audit.md#manual-test-guide">Manual test guide</a>
</p>

---

## Your workspace, under your fingers

Focus a Herdr terminal and **Super+Space** opens your workspace controls.
**Super+A** starts an agent beside your current pane. **Super+G** opens Lazygit
in that pane's directory. Arrow keys move through panes, then continue through
tabs or workspaces when you reach an edge.

Focus another application and your original desktop shortcuts apply again.

| Feature | What it does |
| --- | --- |
| **Focused Super keys** | Route familiar desktop chords into the foreground Herdr client. |
| **Pane-first navigation** | Move to the nearest pane; continue to tabs or workspaces at an edge. |
| **Agents beside your work** | Launch the Omarchy agent picker in a regular pane, in the same tab and directory. |
| **A complete control menu** | Search commands, browse every action, switch destinations, and configure Herdr. |
| **Editable native shortcuts** | See inherited defaults and overrides, detect conflicts, and disable or restore bindings. |
| **Preview and undo** | Review config changes, validate with Herdr, reload, and undo managed edits. |

The registered plugin name is **Herdr Shell**, with ID **`blr.herdr-shell`**.
The repository is **`omarchy-herdr`** and the helper command is **`herdr-shell`**.

## Install

### Requirements

- **Linux and Herdr 0.9.0+**, with a local Herdr session running.
- **Python 3.11+**, curses, and `tomlkit`.
- For Super keys: **Omarchy's Lua-based Hyprland configuration**, including
  `~/.config/hypr/hyprland.lua` and its `require("default.hypr.omarchy")` loader.
- `luac` and `hyprctl` for desktop integration validation and reload.

Optional tools: `lazygit`, an editor selected through `$VISUAL` / `$EDITOR`
(default: `nvim`), and `yazi` or `ranger`. Agent panes use
`omarchy agent --inline --pick`; set up your preferred agent through Omarchy.

> [!NOTE]
> **Early preview.** Developed against Herdr 0.9.0 and Omarchy's Lua Hyprland
> configuration. Older `hyprland.conf` setups are outside the desktop bridge's
> current scope. The latest menu changes have syntax/import checks; their live
> verification is documented in the [manual test guide](docs/ui-ux-audit.md#manual-test-guide).

### 1. Get the plugin

On Omarchy / Arch:

```sh
sudo pacman -S --needed git python python-tomlkit lua

mkdir -p ~/.local/share/herdr-plugins
git clone https://github.com/baileyrosen3/omarchy-herdr.git ~/.local/share/herdr-plugins/omarchy-herdr
cd ~/.local/share/herdr-plugins/omarchy-herdr
```

### 2. Install inside a Herdr pane

```sh
# Preview the Herdr shortcut changes.
./bin/herdr-shell install

# Link the plugin, add the helper, save shortcuts, and reload Herdr.
./bin/herdr-shell install --apply
```

This creates `~/.local/bin/herdr-shell` and adds two native prefix shortcuts.
Use your Herdr prefix, followed by **Space** for the menu or **Alt+K** for
keybindings. For a `Ctrl+Space` prefix, press **Ctrl+Space**, release it, then
press **Space**.

Keep the checkout in place: Herdr links directly to this directory. Make sure
`~/.local/bin` is on your shell's `PATH` to use the short helper command.

### 3. Enable focused Super keys

From the same checkout:

```sh
# Preview the Hyprland loader change.
./bin/herdr-shell desktop install

# Back up the config, install the bridge, reload, and check for new errors.
./bin/herdr-shell desktop install --apply
```

Focus your Herdr terminal and press **Super+Space**.

The menu is a **Herdr plugin**; the Super-key integration is a **Hyprland Lua
bridge**. The steps above install both parts.

## Shortcuts

### Open and create

| Shortcut | Action inside focused Herdr |
| --- | --- |
| **Super+Space** | Open the control menu |
| **Super+K** | Open keybindings |
| **Super+A** | Open the Omarchy agent picker in a new pane to the right |
| **Super+G** | Open Lazygit in a regular pane to the right |
| **Super+U** | Create a pane **above** |
| **Super+D** | Create a pane **below** |
| **Super+L** | Create a pane to the **left** |
| **Super+R** | Create a pane to the **right** |
| **Super+M** | Zoom the pane to fill the Herdr tab; press again to restore |
| **Super+X** | Close the focused pane **immediately** |

New panes, agents, and Lazygit use the originating pane's directory.
Lazygit and agents open as regular panes in the same tab. Editor and file-browser
actions open popups.

**Super+X closes without confirmation.** The menu's Close pane command presents
a confirmation with Cancel selected first.

### Move through your workspace

| Shortcut | First choice | At a pane edge |
| --- | --- | --- |
| **Super+Left** | Pane to the left | Previous tab |
| **Super+Right** | Pane to the right | Next tab |
| **Super+Up** | Pane above | Previous Herdr workspace |
| **Super+Down** | Pane below | Next Herdr workspace |

Tabs and workspaces wrap at the ends. A destination restores its selected pane.
The tab fallback stays within the current workspace.

### Arrange and resize

| Shortcut | Action |
| --- | --- |
| **Super+Shift+arrows** | Swap the pane with its neighbor |
| **Super+Ctrl+Left / Right** | Switch tabs directly |
| **Super+1…9, 0** | Select existing workspace 1…10 |
| **Super+Shift+1…9, 0** | Move the pane to that existing workspace |
| **Super+Minus / Equal** | Resize left / right |
| **Super+Shift+Minus / Equal** | Resize up / down |

Workspace number shortcuts select existing workspaces. Create new ones from
the menu first.

**Desktop behavior:** Super+F, Super+W, Super+Enter, Super+V, Alt+Tab, and
Super+Tab retain their desktop roles. The bridge preserves the actual desktop
dispatchers for mapped shortcuts when Herdr is not focused.

## The control menu

**Super+Space** brings together seven sections:

| Section | Inside |
| --- | --- |
| **Home** | Frequently used agents, tools, panes, tabs, and settings |
| **All** | Every menu action, alphabetically |
| **Launch** | Agents, Lazygit, editor, and file browser |
| **Navigate** | Pane focus, destination pickers, and waiting agents |
| **Panes** | Split, swap, resize, zoom, and close |
| **Workspaces** | Tabs, workspaces, and moving panes between them |
| **Configure** | Keybindings, appearance, desktop integration, reload, and undo |

Type to search every section. The header shows the originating workspace, tab,
directory, and Super-key status. Wide windows show a details panel; smaller
windows keep the action list compact. **F1** always makes the selected item's
full details available.

The official Herdr ram and Omarchy mark are rendered as terminal art using
your terminal's colors. Logos appear on the main menu at **88 × 32** or larger.
Subpages use a compact header. The minimum usable menu size is **46 × 16**.

<details>
<summary><strong>Menu keyboard reference</strong></summary>

| Key | Action |
| --- | --- |
| Up / Down | Select an item |
| Enter | Run, open, edit, switch, or review the selected item |
| Tab / Shift+Tab, Left / Right | Change sections on the main menu |
| Page Up / Page Down | Scroll lists and documents |
| Home / End | Jump to the beginning / end |
| Esc | Clear search, go back, then close |
| F1 | Selected-item details and help |
| F2 | Keybindings |
| Ctrl+O / F3 | Settings |
| Ctrl+Z / F4 | Review undo for the last managed config change |
| Ctrl+R | Refresh while keeping your place |
| Ctrl+C | Close the menu, or cancel the current dialog |

Search, selection, and scroll position survive editing and refreshing. Returning
from a subpage restores the previous view. This is a keyboard-first interface.

</details>

## Configure with a preview

Choose a setting, review its before/after values and diff, then apply. Herdr
validates the configuration before it is saved and reloaded.

- **On/Off settings** use a simple choice dialog.
- **Fixed-choice settings** show supported options and an inherited-default option.
- **Text and numeric settings** support cursor movement, Home/End, Delete,
  and horizontally scrolling input.
- **Invalid entries** stay in the editor for correction.
- **Ctrl+Z** previews undo for the last managed config edit.

Config transactions preserve comments and unrelated fields. If reload fails,
the previous file is restored unless a concurrent edit makes that unsafe.
Undo preserves unrelated later edits and rejects conflicts on the same field.

### Native keybindings

Open **Super+K**, select a native binding, and enter a shortcut such as
`prefix+f`. Multiple bindings use TOML arrays:

```toml
["prefix+f", "alt+f"]
```

Leave the input blank to disable a binding. **Ctrl+D** restores an inherited
native default. **Ctrl+U** clears an editor field; on the keybindings list it
shows or hides unassigned entries. Search always includes unassigned entries.

Super profile rows are read only in this editor. Toggle the profile through
**Configure → Omarchy controls**, or:

```sh
herdr-shell desktop disable
herdr-shell desktop enable
```

When disabled, Super+Space uses its desktop action. Reopen Herdr Shell with
the native prefix shortcut or `herdr-shell menu`.

## Update

Run inside Herdr, from your checkout:

```sh
cd ~/.local/share/herdr-plugins/omarchy-herdr
git pull --ff-only
./bin/herdr-shell install --apply
./bin/herdr-shell desktop install --apply
```

Reopen the menu to load the updated Python code. Reinstalling refreshes the
linked manifest and generated desktop bridge. Skip the last command if you
use only native Herdr shortcuts.

## CLI

The same action catalog powers the menu and the CLI:

```sh
herdr-shell menu
herdr-shell commands
herdr-shell keybindings list
herdr-shell settings
herdr-shell launch git
herdr-shell action run pane-split-right
herdr-shell config check
herdr-shell doctor
```

<details>
<summary><strong>Configuration, scripting, and explicit targets</strong></summary>

```sh
herdr-shell commands --json
herdr-shell keybindings --json

# Preview a change; add --apply to validate, save, and reload.
herdr-shell keybindings set zoom prefix+f
herdr-shell keybindings set zoom prefix+f --apply
herdr-shell keybindings disable zoom --apply
herdr-shell config set ui.pane_gaps false
herdr-shell config set ui.pane_gaps false --apply

herdr-shell config reload
herdr-shell config undo --dry-run
herdr-shell config undo --yes
herdr-shell theme sync
herdr-shell desktop status
herdr-shell logs
```

Inside Herdr, commands use the calling pane. Outside it, select a local session
and pane explicitly; global options go before the command:

```sh
herdr-shell --session my-session --pane w1:p1 menu
herdr-shell --session my-session --active menu
```

Popups require the originating pane to be active. Actions retain its socket and
terminal identity. Moving or replacing that pane invalidates the saved target.

</details>

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Super+Space still opens the desktop menu | Focus a local Herdr terminal, run `herdr-shell desktop status`, and enable/install the bridge. |
| The helper command is missing | Add `~/.local/bin` to `PATH`, or run `./bin/herdr-shell` from the checkout. |
| `No module named tomlkit` | Install `python-tomlkit` for the system `python3` used by the plugin. |
| Installer cannot find the Omarchy loader | Check that your setup uses `hyprland.lua` with the required default loader; native Herdr menu shortcuts remain available. |
| Shortcut collision during install or editing | Read the reported conflicting actions and choose an unused native chord. The conflicting change is not applied. |
| A tool is marked unavailable | Install the named tool or configure `$VISUAL` / `$EDITOR`; the menu does not install tools. |
| A workspace number does nothing | Create that Herdr workspace first; number shortcuts do not create workspaces. |
| Arrow navigation reaches another tab/space | This is the pane-edge fallback. Use native pane-focus commands when you only want pane movement. |
| An error occurs after the popup closes | Check `herdr-shell logs` and `${XDG_STATE_HOME:-~/.local/state}/herdr-shell/actions.log`. |

### Compatibility boundaries

- The desktop bridge targets a **single identifiable local foreground Herdr
  client** in the focused window. Ambiguous process trees are not routed.
- Direct SSH clients do not have a verified remote target for desktop routing;
  use their native Herdr shortcuts.
- Desktop routing reads the selected local session's focused pane. Independent
  per-client focus in a shared session is outside this preview's guarantees.
- The plugin includes menu commands and a desktop bridge. Den integration,
  saved layouts, and project presets are outside the current release.
- Theme sync selects Herdr's terminal palette. It does not install an automatic
  desktop theme-change hook.

## Uninstall

Remove the desktop bridge while the helper is still available:

```sh
herdr-shell desktop remove
herdr-shell desktop remove --apply
```

Remove the two `[[keys.command]]` entries in your Herdr config whose commands
are `blr.herdr-shell.menu` and `blr.herdr-shell.keybindings`, then reload:

```sh
herdr-shell config reload
herdr plugin unlink blr.herdr-shell
```

Remove `~/.local/bin/herdr-shell` if it still points to this checkout. You can
then remove the checkout. Config history remains under
`${XDG_STATE_HOME:-~/.local/state}/herdr-shell/config/`; desktop backups are in
the adjacent `desktop/` directory.

## Development and feedback

- [UI/UX audit and manual test guide](docs/ui-ux-audit.md)
- [Contributing](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)
- [Report an issue](https://github.com/baileyrosen3/omarchy-herdr/issues)

When reporting a UI issue, include your Herdr version, terminal dimensions,
page, selected row, and the exact key sequence. The manual guide covers menus,
editing, undo, destination pickers, integration state, and small-window behavior.

## License and credits

Plugin code is [MIT licensed](LICENSE). Built for [Herdr](https://herdr.dev/)
and [Omarchy](https://omarchy.org/), maintained as a community integration.

Herdr and Omarchy names and marks belong to their respective owners. Upstream
brand assets are covered by their owners' terms, separately from the plugin's
code license. See [logo sources and attribution](assets/branding/README.md).
