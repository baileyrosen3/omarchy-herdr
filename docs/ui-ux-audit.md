# Herdr × Omarchy UI/UX audit

Date: 2026-09-15.

Reviewed the supplied screenshot and the source for every control-menu section,
search, keybindings, settings, destination pickers, config previews, undo,
desktop setup/toggling, action dispatch, and close confirmation.

## Fixed findings

| Priority | Finding | Change |
| --- | --- | --- |
| High | A window showing only the resize message still accepted action keys. | The menu and dialogs now block actions below 46 × 16. Esc remains available. |
| High | Refresh, save, and leaving subpages reset search and selection. | Preserve the selected item, query, and scroll on reload; keep page history and restore the previous view. |
| High | Invalid edits discarded the entered value. | Keep the entry through parsing, collision, and native-validation errors; show the full error in a scrollable dialog before returning to the editor. |
| High | Long config diff lines and errors were silently cut off. | Wrap by terminal cells, show before/after summaries, and support paging plus Home/End. |
| Medium | `""` bindings appeared assigned because the list containing an empty string was truthy. | Remove blank chords before display/filtering. Search includes unassigned entries even when they are hidden in the unfiltered list. |
| Medium | Default theme displayed as `terminal` even though the installed Herdr default is `catppuccin`. | Correct the displayed fallback using the installed default-config reference. |
| Medium | The setting editor required TOML for simple On/Off and fixed-choice values. | Add labeled choice dialogs and an inherited-default option. Keep text entry for free-form settings. |
| Medium | Text editing could only append/backspace, and long values hid the cursor. | Add cursor movement, Home/End, Delete, word deletion, clear, and horizontal input scrolling. |
| Medium | Super profile shortcuts looked editable like native shortcuts. | Mark profile rows explicitly, explain active/inactive scope, and open read-only information instead of an editor. |
| Medium | Both the section rail and action row looked like keyboard focus. | Use an accent marker for the active section and reserve the filled highlight for the selected action. |
| Medium | Generic “Enter Open” and “Esc Back” labels misrepresented what those keys did. | Show Run/Edit/Review/Switch/Move/Info and Clear/Back/Close according to state. |
| Medium | Picker “Current” labels followed snapshot focus instead of the menu's originating target. | Compare destination IDs with the captured origin. Pane labels also include workspace/tab context. |
| Medium | Close confirmation was an unstructured text prompt. | Use the shared dialog with the pane ID, directory, and Cancel selected first. Super+X retains immediate close. |
| Medium | Applying, refreshing, and reloading offered little feedback. | Show in-progress and completion messages. Reload configuration now keeps the menu open. |
| Visual | Omarchy's solid mark dominated Herdr's dotted ram. | Convert both official source logos to the same braille texture. |
| Visual | Large branding and repeated directory details consumed editing space. | Keep logos on the main menu; use compact headers on subpages; remove the repeated directory panel. |
| Visual | Long lists retained double spacing even when it forced unnecessary scrolling. | Use single spacing when the entries do not fit with gaps. Add above/below indicators. |

The appearance adjustments are design judgments informed by the screenshot.
The behavioral findings above are grounded in the source; their fixes still
need the hands-on checks below.

## Validation and limits

- Parsed every plugin Python module for syntax errors.
- Inspected the changes and their call sites, including deferred action dispatch.
- Read the installed `herdr --default-config` to verify the settings choices.
- Ran `21st review`; it reported **zero files reviewed**, because this is a
  Python/curses UI. Its zero-findings output is **not** a passing UI audit.
- Per your request, did not run automated test suites or live desktop/keyboard
  tests. No live configuration was edited during this audit.
- The UI remains keyboard-first. This audit does not claim mouse support,
  screen-reader verification, or live checks across terminal emulators/themes.
- Existing historical captures and `live-checks.json` predate these changes.

## Manual test guide

After installing, close any old menu and reopen **Super+Space**; the installed
plugin is linked to your checkout. Use a spare Herdr workspace/pane for commands that
create, move, or close terminals.

### 1. Main menu and all sections

1. Open Super+Space in a large terminal. Check both logos, the originating
   workspace/tab/directory, and Super-key status.
