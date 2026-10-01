local native_open = io.open
local enabled, files, writes = "1", {}, 0
io.open = function(path, mode)
  if mode == "a" then
    return {
      write = function(_, value) files[path] = (files[path] or "") .. value; writes = writes + 1 end,
      flush = function() end,
      close = function() end,
    }
  end
  local value = files[path]
  if path:match("desktop%-enabled$") then value = enabled end
  if path:match("plugin%-root$") then value = "/tmp/plugin's path" end
  if value then return {read = function() return value end, close = function() end} end
  return native_open(path, mode)
end

local bound, bind_count, commands, timers, forwarded = {}, 0, {}, {}, {}
local active = {pid = 100, address = "0x123"}
local function native_bind(keys, dispatcher, opts)
  bind_count = bind_count + 1
  local binding = {dispatcher, opts}
  binding.unbind = function(self)
    self.removed = true
    if bound[keys] == self then bound[keys] = nil end
  end
  bound[keys] = binding
  return binding
end
hl = {
  bind = native_bind,
  unbind = function() error("only the managed handle may be unbound") end,
  get_active_window = function() return active end,
  exec_cmd = function(value) commands[#commands + 1] = value end,
  timer = function(callback, opts) timers[#timers + 1] = {callback, opts} end,
  dsp = {send_key_state = function(value) return value end},
  dispatch = function(value) forwarded[#forwarded + 1] = value end,
}
local fullscreen = function() return "desktop fullscreen" end
local personal = function() return "personal tab shortcut" end
hl.bind("SUPER + M", fullscreen, {repeatable = true})
hl.bind("SUPER + ALT + T", personal, {repeatable = true})
local original_fullscreen, original_personal = bound["SUPER + M"], bound["SUPER + ALT + T"]

dofile(arg[1])
local bridge = herdr_shell_bridge
assert(hl.bind == native_bind, "bridge must not replace hl.bind")
assert(bound["SUPER + M"] == original_fullscreen and bound["SUPER + ALT + T"] == original_personal,
  "loading must preserve desktop bindings")
assert(bind_count == 2, "registration must wait until compositor inspection")
assert(#timers == 1 and timers[1][2].type == "oneshot")
timers[1][1]()
assert(commands[1]:match("desktop reconcile %-%-generation"), "reload must schedule reconciliation")
assert(commands[1]:find("plugin'\\''s path", 1, true), "helper path must be shell quoted")
commands = {}

bridge.register({["pane-zoom"] = true, menu = true, ["tab-previous"] = true, nonexistent = true})
assert(bind_count == 5, "only explicitly accepted known actions may bind")
assert(bound["SUPER + ALT + T"] == original_personal, "a refused profile chord must preserve its owner")
assert(bound["SUPER + M"] == original_fullscreen)
local zoom = bound["SUPER + ALT + Z"]
local menu = bound["SUPER + ALT + M"]
assert(zoom and menu and bound["SUPER + ALT + Page_Up"], "generated named-key aliases must register")
assert(zoom[2].repeating == false and zoom[2].submap == nil, "bind options must use the documented nonrepeat field")
bridge.register({["pane-zoom"] = true, menu = true})
assert(bind_count == 5, "reconciliation must not duplicate a registered action")

local function expect_forward(callback, key, mods)
  local before, queued, helpers, timer_count = #forwarded, writes, #commands, #timers
  callback()
  assert(#forwarded == before + 1 and forwarded[before + 1].state == "down", "passthrough must press the original key")
  local down = forwarded[before + 1]
  assert(down.key == key and down.mods:gsub("[%s+]", "") == (mods or "SUPERALT"), "passthrough must preserve key and modifiers")
  assert(down.window == nil, "passthrough must target the focused surface")
  assert(#timers == timer_count + 1 and timers[#timers][2].type == "oneshot" and timers[#timers][2].timeout == 50,
    "passthrough must release the synthetic key through the documented timer")
  timers[#timers][1]()
  local up = forwarded[#forwarded]
  assert(#forwarded == before + 2 and up.state == "up" and up.mods == down.mods and up.key == down.key,
    "passthrough must release the same chord")
  assert(writes == queued and #commands == helpers, "passthrough must not queue or execute a Herdr action")
end
bridge.candidates = function() return {} end
expect_forward(zoom[1], "Z")
for _, clients in ipairs({false, {{pid = 101}, {pid = 102}}}) do
  local before = #forwarded
  bridge.candidates = function() return clients or nil end
  zoom[1]()
  assert(writes == 0 and #commands == 0, "ordinary or ambiguous terminals must not queue actions")
  assert(#forwarded == before, "ambiguous or unreadable terminals must not synthesize keys")
end
bridge.candidates = function() return {{pid = 101, start = "500"}} end
zoom[1](); menu[1]()
local queue
for path, value in pairs(files) do if path:match("events%-.*%.queue$") then queue = value end end
assert(queue == "1\tpane-zoom\t100\t101\t500\t0x123\n2\tmenu\t100\t101\t500\t0x123\n",
  "presses must be queued synchronously in physical press order")
assert(#commands == 2 and commands[1]:match("desktop drain %-%-generation"))
enabled = "0"; expect_forward(zoom[1], "Z")
assert(writes == 2 and #commands == 2, "disabled profile must not queue")
enabled = "1"; active = nil; expect_forward(zoom[1], "Z")
assert(writes == 2 and #commands == 2, "a missing focused window must not queue")
active = {pid = 100, address = "0x123"}
bridge.register({menu = true, ["pane-zoom"] = true, ["workspace-previous"] = true})
bridge.candidates = function() return {} end
expect_forward(bound["SUPER + ALT + SHIFT + Page_Up"][1], "Page_Up", "SUPERALTSHIFT")

-- A personal bind appearing without a reload must survive reconciliation.
hl.bind("SUPER + ALT + Z", personal, {repeating = true})
local late_owner = bound["SUPER + ALT + Z"]
bridge.register({menu = true})
assert(zoom.removed and bridge.registered["pane-zoom"] == nil, "new conflicts must remove the managed handle")
assert(bound["SUPER + ALT + Z"] == late_owner, "removing the managed handle must preserve the later desktop owner")
assert(bound["SUPER + M"] == original_fullscreen and bound["SUPER + ALT + T"] == original_personal)

-- Restore the real parser and check the same command-line cases as Python.
dofile(arg[1])
bridge = herdr_shell_bridge
local fixtures = dofile(arg[2])
for _, case in ipairs(fixtures) do
  local parsed = bridge.client_options(case.argv)
  assert((parsed ~= nil) == case.accepted, "Python/Lua CLI acceptance differs for " .. table.concat(case.argv, " "))
  if parsed then
    assert(parsed.remote == case.remote, "Python/Lua remote detection differs")
    assert(parsed.session == case.session, "Python/Lua session targeting differs")
  end
end
local function process(pid, argv, children, foreground, state)
  local fields = {}; for i = 1, 50 do fields[i] = "0" end
  fields[1], fields[2], fields[3], fields[5], fields[6], fields[20] = state or "S", "1", tostring(pid), "9", tostring(foreground and pid or 1), "500"
  local base = "/proc/" .. pid
  files[base .. "/stat"] = pid .. " (name ) parentheses) " .. table.concat(fields, " ")
  files[base .. "/cmdline"] = table.concat(argv, "\0") .. "\0"
  files[base .. "/task/" .. pid .. "/children"] = children or ""
end
process(100, {"foot"}, "101", false)
process(101, {"/usr/bin/herdr", "--session", "demo"}, "102", true)
process(102, {"herdr"}, "", true)
local clients = bridge.candidates(100)
assert(#clients == 1 and clients[1].pid == 101 and clients[1].start == "500", "nested pane processes must not become clients")
process(101, {"herdr", "--remote", "host"}, "102", true)
assert(#bridge.candidates(100) == 0, "remote client must suppress local descendants")
process(101, {"herdr"}, "102", true, "T")
assert(#bridge.candidates(100) == 0, "suspended client must suppress descendants")
process(101, {"bash"}, "", true)
assert(#bridge.candidates(100) == 0, "plain terminal must not target Herdr")
process(100, {"foot"}, "101 103", false)
process(101, {"herdr"}, "", true)
process(103, {"herdr"}, "", true)
assert(#bridge.candidates(100) == 2, "shared terminal process must remain ambiguous")

-- A new bridge can restore a previously installed legacy bind wrapper safely.
herdr_shell_bridge = {original_bind = native_bind}
hl.bind = function() error("legacy wrapper was not removed") end
dofile(arg[1])
assert(hl.bind == native_bind)
print("Explicit registration, desktop preservation, ordered queue, parser parity and process checks passed")
