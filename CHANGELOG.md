# Changelog

## 0.2.0 — 2026-10-14

- Preserve manually named tabs, including directory names on first run and names
  changed during a naming pass; no longer adopt earlier directory labels.
- Require successful privacy markers before task-derived tab renames and renew
  those markers while the daemon runs.
- Clean up removed Omarchy plugins promptly even when checkout deletion races a
  runtime snapshot, without delaying removal behind the update error retry.
- Package a real Omarchy service plugin for standard add/update/remove commands.
  Manage the Herdr plugin and shortcut bridge through a private runtime copy,
  preserve developer-linked installations, and keep cleanup available after
  Omarchy removes its managed checkout.
- Name tabs after the task of the agent running in them, live. One namer per
  Herdr server follows agent session titles (or herdr-agent-usage chat titles),
  never replaces manual or other tools' tab names, and adds **Name tab from agent
  task** to hand a tab back. New workspaces take the Git project name, then the
  branch or a suffix for duplicates, and never change; panes stay unlabeled. Agent
  panes in task-named tabs carry a `herdr_shell_tab_label` token for dashboards.
- Replace desktop shortcut overrides with dedicated, focused Super+Alt commands.
  Refuse conflicts instead of replacing Omarchy or personal bindings, and report
  the actual installed and active integration state.
- Install free shortcuts even when another plugin owns a chord. Leave occupied
  native recovery defaults unbound, preserve custom keys, and retry desktop
  registration across reloads or late conflicts. Recheck bindings on package
  updates, including Python-only changes, while preserving a disabled profile.
- Require Herdr 0.9.3+ for verified pane swapping and layout operations.
- Toggle the control menu with Super+Alt+M. Add direct splits, swaps, nearest-split
  rotation, zoom, pane cycling, tab/workspace navigation, and agent/tool launchers.
- Keep native Ctrl+Alt directional pane focus strict: reaching a pane edge does
  not switch tabs or workspaces. Preserve native Ctrl+Alt+Shift resizing and the
  Super+Ctrl+Enter Herdr launcher.
- Cycle agents in a stable priority sweep: blocked, done, working, then idle.
  Super+Alt+Q advances and Shift+Q reverses it.
- Make next/previous pane continue through tabs and workspaces, and next/previous
  tab continue through workspaces, wrapping around the session in both directions.
  Preserve tab-selected panes, local directional focus, and bounded practice navigation.
- Close idle shells directly; confirm running or unknown work before closing a
  pane, tab, or workspace, using the same policy for menu and shortcut actions.
- Add a persisted **Confirm pane close** settings toggle, enabled by default.
  Turning it off skips pane prompts for menu and shortcut closes, while retaining
  tab/workspace confirmations, target identity checks, and safe practice closes.
- Add native-gated numeric pane gaps and tab-edge padding to Settings, with
  2-column/1-row gaps and 1-column/1-row edge margins (half-width horizontally,
  with one-row defaults vertically). Preview, live
  reload, zero sizes, inherited-default reset, and field-level undo use the
  existing config transactions.
- Ship a reproducible, pinned native spacing patch and opt-in user-owned build;
  keep the packaged binary untouched and hide ignored fields on stock Herdr.
- Display spacing values as plain numbers, with units only in the control labels.
  Make zero gaps use a single shared divider per axis instead of adjacent double
  borders, with matching border joins, focus colors, and mouse hit areas. Keep
  positive gaps and tab-edge padding independent and equal to their blank-cell counts.
- Update shortcut hints, installation instructions, the banner, and manual checks
  to describe the dedicated profile.
- Make Home use the same branded header, section rail, grouped action list, and
  responsive details layout as the other main sections. Retain all 26 direct
  shortcuts, full chords, live availability, and selection across resizing.
- Organize actions into Home, Panes, Tabs, Workspaces, Agents, Tools, Settings,
  Learn, and All. Group pane commands by creation, navigation, and resize/close;
  All uses the same purpose-based ordering.
- Split Settings into Layout, Appearance, Behavior, Notifications, and Controls.
  Search across all categories; clearing search restores its original category
  and value. Preserve categories through edits, refresh, and subpage navigation.
- Keep the read-only Walkthrough and add a Hands-on walkthrough and timed Speed Run.
  First welcome offers both walkthroughs; completing either persistently unlocks
  all three activities plus Exit welcome. All remain accessible from the menu/CLI.
  Automatically prepare and advance all 26 verified shortcut missions after one
  start. Hands-on shows each chord and offers a clickable demonstration
  and replay; Speed Run shuffles goals and rewards reaction speed and accurate
  streaks against a 120-second active clock, excluding fixture/action processing.
  Keep the guide outside disposable close targets, bound workspace
  jumps to practice fixtures, use harmless practice agents and a temporary Git
  repository, and open the real menu in a browsing-only practice view. Stop owned
  simulators and clear their agent reports on exit while preserving practice panes.
- Add previewable update and removal scripts. Preserve disabled desktop profiles,
  personal configuration, backups, and practice workspaces.
- Preserve split orientation, terminal identities, running processes, and zoom
  when rotating a zoomed pane.

## 0.1.0 — Initial public preview

- Focused Omarchy Super-key routing for local Herdr terminals.
- Directional pane creation and pane-first navigation with tab/workspace fallback.
- Agent and Lazygit launchers using the originating pane's directory and tab.
- Searchable control menu with Home, All, Launch, Navigate, Panes, Workspaces,
  and Configure sections.
- Native keybinding editing, conflict detection, and inherited defaults.
- Settings choices, config previews, native validation, reload, and field-level undo.
- Shared dialogs with input editing, complete error messages, and scrollable diffs.
- Preserved search, selection, scroll position, and page history.
- Official upstream logo shapes converted into bundled terminal art.
- CLI commands, reversible desktop setup, and manual verification documentation.

Developed against Herdr 0.9.3 and Omarchy's Lua Hyprland configuration. Use the
manual guide to verify desktop routing and interaction on your terminal.
