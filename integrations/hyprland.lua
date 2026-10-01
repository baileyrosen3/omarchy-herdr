-- Explicit, conflict-checked Herdr shortcuts. Loaded after desktop/user bindings.
-- Registration is reconciled after reload, when hyprctl can inspect every bind.
local previous = rawget(_G, "herdr_shell_bridge")
if previous and previous.original_bind then hl.bind = previous.original_bind end
local mappings = {
-- GENERATED MAPPINGS
}
local root = (os.getenv("XDG_CONFIG_HOME") or (os.getenv("HOME") .. "/.config")) .. "/herdr-shell/"
local function read(path)
  local file = io.open(path, "rb")
  if not file then return nil end
  local value = file:read("*a"); file:close(); return value
end
local function quote(value) return "'" .. tostring(value):gsub("'", "'\\''") .. "'" end
local function client_options(argv)
  if not argv[1] or argv[1]:match("([^/]+)$") ~= "herdr" then return nil end
  local i, remote, session = 2, false, "default"
  while i <= #argv do
    local value = argv[i]
    if value == "--session" or value == "--remote" or value == "--remote-keybindings" then
      if not argv[i + 1] or argv[i + 1] == "" then return nil end
      if value == "--remote" then remote = true end
      if value == "--session" then session = argv[i + 1] end
      i = i + 2
    elseif value:match("^%-%-session=.+") or value:match("^%-%-remote%-keybindings=.+") or value == "--handoff" then
      if value:match("^%-%-session=") then session = value:match("^%-%-session=(.*)$") end
      i = i + 1
    elseif value:match("^%-%-remote=.+") then
      remote = true; i = i + 1
    elseif value == "session" and argv[i + 1] == "attach" and argv[i + 2] and i + 2 == #argv then
      session = argv[i + 2]
      i = #argv + 1
    else return nil end
  end
  if session == "" or session == "." or session == ".." or session:find("/", 1, true) or session:find("\\", 1, true) then return nil end
  return {remote = remote, session = session}
end
local function candidates(window_pid)
  local pending, seen, found, total = {window_pid}, {}, {}, 0
  while #pending > 0 do
    local pid = table.remove(pending)
    if not seen[pid] then
      seen[pid], total = true, total + 1
      if total > 256 then return nil end
      local base = "/proc/" .. tostring(pid)
      local stat = read(base .. "/stat")
      if not stat and pid == window_pid then return nil end
      local tail = stat and stat:match("^.*%) (.*)$")
      local fields = {}
      if tail then for f in tail:gmatch("%S+") do fields[#fields + 1] = f end end
      if #fields >= 20 and fields[1] ~= "T" and fields[1] ~= "t" and fields[1] ~= "Z" and fields[1] ~= "X" then
        local argv = {}
        for arg in (read(base .. "/cmdline") or ""):gmatch("([^%z]+)") do argv[#argv + 1] = arg end
        local options = client_options(argv)
        if options and fields[5] ~= "0" and fields[3] == fields[6] then
          if not options.remote then found[#found + 1] = {pid = pid, start = fields[20]} end
        else
          for child in (read(base .. "/task/" .. pid .. "/children") or ""):gmatch("%d+") do
            pending[#pending + 1] = tonumber(child)
          end
        end
      end
    end
  end
  return found
end
local generation = (tostring({}) .. tostring(os.clock())):gsub("[^%w]", "")
local bridge = {generation = generation, candidates = candidates, client_options = client_options, registered = {}}
_G.herdr_shell_bridge = bridge
local sequence = 0
local function helper(command)
  local plugin_root = (read(root .. "plugin-root") or ""):gsub("%s+$", "")
  if plugin_root == "" then return end
  hl.exec_cmd("python3 " .. quote(plugin_root .. "/bin/herdr-shell") .. " desktop " .. command)
end
function bridge.register(allowed)
  for _, mapping in ipairs(mappings) do
    local action, keys = mapping.action, mapping.keys
    if not allowed[action] and bridge.registered[action] then
      bridge.registered[action]:unbind()
      bridge.registered[action] = nil
    end
    if allowed[action] and not bridge.registered[action] then
      local mods, key = keys:match("^(.*)%s+%+%s+(%S+)$")
      local function forward()
        hl.dispatch(hl.dsp.send_key_state({mods = mods, key = key, state = "down"}))
        hl.timer(function()
          hl.dispatch(hl.dsp.send_key_state({mods = mods, key = key, state = "up"}))
        end, {timeout = 50, type = "oneshot"})
      end
      bridge.registered[action] = hl.bind(keys, function()
        if not (read(root .. "desktop-enabled") or ""):match("^1") then forward(); return end
        local window = hl.get_active_window()
        if not window then forward(); return end
        local clients = bridge.candidates(window.pid)
        if clients and #clients == 0 then forward(); return end
        if not clients or #clients ~= 1 then return end
        local client = clients[1]
        local file = io.open(root .. "events-" .. generation .. ".queue", "a")
        if not file then return end
        sequence = sequence + 1
        file:write(string.format("%d\t%s\t%d\t%d\t%s\t%s\n", sequence, action,
          window.pid, client.pid, client.start, window.address))
        file:flush(); file:close()
        helper("drain --generation " .. quote(generation))
      end, {description = "Herdr Shell: " .. action, repeating = false})
    end
  end
end
hl.timer(function() helper("reconcile --generation " .. quote(generation)) end,
  {timeout = 100, type = "oneshot"})
