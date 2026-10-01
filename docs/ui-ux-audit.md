# Herdr × Omarchy UI/UX audit

Updated: 2026-10-01. Original menu review: 2026-09-15.

Reviewed the control-menu structure, search, keybindings, settings, destination
pickers, previews, undo, desktop integration, and action dispatch. The shortcut
audit also compared the configured desktop with 233 live Hyprland bindings.
The previous 52-chord desktop profile overlapped 47 existing bindings; the
replacement uses dedicated Super+Alt commands and refuses occupied chords.

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
| Medium | Profile shortcuts looked editable like native shortcuts. | Mark Super+Alt rows explicitly, explain the actual integration state, and open read-only information instead of an editor. |
| Medium | Both the section rail and action row looked like keyboard focus. | Use an accent marker for the active section and reserve the filled highlight for the selected action. |
| Medium | Generic “Enter Open” and “Esc Back” labels misrepresented what those keys did. | Show Run/Edit/Review/Switch/Move/Info and Clear/Back/Close according to state. |
| Medium | Picker “Current” labels followed snapshot focus instead of the menu's originating target. | Compare destination IDs with the captured origin. Pane labels also include workspace/tab context. |
| High | Shortcut close bypassed confirmation for running work. | Use one close policy for the menu and shortcuts: idle shells close directly; running or unknown work prompts with Cancel selected first. Check all panes before closing a tab or workspace. |
| Medium | Applying, refreshing, and reloading offered little feedback. | Show in-progress and completion messages. Reload configuration now keeps the menu open. |
| Visual | Omarchy's solid mark dominated Herdr's dotted ram. | Convert both official source logos to the same braille texture. |
| Visual | Large branding and repeated directory details consumed editing space. | Use compact headers on Home and subpages; keep logos on other main-menu sections; remove the repeated directory panel. |
| Visual | Long lists retained double spacing even when it forced unnecessary scrolling. | Use single spacing when the entries do not fit with gaps. Add above/below indicators. |
| Visual | Home showed only a subset of direct shortcuts and repeated long descriptions. | Show all 26 added shortcuts in a compact grouped sheet. State the shared Super+Alt modifiers once, keep Shift explicit, and leave full explanations in F1 help. |
| High | The learning game asked for Enter before every mission, showed every answer, and awarded fixed points unrelated to speed. | Retain the read-only Walkthrough, add untimed Hands-on demonstrations and shuffled Speed Run goals; auto-prepare and advance targets, count active reaction time, and reward speed and accuracy. |

The appearance adjustments are design judgments informed by the original
screenshot. The shortcut audit is grounded in configured bindings and the live
Hyprland snapshot. The checks below cover interaction on a working desktop.

## Validation and limits

- Configured Lua and live bindings were checked together. Physical number,
  minus/equal, and bracket positions must be checked from source when a runtime
  snapshot does not populate their keycode fields.
- Omarchy already uses Super+Alt arrows, Enter, Tab, Space, G, K, and other
  chords. The dedicated profile avoids them; installation checks the current
  desktop again, including personal bindings.
- The old prefix menu shortcuts remain available for recovery. No plain Alt
  chords are added by this profile.
- Automated checks and isolated probes belong in the implementation validation
  report. This guide does not stand in for a live desktop interaction check.
- The menu remains keyboard-first. The hands-on walkthrough additionally
  offers a clickable Demo; verify terminal mouse events on the user's emulator.
  This audit does not claim screen-reader verification across emulators and themes.
- Historical captures predate the dedicated shortcut profile.

The opt-in desktop probe (`python3 tests/desktop_probe.py`) needs `foot`, Herdr,
a running Hyprland session with all 26 shortcuts active, and permitted write
access to the existing `/dev/uinput` device, such as existing input-group or
device permissions. Its Python standard-library fixture creates a QA keyboard
named `herdr-shell-qa-<probe PID>`, guards every keypress against its own
disposable foot window, releases held keys and destroys the device on exit,
and restores the pointer position and original window focus if that window still
exists. It floats only its owned window and puts the pointer inside it for
predictable mouse-following focus. It uses an isolated Herdr server under `/tmp`;
it adds no service or persistent desktop configuration. Without device access,
perform the manual checks and leave automated desktop input validation marked
unverified. A successful registry check or terminal key injection alone does
not prove that a compositor shortcut reaches the bridge.

The full probe includes A, V, Q, and Shift+Q; `--launches-only` runs just those
four commands after input sanity checks. A uses the normal agent entrypoint
with a harmless argument-recording Omarchy stub on the disposable server's
PATH. V runs Lazygit in a temporary Git repository, and Q/Shift+Q use agent
reports belonging only to that server. The test avoids configured user agents
and working projects.

