local native_open = io.open
local enabled, files = "1", {}
io.open = function(path, mode)
  local value = files[path]
  if path:match("desktop%-enabled$") then value = enabled end
  if path:match("plugin%-root$") then value = "/tmp/plugin's path" end
  if value then return {read = function() return value end, close = function() end} end
  return native_open(path, mode)
end
local bound, dispatched, command, submap = {}, nil, nil, ""
local active = {pid = 100, address = "0x123"}
hl = {
  bind = function(keys, dispatcher, opts) bound[keys] = {dispatcher, opts}; return bound[keys] end,
  unbind = function(keys) bound[keys] = nil end,
  dispatch = function(dispatcher) dispatched = dispatcher end,
  get_active_window = function() return active end,
  get_current_submap = function() return submap end,
  exec_cmd = function(value) command = value end,
}
dofile(arg[1])
local bridge = herdr_shell_bridge
local fallback = function() error("must use hl.dispatch") end
local opts = {description = "My custom fullscreen", repeatable = true}
hl.bind("SUPER + M", fallback, opts)
local wrapper = bound["SUPER + M"][1]
assert(wrapper ~= fallback and opts.description == "My custom fullscreen")
assert(bound["SUPER + M"][2].repeatable)
bridge.candidates = function() return {} end
wrapper(); assert(dispatched == fallback and command == nil)
bridge.candidates = function() return {{pid = 101, start = "500"}} end
dispatched = nil
wrapper(); assert(dispatched == nil and command:match("pane%-zoom"))
assert(command:match("%-%-window%-pid 100") and command:match("%-%-client%-pid 101"))
command = nil; enabled = "0"
wrapper(); assert(dispatched == fallback and command == nil)
enabled = "1"; dispatched = nil
for _, candidates in ipairs({false, {{pid = 101}, {pid = 102}}}) do
  bridge.candidates = function() return candidates or nil end
  wrapper(); assert(dispatched == nil and command == nil)
end
hl.bind("SUPER + TAB", fallback, opts)
assert(bound["SUPER + TAB"][1] == fallback)
submap = "resize"
hl.bind("SUPER + M", fallback)
assert(bound["SUPER + M"][1] == fallback)
assert(bridge.canonical("SHIFT + SUPER + code:10") == bridge.canonical("SUPER + SHIFT + 1"))
-- Check the actual /proc parser with a terminal and one foreground Herdr child.
submap = ""
dofile(arg[1])
local function process(pid, argv, children, fg)
  local fields = {}; for i=1,50 do fields[i] = "0" end
  fields[1], fields[2], fields[3], fields[5], fields[6], fields[20] = "S", "1", tostring(pid), "9", tostring(fg and pid or 1), "500"
  local base = "/proc/" .. pid
  files[base .. "/stat"] = pid .. " (name ) parentheses) " .. table.concat(fields, " ")
  files[base .. "/cmdline"] = table.concat(argv, "\0") .. "\0"
  files[base .. "/task/" .. pid .. "/children"] = children
end
process(100, {"foot"}, "101", false)
process(101, {"/usr/bin/herdr", "--session", "demo"}, "102", true)
local clients = herdr_shell_bridge.candidates(100)
assert(#clients == 1 and clients[1].pid == 101 and clients[1].start == "500")
print("Lua fallback, focused routing, ambiguity, submap, quoting and process checks passed")
