-- Beam-MP-Server-Manager telemetry bridge for BeamMP Server v3.
-- Collects only gameplay telemetry required by the local manager.

-- Keep generated data in a subfolder. BeamMP v3 hot-reloads the Lua state when
-- files change in the plugin root, so publishing telemetry.json next to main.lua
-- would continuously reload this plugin.
local DATA_DIR = "Resources/Server/BeamServerManager/data"
local DATA_FILE = DATA_DIR .. "/telemetry.json"
local TEMP_FILE = DATA_FILE .. ".tmp"
local CONTROL_FILE = DATA_DIR .. "/control.json"
local STARTED_AT = os.time()
local EVENT_LIMIT = 200

local sessions = {}
local events = {}
local event_sequence = 0
local processed_control_id = nil

local function iso_time(timestamp)
    return os.date("!%Y-%m-%dT%H:%M:%SZ", timestamp)
end

local function safe_number(value)
    if type(value) == "number" and value == value and value ~= math.huge and value ~= -math.huge then
        return value
    end
    return nil
end

local function copy_vector(value, length)
    if type(value) ~= "table" then return nil end
    local result = {}
    for index = 1, length do
        local number = safe_number(value[index])
        if number == nil then return nil end
        result[index] = number
    end
    return result
end

local function parse_vehicle_model(raw)
    if type(raw) ~= "string" then return nil end
    local json_start = string.find(raw, "{", 1, true)
    if json_start == nil then return nil end
    local ok, decoded = pcall(Util.JsonDecode, string.sub(raw, json_start))
    if not ok or type(decoded) ~= "table" then return nil end
    if type(decoded.jbm) == "string" and decoded.jbm ~= "" then return decoded.jbm end
    if type(decoded.model) == "string" and decoded.model ~= "" then return decoded.model end
    return nil
end

local function player_name(player_id)
    local ok, name = pcall(MP.GetPlayerName, player_id)
    if ok and type(name) == "string" and name ~= "" then return name end
    return "Joueur " .. tostring(player_id)
end

local function ensure_session(player_id)
    if sessions[player_id] == nil then
        sessions[player_id] = {
            connected_at = os.time(),
            vehicles_used = {}
        }
    end
    return sessions[player_id]
end

local function push_event(kind, player_id, vehicle_id, model)
    event_sequence = event_sequence + 1
    local event = {
        id = event_sequence,
        type = kind,
        timestamp = iso_time(os.time()),
        timestamp_epoch = os.time(),
        player_id = player_id,
        player_name = player_name(player_id)
    }
    if vehicle_id ~= nil then event.vehicle_id = vehicle_id end
    if model ~= nil then event.vehicle_model = model end
    table.insert(events, event)
    while #events > EVENT_LIMIT do table.remove(events, 1) end
end

local function build_vehicle(player_id, vehicle_id, raw)
    local vehicle = {
        id = vehicle_id,
        model = parse_vehicle_model(raw)
    }
    local ok, position, error_message = pcall(MP.GetPositionRaw, player_id, vehicle_id)
    if ok and type(position) == "table" and (error_message == nil or error_message == "") then
        vehicle.position = copy_vector(position.pos, 3)
        vehicle.velocity = copy_vector(position.vel, 3)
        vehicle.rotation = copy_vector(position.rot, 4)
        local ping_seconds = safe_number(position.ping)
        if ping_seconds ~= nil and ping_seconds >= 0 then
            vehicle.ping_ms = ping_seconds * 1000
        end
        if vehicle.velocity ~= nil then
            local x, y, z = vehicle.velocity[1], vehicle.velocity[2], vehicle.velocity[3]
            vehicle.speed_kmh = math.sqrt(x * x + y * y + z * z) * 3.6
        end
    end
    return vehicle
end

local function build_snapshot()
    local players = {}
    local vehicle_count = 0
    local ok, connected = pcall(MP.GetPlayers)
    if ok and type(connected) == "table" then
        for player_id, name in pairs(connected) do
            -- MP.GetPlayers is the authoritative list of players known by the
            -- server. MP.IsPlayerConnected additionally requires UDP traffic;
            -- do not hide an authenticated player merely because that flag is
            -- temporarily false.
            if type(player_id) == "number" then
                local session = ensure_session(player_id)
                local udp_connected = false
                local connected_ok, connected_value = pcall(MP.IsPlayerConnected, player_id)
                if connected_ok then udp_connected = connected_value == true end
                local player = {
                    id = player_id,
                    name = type(name) == "string" and name or player_name(player_id),
                    connected = udp_connected,
                    connected_at = iso_time(session.connected_at),
                    connected_at_epoch = session.connected_at,
                    vehicles = {}
                }
                local vehicles_ok, vehicle_data = pcall(MP.GetPlayerVehicles, player_id)
                if vehicles_ok and type(vehicle_data) == "table" then
                    for vehicle_id, raw in pairs(vehicle_data) do
                        if type(vehicle_id) == "number" then
                            local vehicle = build_vehicle(player_id, vehicle_id, raw)
                            table.insert(player.vehicles, vehicle)
                            vehicle_count = vehicle_count + 1
                            if vehicle.model ~= nil then session.vehicles_used[vehicle.model] = true end
                        end
                    end
                end
                table.insert(players, player)
            end
        end
    end
    table.sort(players, function(left, right) return left.id < right.id end)
    return {
        schema_version = 1,
        plugin_version = "1.2.1",
        server_started_at = iso_time(STARTED_AT),
        generated_at = iso_time(os.time()),
        generated_at_epoch = os.time(),
        uptime_seconds = os.time() - STARTED_AT,
        player_count = #players,
        vehicle_count = vehicle_count,
        players = players,
        events = events
    }