## Manual test guide

After installing, close any old menu and reopen **Super+Alt+M**; the installed
plugin is linked to your checkout. Use a spare Herdr workspace/pane for commands that
create, move, or close terminals.

### 1. Main menu and all sections

1. Open Super+Alt+M. Home should show all 26 added shortcuts in groups, with
   a shared Super+Alt instruction and explicit Shift combinations. Check the
   originating workspace/tab and actual integration status. Home should have
   no repeated description lines or large logos.
2. Cycle Home, All, Launch, Navigate, Panes, Workspaces, and Configure with Tab;
   reverse with Shift+Tab or Left. Only the action row should be filled.
3. In All, use Up/Down, Page Up/Down, Home, and End. Long lists should use
   compact spacing and show an arrow when more rows exist above/below.
4. Select an item and press F1. Its full label, shortcut, description, and
   directory should be available, followed by the menu help. Esc returns.
5. Repeat Home at 46 columns and in a short terminal. Every shortcut suffix,
   including Shift+Page Up/Down, should remain readable. Scroll to the last
   action; group headings must never become selectable actions. Turn the
   profile off or reserve one chord and check that Home retains its key with
   a visible inactive/reserved status.

### Optional learning

1. After installation or the first menu launch, check the initial welcome offers
   Walkthrough and Hands-on walkthrough, with Esc to close. Speed Run should only
   appear in this welcome after finishing either walkthrough, followed by Exit
   welcome. Exit closes the panel; it must not open the menu. The next ordinary
   menu launch should not repeat the welcome automatically.
2. Search `learn` to reopen the welcome choice or start any activity directly.
   All three activities must already be accessible from menu/CLI before completion.
   `herdr-shell learn walkthrough` should retain the read-only feature guide,
   with Back/Next and scrolling, without creating practice fixtures. Completing it
   should save completion without overwriting the welcome preference or other state.
3. Start the hands-on walkthrough. Its practice workspace should be separate
   from the originating workspace. Space starts once; targets should prepare and
   advance automatically while keeping the guide unzoomed. The full chord and
   explanation should remain visible. Press each shortcut or click Demo to see
   the real action (F4 also demonstrates); F2 should replay it in a fresh safe
   target. Demo should work without active desktop bindings. There is no timer
   or score penalty. Typing a shortcut's ordinary letter must not perform it.
4. Practice splits, swaps, rotation, zoom/restore, pane cycles, tab/workspace
   creation, closes, and navigation. Close targets must be disposable idle shells;
   new work in a target must refuse the close. Workspace jumps must stay inside
   the prepared practice fixtures. The guide should return after the result is
   visible, including when a close removes its target.
5. A should create a labeled harmless agent simulator. Q and Shift+Q should each
   visit all four simulated priority groups. V should open Lazygit in a temporary
   repository, or a clearly labeled harmless simulator if Lazygit is unavailable.
   M should open the real menu in a browsing-only view and M again should close
   it. Practice menu entries must not apply settings or launch ordinary actions.
   Hands-on F4 should demonstrate closing that popup without the desktop bridge.
   Esc dismissing it early should return to the guide with F2/F3 recovery.
6. Start Speed Run. Goals should be shuffled with answers hidden until F1.
   The 120-second clock should run only while an armed task can receive input,
   excluding preparation, execution, verification, pause, and unusable resizing.
   Faster accurate presses should increase score and streaks; wrong chords should
   cause feedback and a penalty without mutating any target. Busy presses and
   inactive shortcuts must not become accuracy errors. Click Demo must not exist
   in this mode. Zoom/restore, both agent cycles, and menu open/close should count
   their real required presses. Check time expiry and end-of-deck results.
7. From the guide, Space pauses/resumes; F1 explains or reveals a hint and pauses
   timing; F2 retries a blocked target or replays a hands-on lesson; F3 skips. The next task should not
   require Enter. Shrink below 38 × 16 and verify clock/input suspension.
   Esc should expire the input broker while retaining practice spaces.
   Unchanged simulators and their agent reports should disappear; unrelated or
   newly started work must remain intact.
   The originating workspace and desktop shortcuts must remain unchanged; no
   additional desktop chord should be registered.
8. Play again or switch live activities from the completion choices. A fresh
   guide/workspace should open; previous practice spaces should remain, their
   unchanged simulators stopped, and the new input record owned by the new guide.

### 2. Search and returning to your place

1. From Home, type `swap` to search every section. Esc restores the
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
5. In an idle shell, Close pane should close directly. Repeat with a harmless
   foreground task such as `sleep 300`: a confirmation must appear. Cancel
   keeps the pane and its work running; an explicit Close applies afterward.
