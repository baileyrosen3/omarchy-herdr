# Changelog

## Unreleased

- Package a real Omarchy service plugin for standard add/update/remove commands.
  Manage the Herdr plugin and shortcut bridge through a private runtime copy,
  preserve developer-linked installations, and keep cleanup available after
  Omarchy removes its managed checkout.
- Replace desktop shortcut overrides with dedicated, focused Super+Alt commands.
  Refuse conflicts instead of replacing Omarchy or personal bindings, and report
  the actual installed and active integration state.
- Require Herdr 0.9.3+ for verified pane swapping and layout operations.
- Toggle the control menu with Super+Alt+M. Add direct splits, swaps, nearest-split
  rotation, zoom, pane cycling, tab/workspace navigation, and agent/tool launchers.
- Keep native Ctrl+Alt directional pane focus strict: reaching a pane edge does
  not switch tabs or workspaces. Preserve native Ctrl+Alt+Shift resizing and the
  Super+Ctrl+Enter Herdr launcher.
- Cycle agents in a stable priority sweep: blocked, done, working, then idle.
  Super+Alt+Q advances and Shift+Q reverses it.
- Close idle shells directly; confirm running or unknown work before closing a
  pane, tab, or workspace, using the same policy for menu and shortcut actions.
- Update shortcut hints, installation instructions, the banner, and manual checks
  to describe the dedicated profile.
- Simplify Home into a grouped quick reference for all 26 direct shortcuts,
  with explicit Shift combinations and live availability instead of repeated
  action descriptions.
- Offer an optional first-menu walkthrough or Herdr Key Quest learning game.
  Practice all 26 real Super+Alt shortcuts through live missions and verified
  Herdr results. Keep the guide outside disposable close targets, bound workspace
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