2. Cycle Home, All, Launch, Navigate, Panes, Workspaces, and Configure with Tab;
   reverse with Shift+Tab or Left. Only the action row should be filled.
3. In All, use Up/Down, Page Up/Down, Home, and End. Long lists should use
   compact spacing and show an arrow when more rows exist above/below.
4. Select an item and press F1. Its full label, shortcut, description, and
   directory should be available, followed by the menu help. Esc returns.

### 2. Search and returning to your place

1. From Home, type `swap` to find actions outside Home. Esc restores the
   pre-search selection.
2. Type a nonsense query; check the empty message. Esc clears it.
3. Search for `keybindings` and open it. Esc returns to the same search and
   selected action. Another Esc clears search.
4. In a long list, select a lower item and press Ctrl+R. Your query, selected
   item, and scroll position should stay in place.

### 3. Keybindings

1. Press F2. Open a `(profile)` row: it should show read-only information.
2. Ctrl+U should show/hide unassigned native bindings. Search for an unassigned
   action; it should still be found while the unfiltered list hides it.
3. Open a native key. Edit using Left/Right, Home/End, Delete, Backspace,
   Ctrl+W, and Ctrl+U. Long values should scroll with the cursor.
4. Enter a malformed TOML array such as `[`. Dismiss the error: the `[` should
   remain in the editor. Correct it or cancel with Esc.
5. Try another native action's already-assigned chord to inspect a collision
   error. It should not save, and your input should remain available.
6. On a spare binding, leave the value blank, review it, then press Esc to
   return to editing. Esc again cancels. No change should be written.

### 4. Settings, preview, save, and undo

1. Press Ctrl+O. Open “Gaps between panes”: choose On/Off using the arrows.
2. Enter opens a summary and diff. Esc returns to the choice dialog without
   saving. Esc again returns to Settings with the same row selected.
3. Choose the opposite of your current value, review, and apply. Check the
   completion message and that your selection/query remain intact.
4. Press Ctrl+Z, review the undo, and apply. The previous value should return.
5. Open Tab bar position and Notification delivery. Check labeled options
   and the inherited-default option.
6. Open Sidebar width, enter `abc`, and review. The error should explain the
   invalid entry; dismissing it should return to `abc` for correction.
7. Open Theme or a native binding and press Ctrl+D. Inspect the proposed
   removal of the override, then Esc back and cancel if you want to keep it.

### 5. Pickers and commands

1. Open workspace, tab, and pane pickers. Confirm the “Current” marker matches
   the pane that opened the menu. Pane rows should identify workspace/tab.
2. Search a picker, open F1 for full details, then cancel back to the same
   menu location. Switching should happen after the popup closes.
3. In a spare pane, launch Lazygit and an agent: each should open a regular
   pane to the right in the originating tab/directory. Editor/files remain
   popup tools.
4. Check split, focus, swap, resize, zoom, and workspace movement from their
   sections. The menu should keep targeting its originating pane.
5. Open Close pane from the menu. Enter with Cancel selected must keep the
   pane. Selecting Close explicitly should close it after the menu exits.
6. Super+X should still close a spare pane immediately without a dialog.

### 6. Integration state

1. Toggle Omarchy controls Off from Configure. The status and completion
   message should change; menu actions should show native shortcuts where
   available. The Super profile should explain that Herdr routing is off.
2. Toggle On again **before closing this menu**. Super+Space uses the desktop
   action while the integration is off; the native prefix menu shortcut is
   also available to reopen Herdr Shell.
3. Choose Reload configuration. The menu should remain open with a completion
   message, preserving your selection.

### 7. Window sizes and errors

1. Resize through roughly 142 × 48, 100 × 32, 80 × 24, and 46 × 16. The rail,
   details, branding, and footer should adapt without overlapping controls.
2. Shrink below 46 × 16, then press Enter and Ctrl+Z. No action should run.
   Resize larger to resume, or Esc to close.
3. Resize while entering a value or reading a long preview. Input should
   survive; wrapped text should remain scrollable.
4. On a missing-tool entry, Enter should explain the missing dependency.
   A long error must be readable by scrolling, without modifying configuration.

If something looks wrong, report the page, terminal dimensions, selected row,
and key sequence; these identify the layout or state transition to inspect.