6. Super+Alt+X uses the same policy. Repeat tab/workspace close with one busy
   pane among idle shells: the whole scope must wait for confirmation.
7. If the foreground process state cannot be verified, require confirmation.
   Reaching a pane edge with Ctrl+Alt+arrows must leave the tab/workspace intact.

### 6. Integration state

1. Toggle Omarchy controls Off from Configure. The status and completion
   message should change; shortcut hints must describe their active state.
   The profile should explain that focused Super+Alt commands are disabled.
2. Toggle On again **before closing this menu**. When disabled,
   Super+Alt+M passes through to the application; use the native prefix menu
   shortcut or `herdr-shell menu` to reopen it.
3. Choose Reload configuration. The menu should remain open with a completion
   message, preserving your selection.
4. Verify the status checks the installed integration and registered bindings,
   rather than displaying On solely because an enabled flag exists.
5. In an isolated desktop fixture, assign an existing chord to another action.
   Installation must report the conflict, mark that Herdr chord inactive, and
   leave the existing binding intact while registering the remaining free chords.

### 7. Window sizes and errors

1. Resize through roughly 142 × 48, 100 × 32, 80 × 24, and 46 × 16. The rail,
   details, branding, and footer should adapt without overlapping controls.
2. Shrink below 46 × 16, then press Enter and Ctrl+Z. No action should run.
   Resize larger to resume, or Esc to close.
3. Resize while entering a value or reading a long preview. Input should
   survive; wrapped text should remain scrollable.
4. On a missing-tool entry, Enter should explain the missing dependency.
   A long error must be readable by scrolling, without modifying configuration.

### 8. Dedicated focused shortcuts

After updating, run `./bin/herdr-shell desktop install --apply` from the plugin
checkout to refresh the generated bridge. Close any old menu before checking
its new shortcut hints. Use a spare tab/workspace for close operations.

Start from a normal Herdr terminal pane in a recognizable directory.

| Shortcut | Expected result |
| --- | --- |
| Super+Alt+M | Open the existing control menu; the same chord closes it. |
| Super+Alt+U / D / L / R | New pane above / below / left / right in the originating directory. |
| Super+Alt+Shift+U / D / L / R | Swap the focused pane with the neighbor in that direction. |
| Super+Alt+J | Rotate the nearest two-leaf split, preserving the remaining layout and focused pane. |
| Super+Alt+Z | Zoom the focused pane, then restore it on a second press. |
| Super+Alt+X | Close an idle shell directly; confirm running or unknown work. |
| Super+Alt+T / W | Create / close a tab in the current workspace. |
| Super+Alt+Shift+T / W | Create / close a Herdr workspace. |
| Super+Alt+Page Up / Page Down | Previous / next tab, without depending on pane geometry. |
| Super+Alt+Shift+Page Up / Page Down | Previous / next Herdr workspace. |
| Super+Alt+P / Shift+P | Next / previous pane in the current tab, wrapping at the ends. |
| Super+Alt+A | New Omarchy agent pane to the right in the originating tab/directory. |
| Super+Alt+V | Lazygit opens in a regular pane to the right in the originating directory. |
| Super+Alt+Q / Shift+Q | Next / previous agent in a stable blocked → done → working → idle sweep. |
| Ctrl+Alt+arrows | Focus a neighboring pane; stay in place at an edge. |
| Ctrl+Alt+Shift+arrows | Resize the focused pane using the existing native bindings. |

Then verify:

1. Reopen Super+Alt+M. Home, All/search, details, and the Super+Alt profile
   display the same chords. Workspace/tab/pane pickers, editor/files, settings,
   and keybindings remain available from the menu.
2. Try rotation beside a nested sibling group. It must explain the limitation
   before mutation, retaining pane identities, processes, ratios, and focus.
3. Change an agent's status while sweeping. The sweep must retain its current
   order instead of re-ranking on every keypress and trapping focus on the same
   agent. Reverse direction and test wrapping; repeat when no agents exist.
4. Focus an application outside Herdr. The profile chords pass through. Existing
   Omarchy actions, including Super+Space, Super+Alt+arrows, Super+Alt+G, and
   Super+Ctrl+Enter, keep their configured meanings inside and outside Herdr.
5. Toggle Omarchy controls Off and check passthrough again, then turn the
   profile back On using the native prefix menu shortcut.
6. If an action fails or the target client becomes ambiguous, keep the outer
   desktop window intact and report the error. A close action must never fall
   back to closing the whole terminal window.

If something looks wrong, report the page, terminal dimensions, selected row,
and key sequence; these identify the layout or state transition to inspect.
