-- Loaded before Omarchy's defaults; wrappers retain the actual dispatchers,
-- including later personal overrides. No process is spawned for desktop fallbacks.
local previous = rawget(_G, "herdr_shell_bridge")
local original_bind = previous and previous.original_bind or hl.bind
local mappings = {
-- GENERATED MAPPINGS
}
local root = (os.getenv("XDG_CONFIG_HOME") or (os.getenv("HOME") .. "/.config")) .. "/herdr-shell/"

local function read(path)
  local file = io.open(path, "rb")
  if not file then return nil end
  local value = file:read("*a")
  file:close()
  return value
end

local function canonical(key)
  local parts = {}
  for token in key:upper():gmatch("[^+]+") do
    local part = token:match("^%s*(.-)%s*$")
    if part == "ENTER" then part = "RETURN" end
    local code = tonumber(part:match("^CODE:(%d+)$"))
    if code and code >= 10 and code <= 19 then part = tostring((code - 9) % 10) end
    if code == 20 then part = "MINUS" end
    if code == 21 then part = "EQUAL" end
    parts[#parts + 1] = part
  end
  local keypart = table.remove(parts)
  table.sort(parts)
  parts[#parts + 1] = keypart
  return table.concat(parts, "+")
end

local by_key = {}
for key, value in pairs(mappings) do by_key[canonical(key)] = value end

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
        local cmdline = read(base .. "/cmdline") or ""
        local argv = {}
        for arg in cmdline:gmatch("([^%z]+)") do argv[#argv + 1] = arg end
        local name = argv[1] and argv[1]:match("([^/]+)$")
        local is_client = name == "herdr" and (not argv[2] or argv[2]:sub(1, 2) == "--" or
                          (argv[2] == "session" and argv[3] == "attach"))
        if is_client and fields[5] ~= "0" and fields[3] == fields[6] then
          found[#found + 1] = {pid = pid, start = fields[20]}
        else
          local children = read(base .. "/task/" .. tostring(pid) .. "/children") or ""
          for child in children:gmatch("%d+") do pending[#pending + 1] = tonumber(child) end
        end
      end
    end
  end
  return found
end

local function quote(value) return "'" .. tostring(value):gsub("'", "'\\''") .. "'" end
local bridge = {original_bind = original_bind, canonical = canonical, candidates = candidates}
_G.herdr_shell_bridge = bridge
local seeded = {}

hl.bind = function(keys, dispatcher, opts)
  local mapping = by_key[canonical(keys)]
  local submap = hl.get_current_submap()
  if not mapping or (submap ~= "" and submap ~= "reset") or (opts and opts.submap and opts.submap ~= "") then
    return original_bind(keys, dispatcher, opts)
  end
  -- Replace our provisional binding if defaults or user config define it later.
  -- Keep their dispatcher as the desktop fallback, without duplicate actions.
  if seeded[canonical(keys)] then
    hl.unbind(keys)
    seeded[canonical(keys)] = nil
  end
  local options = {}
  for k, v in pairs(opts or {}) do options[k] = v end
  options.description = (options.description or keys) .. " / Herdr: " .. mapping[2]
  return original_bind(keys, function()
    local window = hl.get_active_window()
    if not window or not (read(root .. "desktop-enabled") or ""):match("^1") then
      return hl.dispatch(dispatcher)
    end
    local clients = bridge.candidates(window.pid)
    if clients and #clients == 0 then return hl.dispatch(dispatcher) end
    -- Ambiguous or inaccessible trees consume the key; closing the outer window
    -- as an error fallback could terminate the user's terminal unexpectedly.
    if not clients or #clients ~= 1 then return end
    local plugin_root = (read(root .. "plugin-root") or ""):gsub("%s+$", "")
    if plugin_root == "" then return end
    local client = clients[1]
    hl.exec_cmd("python3 " .. quote(plugin_root .. "/bin/herdr-shell") .. " desktop route " .. quote(mapping[1]) ..
      " --window-pid " .. tostring(window.pid) .. " --client-pid " .. tostring(client.pid) ..
      " --start " .. quote(client.start) .. " --address " .. quote(window.address))
  end, options)
end

-- Herdr shortcuts may have no desktop binding to wrap. Seed them
-- before defaults, forwarding the chord outside Herdr. Later desktop bindings
-- replace these provisional fallbacks through the unbind above.
for _, shortcut in ipairs({
  { "SUPER", "A" }, { "SUPER", "U" }, { "SUPER", "D" },
  { "SUPER", "L" }, { "SUPER", "R" }, { "SUPER", "M" },
  { "SUPER", "T" }, { "SUPER + SHIFT", "T" },
  { "SUPER", "P" }, { "SUPER + SHIFT", "P" },
  { "SUPER", "comma" }, { "SUPER + SHIFT", "A" },
  { "SUPER + SHIFT", "N" }, { "SUPER + SHIFT", "F" },
}) do
  local mods, key = shortcut[1], shortcut[2]
  local keys = mods .. " + " .. key
  hl.bind(keys, function()
    hl.dispatch(hl.dsp.send_key_state({ mods = mods, key = key, state = "down" }))
    hl.timer(function()
      hl.dispatch(hl.dsp.send_key_state({ mods = mods, key = key, state = "up" }))
    end, { timeout = 50, type = "oneshot" })
  end, { description = "Pass " .. keys:gsub("%s+", "") .. " to application" })
  seeded[canonical(keys)] = true
end
