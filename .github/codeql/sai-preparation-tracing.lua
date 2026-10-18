-- CodeQL restores its tracing preload in child environments. The pinned SAI
-- and shared build-tools preparation actions reject undeclared libraries in
-- loader --list output, so they must run without that injected preload.
-- The generated SAI metadata and SWSS sources compile in separate Bazel actions.
-- Reassess these exclusions if either pinned action starts a C/C++ compiler.

local function ends_with(value, suffix)
    return string.sub(value, -string.len(suffix)) == suffix
end

local function is_tool_preparation(compilerName, compilerPath, compilerArguments)
    if OperatingSystem ~= "linux" then return false end
    local isPython = compilerName == "python3" or string.match(compilerName, "^python3%.%d+$")
    if not isPython or not string.find(compilerPath, "/external/rules_python++python+", 1, true) then
        return false
    end
    if not string.match(compilerPath, "/bin/python3$") and not string.match(compilerPath, "/bin/python3%.%d+$") then
        return false
    end

    local argv = compilerArguments.argv
    if not argv or #argv < 7 or (#argv - 5) % 2 ~= 0 then return false end
    if argv[4] ~= "--architecture" or (argv[5] ~= "amd64" and argv[5] ~= "arm64") then
        return false
    end
    local script = "external/sai+/bazel/prepare_deb_tools.py"
    local shared = "external/sonic-build-infra+/tools/build_tools/prepare_runtime.py"
    local output, flag, suffix
    local first, last = 6, #argv
    if argv[1] == script or ends_with(argv[1], "/" .. script) then
        output = "/external/sai+/bazel/sai_generator_tools"
        flag, suffix = "--deb", ".deb"
    elseif argv[1] == shared or ends_with(argv[1], "/" .. shared) then
        local profile = "documentation"
        if argv[6] == "--profile" then
            profile = argv[7]
            local profiles = {documentation = true, configure = true, kerberos = true, metadata = true, debian = true}
            if not profiles[profile] then return false end
            local bootstrap = "external/sonic-build-infra+/tools/build_tools/dh_python_bootstrap.py"
            if argv[#argv - 1] ~= "--python-bootstrap" or
               (argv[#argv] ~= bootstrap and not ends_with(argv[#argv], "/" .. bootstrap)) then return false end
            first, last = 8, #argv - 2
        end
        local prefix = profile == "documentation" and "" or profile .. "_"
        output = "/external/sonic-build-infra+/tools/build_tools/" .. prefix .. "runtime_" .. argv[5]
        flag, suffix = "--tar", nil
    else
        return false
    end
    if argv[2] ~= "--out" or not ends_with(argv[3], output) then return false end
    if first > last then return false end
    for index = first, last, 2 do
        if argv[index] ~= flag then return false end
        local archive = argv[index + 1]
        if suffix then
            if not ends_with(archive, suffix) then return false end
        elseif not ends_with(archive, ".tar.gz") and not ends_with(archive, ".tar.xz") then
            return false
        end
    end
    return true
end

function GetCompatibleVersions() return {"1.0.0"} end

function RegisterExtraConfig()
    local matchers = {
        function(compilerName, compilerPath, compilerArguments, languageId)
            if is_tool_preparation(compilerName, compilerPath, compilerArguments) then
                return {trace = false}
            end
        end,
    }
    local existing = GetRegisteredMatchers("cpp")
    if not existing or #existing == 0 then error("C++ tracing matchers must be registered") end
    for _, matcher in ipairs(existing) do
        table.insert(matchers, matcher)
    end
    return {cpp = matchers}
end