end

local function ensure_data_directory()
    if FS.Exists(DATA_DIR) then return true end
    local created, create_error = FS.CreateDirectory(DATA_DIR)
    if not created then
        Util.LogError("BeamServerManager: telemetry data directory cannot be created: " .. tostring(create_error))
        return false
    end
    return true
end

local function write_snapshot()
    if not ensure_data_directory() then return end
    local ok, encoded = pcall(Util.JsonEncode, build_snapshot())
    if not ok or type(encoded) ~= "string" then
        Util.LogError("BeamServerManager: telemetry encoding failed")
        return
    end
    local handle = io.open(TEMP_FILE, "w")
    if handle == nil then
        Util.LogError("BeamServerManager: telemetry file cannot be opened")
        return
    end
    handle:write(encoded)
    handle:flush()
    handle:close()
    -- BeamMP Server v3.9.x FS.Rename returns true on success and false on failure.
    local rename_ok, rename_error = FS.Rename(TEMP_FILE, DATA_FILE)
    if not rename_ok then
        Util.LogError("BeamServerManager: telemetry publish failed: " .. tostring(rename_error))
    end
end

local function remove_control_file()
    local removed, remove_error = os.remove(CONTROL_FILE)
    if not removed and remove_error ~= nil then
        Util.LogError("BeamServerManager: control file cannot be removed: " .. tostring(remove_error))
    end
end

local function integer(value)
    return type(value) == "number" and value >= 0 and math.floor(value) == value
end

local function process_control()
    local handle = io.open(CONTROL_FILE, "r")
    if handle == nil then return end
    local raw = handle:read("*a")
    handle:close()

    local decoded_ok, command = pcall(Util.JsonDecode, raw)
    if not decoded_ok or type(command) ~= "table" or command.schema_version ~= 1 then
        Util.LogError("BeamServerManager: invalid control command")
        remove_control_file()
        return
    end

    local command_id = type(command.id) == "string" and command.id or ""
    if command_id == "" then
        Util.LogError("BeamServerManager: control command has no id")
        remove_control_file()
        return
    end
    if processed_control_id == command_id then
        remove_control_file()
        return
    end

    local action_ok, action_error = pcall(function()
        if command.action == "say" then
            if type(command.message) ~= "string" or command.message == "" or #command.message > 500 then
                error("invalid message")
            end
            MP.SendChatMessage(-1, command.message)
            Util.LogInfo("BeamServerManager: web broadcast sent")
        elseif command.action == "kick" then
            if not integer(command.player_id) then error("invalid player id") end
            local reason = command.reason
            if type(reason) ~= "string" or reason == "" then reason = "Kicked by Beam Server Manager" end
            if #reason > 200 then error("invalid kick reason") end
            MP.DropPlayer(command.player_id, reason)
            Util.LogInfo("BeamServerManager: web kick executed for player " .. tostring(command.player_id))
        elseif command.action == "remove_vehicle" then
            if not integer(command.player_id) or not integer(command.vehicle_id) then
                error("invalid vehicle target")
            end
            MP.RemoveVehicle(command.player_id, command.vehicle_id)
            Util.LogInfo(
                "BeamServerManager: web vehicle removal executed for player " ..
                tostring(command.player_id) .. " vehicle " .. tostring(command.vehicle_id)
            )
        else
            error("unsupported action")
        end
    end)

    processed_control_id = command_id
    if not action_ok then
        Util.LogError("BeamServerManager: control command failed: " .. tostring(action_error))
    end
    remove_control_file()
end

function BeamServerManagerTick()
    process_control()
    write_snapshot()
end

function BeamServerManagerPlayerJoin(player_id)
    ensure_session(player_id)
    push_event("player_join", player_id)
    write_snapshot()
end

function BeamServerManagerPlayerDisconnect(player_id)
    push_event("player_disconnect", player_id)
    sessions[player_id] = nil
    write_snapshot()
end

function BeamServerManagerVehicleSpawn(player_id, vehicle_id, vehicle_data)
    local model = parse_vehicle_model(vehicle_data)
    local session = ensure_session(player_id)
    if model ~= nil then session.vehicles_used[model] = true end
    push_event("vehicle_spawn", player_id, vehicle_id, model)
end

function BeamServerManagerVehicleEdited(player_id, vehicle_id, vehicle_data)
    local model = parse_vehicle_model(vehicle_data)
    local session = ensure_session(player_id)
    if model ~= nil then session.vehicles_used[model] = true end
    push_event("vehicle_edited", player_id, vehicle_id, model)
end

function onInit()
    MP.RegisterEvent("BeamServerManagerTick", "BeamServerManagerTick")
    MP.CreateEventTimer("BeamServerManagerTick", 1000)
    MP.RegisterEvent("onPlayerJoin", "BeamServerManagerPlayerJoin")
    MP.RegisterEvent("onPlayerDisconnect", "BeamServerManagerPlayerDisconnect")
    MP.RegisterEvent("onVehicleSpawn", "BeamServerManagerVehicleSpawn")
    MP.RegisterEvent("onVehicleEdited", "BeamServerManagerVehicleEdited")
    Util.LogInfo("BeamServerManager telemetry plugin started (v1.2.1)")
    write_snapshot()
end
