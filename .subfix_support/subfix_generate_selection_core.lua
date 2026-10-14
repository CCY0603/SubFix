#!/usr/bin/env lua
-- SubFix shared module: generate subtitles for the current DaVinci Resolve In/Out selection.

-- 全局关闭 LuaJIT JIT（与 SubFix.lua 同一守卫）：本模块被宿主 dofile 时通常已关闭，
-- 作为独立入口运行时由此兜底。原因详见 SubFix.lua 顶部说明——Resolve 的 LuaJIT
-- 2.1.0-beta3 在 FFI/热循环/嵌套 RunLoop 组合下存在误编译写坏 C 堆的随机崩溃。
if not _G.SUBFIX_JIT_DISABLED then
    _G.SUBFIX_JIT_DISABLED = true
    pcall(function()
        local jit_mod = require("jit")
        jit_mod.off()
        if jit_mod.flush then pcall(jit_mod.flush) end
    end)
end

-- 跨平台辅助：macOS / Windows 下路径与系统调用差异集中处理。
-- 若 SubFix.lua 已定义则复用，否则在此定义，保证本模块作为独立入口（生成选区字幕.lua）也能运行。
if not _G.SUBFIX_IS_WINDOWS then
    _G.SUBFIX_PATH_SEP = package.config:sub(1, 1)
    _G.SUBFIX_IS_WINDOWS = (_G.SUBFIX_PATH_SEP == "\\")

    _G.subfix_pjoin = function(dir_path, leaf_name)
        local dir_value = tostring(dir_path or "")
        local name_value = tostring(leaf_name or "")
        if dir_value == "" then return name_value end
        if name_value == "" then return dir_value end
        local tail = dir_value:sub(-1)
        if tail == "/" or tail == "\\" then return dir_value .. name_value end
        return dir_value .. _G.SUBFIX_PATH_SEP .. name_value
    end

    _G.subfix_home_dir = function()
        return os.getenv("USERPROFILE") or os.getenv("HOME") or ""
    end

    _G.subfix_dr_utility_root = function()
        if _G.SUBFIX_IS_WINDOWS then
            local appdata = os.getenv("APPDATA") or ""
            local programdata = os.getenv("ProgramData") or ""
            local rel = "Blackmagic Design\\DaVinci Resolve\\Fusion\\Scripts\\Utility"
            return {
                user = appdata ~= "" and _G.subfix_pjoin(appdata, rel) or nil,
                system = programdata ~= "" and _G.subfix_pjoin(programdata, rel) or nil
            }
        else
            local home = _G.subfix_home_dir()
            local rel = "Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
            return {
                user = home ~= "" and _G.subfix_pjoin(home, rel) or nil,
                system = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
            }
        end
    end

    _G.subfix_support_dir = function(utility_root)
        return _G.subfix_pjoin(tostring(utility_root or (_G.subfix_dr_utility_root().user or "")), ".subfix_support")
    end

    _G.subfix_venv_python = function(helper_dir)
        if _G.SUBFIX_IS_WINDOWS then
            return _G.subfix_pjoin(helper_dir, ".subfix_asr_env\\Scripts\\python.exe")
        end
        return _G.subfix_pjoin(helper_dir, ".subfix_asr_env/bin/python")
    end

    _G.subfix_runtime_python = function(helper_dir)
        if _G.SUBFIX_IS_WINDOWS then
            return _G.subfix_pjoin(helper_dir, "runtime\\python\\python.exe")
        end
        return _G.subfix_pjoin(helper_dir, "runtime/python/bin/python3")
    end

    _G.subfix_dq = function(value)
        local s = tostring(value or ""):gsub('"', '\\"')
        return '"' .. s .. '"'
    end

    _G.subfix_open_url = function(url)
        local u = tostring(url or "")
        if u == "" then return false end
        if _G.SUBFIX_IS_WINDOWS then
            os.execute("cmd /c start \"\" " .. _G.subfix_dq(u))
            return true
        end
        os.execute("/usr/bin/open " .. (shell_quote and shell_quote(u) or _G.subfix_dq(u)))
        return true
    end

    if not _G.shell_quote then
        _G.shell_quote = function(value)
            if _G.SUBFIX_IS_WINDOWS then
                return _G.subfix_dq(value)
            end
            local s = tostring(value or "")
            return "'" .. s:gsub("'", "'\\''") .. "'"
        end
    end
end

-- Windows 宽字符能力表（在本文件稍后的 ffi 块里填充）。前向声明为单一 local，
-- 让前面定义的 temp_dir/read_text_file/write_text_file 能在“运行时”引用，
-- 同时只占一个 Lua 5.1 顶层 local 槽位。非 Windows 或 ffi 不可用时保持为空表，
-- 所有调用方据此回退到 ANSI io.open / os.execute。
local subfix_winio = {}

-- 统一临时目录：Windows 用 TEMP/TMP，其余用 TMPDIR//tmp；返回值不带结尾分隔符。
local function temp_dir()
    local base
    if SUBFIX_IS_WINDOWS then
        base = os.getenv("TEMP") or os.getenv("TMP") or os.getenv("TMPDIR")
    else
        base = os.getenv("TMPDIR") or os.getenv("TEMP") or os.getenv("TMP")
    end
    if not base or base == "" then
        base = SUBFIX_IS_WINDOWS and "." or "/tmp"
    end
    base = tostring(base):gsub("[/\\]+$", "")
    local root = base .. "/SubFix_GenerateSelectionSubtitles"
    if SUBFIX_IS_WINDOWS then
        -- 优先宽字符 CreateDirectoryW（TEMP 含中文用户名也能建）；不可用时回退 ANSI cmd。
        if not (subfix_winio.mkdir and subfix_winio.mkdir(root)) then
            os.execute('cmd /c if not exist "' .. root .. '" mkdir "' .. root .. '"')
        end
    else
        os.execute("mkdir -p " .. shell_quote(root) .. " 2>/dev/null")
    end
    return root
end

local SubFixGenerateSelectionCore = {}
local runtime_options = {}
local TARGET_SUBTITLE_TRACK = 1
local DEFAULT_ASR_MODEL = "large-v3-turbo"
local DEFAULT_ASR_LANGUAGE = tostring(os.getenv("SUBFIX_ASR_LANGUAGE") or "zh")
local DEFAULT_ASR_BACKEND = "auto"
local WORK_SCOPE_MODE_SELECTION = "selection"
local FALLBACK_ITEM_SCOPE_EXPAND_MAX_GAP_FRAMES = 2
local GENERATE_PROGRESS_BAR_WIDTH = 36
local GENERATE_PROGRESS_STALL_WARNING_SECONDS = 300
local GENERATE_BACKUP_PROGRESS_CHUNK_SIZE = 25
local GENERATE_UI_YIELD_INTERVAL_MS = 10
local GENERATE_WRITEBACK_OVERLAY_GEOMETRY = {120, 90, 1680, 880}

-- 宿主注入：SubFix.lua 在 loadfile 本模块前设置 _G.SUBFIX_GENERATE_CORE_HOST。
-- embedded 模式下复用主窗口的 UIManager/dispatcher：不创建第二个 UIDispatcher、
-- 不覆盖宿主的 On.Timeout（主路由在 SubFix.lua 内），timer 注册/注销走宿主函数。
-- standalone 调试模式（直接运行「生成选区字幕.lua」）无 host，保持自建 dispatcher/RunLoop。
local host_env = _G.SUBFIX_GENERATE_CORE_HOST
local EMBEDDED = (type(host_env) == "table") and host_env.mode == "embedded" or false

local fusion_app = fu or fusion
local ui = EMBEDDED and host_env.ui or (fusion_app and fusion_app.UIManager or nil)
local dispatcher
if EMBEDDED then
    dispatcher = host_env.dispatcher
else
    dispatcher = (ui and bmd and bmd.UIDispatcher) and bmd.UIDispatcher(ui) or nil
end
local ui_timer_handlers = {}

-- standalone 模式才挂载本模块自有的 On.Timeout；embedded 模式事件由宿主统一路由，
-- 绝不能覆盖 dispatcher.On.Timeout，否则主窗口的 timer 全部失效。
if dispatcher and dispatcher.On and not EMBEDDED then
    function dispatcher.On.Timeout(ev)
        local timer_id = tostring(ev and ev.who or "")
        local handler = ui_timer_handlers[timer_id]
        if handler then handler(ev) end
    end
end

-- 统一视觉常量：14px 字体 + 深色专业主题（背景 #1A212B / #0F141A、文字 #D6DDE7、强调橙 #FF6A00、选中橙 #FF6A00）。
local DIALOG_FONT = nil
if ui then
    DIALOG_FONT = ui:Font{PixelSize = 14}
end
-- Fusion UIManager 的逐项颜色（TextColor[]/BackgroundColor[]）取值为 0–1 浮点；
-- 传 0–255 会被钳位成 1.0（纯黄/纯白），故统一经本函数换算，避免再踩坑。
local function ui_color(r, g, b, a)
    return {R = r / 255, G = g / 255, B = b / 255, A = (a or 255) / 255}
end
local SUBFIX_TEXT_COLOR = ui_color(214, 221, 231)      -- #D6DDE7
local SUBFIX_ACCENT_COLOR = ui_color(255, 106, 0)      -- #FF6A00
local SUBFIX_CHECKED_COLOR = ui_color(255, 106, 0)     -- #FF6A00
-- 音频轨道树样式改用窗口级 QSS 承载：未选中=白灰字(#C2C7D0)，选中=橙底(#FF6A00)+深色字，
-- 通过 selection-background-color / ::item:selected 覆盖原生高亮（原生高亮本机可见，仅颜色需改）。
-- 样式令牌：embedded（与宿主同 VM）时优先复用宿主 SUBFIX_DESIGN，保证左右两栏完全一致；
-- standalone 调试窗使用下面的兜底常量（与宿主令牌保持同值，含 WCAG 对比度修正）。
-- 注意：Fusion loadfile chunk 与主脚本沙箱全局不互通，rawget(_G,"SUBFIX_DESIGN")
-- 实测为 nil，embedded 下必须走 host_env.design 显式注入。
local host_design = (type(host_env) == "table" and host_env.design) or rawget(_G, "SUBFIX_DESIGN")
local function host_ui_style(name)
    return host_design and host_design.styles and host_design.styles[name] or nil
end

local SUBFIX_ROOT_STYLESHEET = host_ui_style("panel") or [[
QWidget { font-size: 14px; color: #D6DDE7; background-color: #1A212B; }
QLabel { font-size: 14px; color: #D6DDE7; background-color: transparent; }
QPushButton { font-size: 13px; background-color: #282828; color: #D6DDE7; border: 1px solid #454545; border-radius: 5px; padding: 4px 10px; }
QPushButton:hover { background-color: #333333; border: 1px solid #6E6E6E; }
QPushButton:pressed { background-color: #1F1F1F; border: 1px solid #454545; }
QPushButton:disabled { color: #7A7A7A; border: 1px solid #3A3A3A; }
QLineEdit, QTextEdit, QComboBox { background-color: #0F141A; color: #D6DDE7; border: 1px solid #34506b; border-radius: 5px; padding: 6px; font-size: 14px; }
QTreeWidget { background-color: #0F141A; color: #C2C7D0; border: 1px solid #34506b; border-radius: 5px; font-size: 14px; selection-background-color: #FF6A00; selection-color: #1A212B; }
QTreeWidget::item:selected { background-color: #FF6A00; color: #1A212B; }
QTreeWidget::item:!selected:hover { background-color: #243240; }
QTreeWidget::item { padding: 6px; }
QComboBox QAbstractItemView { background-color: #0F141A; color: #D6DDE7; selection-background-color: #FF6A00; selection-color: #1A212B; }
]]
-- 主操作按钮：橙底 + 深色字（过 AA）；未选中按钮的基础样式（13px 控件字号与宿主一致）。
local SUBFIX_BTN_ACCENT = host_ui_style("btn_accent") or "QPushButton{font-size:16px;font-weight:normal;background-color:#FF6A00;color:#14161A;border:1px solid #FF6A00;border-radius:6px;padding:6px 18px;} QPushButton:hover{background-color:#FF7D1F;color:#14161A;border:1px solid #FF7D1F;} QPushButton:pressed{background-color:#E86200;color:#14161A;border:1px solid #E86200;}"
local SUBFIX_BTN_BASE = host_ui_style("btn_base") or "QPushButton{font-size:13px;background-color:#243240;color:#D6DDE7;border:1px solid #34506b;border-radius:5px;padding:4px 10px;}"
-- 选中态：描边高亮（不填充），与"橙色边框"一致的观感
local SUBFIX_BTN_SELECTED = host_ui_style("btn_selected") or "QPushButton{font-size:13px;background-color:#1A212B;color:#FF6A00;font-weight:bold;border:1px solid #FF6A00;border-radius:5px;padding:4px 10px;} QPushButton:hover{background-color:#243240;border:1px solid #FF8C33;}"
-- 切换按钮统一样式：未选中=深灰底+灰边框+灰白字；选中(:checked)=深底+橙边框+橙字。
-- 用 Qt 的 :checked 伪状态驱动选中态，避免依赖运行时改写 StyleSheet（本机 Fusion 运行时改 StyleSheet 不生效）。
local SUBFIX_BTN_TOGGLE = host_ui_style("btn_toggle") or [[
QPushButton{font-size:13px;background-color:#243240;color:#D6DDE7;border:1px solid #34506b;border-radius:5px;padding:4px 10px;}
QPushButton:hover{background-color:#2c3e52;border:1px solid #4A6785;}
QPushButton:checked{background-color:#1A212B;color:#FF6A00;font-weight:bold;border:1px solid #FF6A00;}
QPushButton:checked:hover{background-color:#243240;border:1px solid #FF8C33;}
]]

-- 内嵌进度不画条形（Fusion UIManager 的 Weight/像素宽运行时改不可靠，条形多次尝试均失败），
-- 改为把「进度 X% · 用时 Ns」整行用主题橙色加粗标注。
-- 样式构建时静态赋予（Fusion 运行时改 StyleSheet 不生效），选择器带 #ID 限定只染这一个标签。
local _prog_colors = host_design and host_design.color
local _prog_accent = (_prog_colors and _prog_colors.accent) or "#FF6A00"
local SUBFIX_PROGRESS_META_STYLE = ([[QLabel#GenerateEmbeddedProgressMetaLabel{color:%s;font-weight:bold;}]]):format(_prog_accent)

local function shell_quote(value)
    -- Windows 的 cmd.exe 不认 POSIX 单引号：'C:\...\python.exe' 会被当成含非法字符的
    -- 文件名，报 “The filename, directory name, or volume label syntax is incorrect.”。
    -- 必须复用全局的 Windows 感知实现（双引号）；POSIX 分支保持单引号转义不变。
    -- 注意：此局部函数会遮蔽全局 shell_quote，因此这里必须自己处理平台差异。
    if _G.SUBFIX_IS_WINDOWS then
        if _G.subfix_dq then return _G.subfix_dq(value) end
        local fallback = tostring(value or ""):gsub('"', "")
        return '"' .. fallback .. '"'
    end
    local text = tostring(value or "")
    return "'" .. text:gsub("'", "'\\''") .. "'"
end

-- 模块可独立加载；复用全局命名空间也避免主脚本的 local 槽位上限。
SUBFIX_WINDOW_GEOMETRY = SUBFIX_WINDOW_GEOMETRY or {}

-- embedded 模式下宿主（SubFix.lua）已提供这两个函数（含小屏钳制逻辑），
-- 绝不能无条件覆盖；仅 standalone 调试运行、宿主实现缺失时才由本模块补定义。
if not (type(SUBFIX_WINDOW_GEOMETRY.resolve_screen_bounds) == "function"
    and type(SUBFIX_WINDOW_GEOMETRY.centered_geometry) == "function") then

function SUBFIX_WINDOW_GEOMETRY.resolve_screen_bounds()
    if not (io and io.popen) then return nil end

    -- JXA 的返回值写入 stdout；console.log 写入 stderr，会被下面的重定向丢弃。
    local jxa = [[(function () {
    ObjC.import("AppKit");
    ObjC.import("CoreGraphics");
    const screens = $.NSScreen.screens;
    const primary = screens.objectAtIndex(0);
    const desktopTop = Number(primary.frame.origin.y) + Number(primary.frame.size.height);
    function screenRect(frame) {
        return {x: Number(frame.origin.x),
            y: desktopTop - Number(frame.origin.y) - Number(frame.size.height),
            width: Number(frame.size.width), height: Number(frame.size.height)};
    }
    let selected = primary;
    let mainWindow = null;
    let largestArea = 0;
    try {
        const raw = $.CGWindowListCopyWindowInfo(17, 0);
        const windows = ObjC.deepUnwrap(ObjC.castRefToObject(raw));
        for (const window of windows) {
            if (!/^(DaVinci Resolve|Resolve)$/i.test(String(window.kCGWindowOwnerName || ""))
                || Number(window.kCGWindowLayer) !== 0 || Number(window.kCGWindowAlpha) === 0) continue;
            const bounds = window.kCGWindowBounds;
            const area = bounds ? Number(bounds.Width) * Number(bounds.Height) : 0;
            if (area > largestArea) { largestArea = area; mainWindow = bounds; }
        }
    } catch (error) {
        // Window information may be unavailable; retain the primary display fallback.
    }
    if (mainWindow) {
        let largestOverlap = 0;
        for (let i = 0; i < Number(screens.count); i++) {
            const screen = screens.objectAtIndex(i);
            const rect = screenRect(screen.frame);
            const overlapWidth = Math.max(0, Math.min(rect.x + rect.width, Number(mainWindow.X) + Number(mainWindow.Width)) - Math.max(rect.x, Number(mainWindow.X)));
            const overlapHeight = Math.max(0, Math.min(rect.y + rect.height, Number(mainWindow.Y) + Number(mainWindow.Height)) - Math.max(rect.y, Number(mainWindow.Y)));
            const overlap = overlapWidth * overlapHeight;
            if (overlap > largestOverlap) { largestOverlap = overlap; selected = screen; }
        }
    }
    const visible = screenRect(selected.visibleFrame);
    return [visible.x, visible.y, visible.width, visible.height].join(",");
})();]]
    if SUBFIX_IS_WINDOWS then return nil end
    local escaped = jxa:gsub("'", "'\\\"'\\\"'")
    local pipe = io.popen("/usr/bin/osascript -l JavaScript -e '" .. escaped .. "' 2>/dev/null", "r")
    if not pipe then return nil end
    local output = pipe:read("*a") or ""
    pipe:close()
    local x, y, width, height = output:match("^%s*([%-%.%d]+),([%-%.%d]+),([%-%.%d]+),([%-%.%d]+)%s*$")
    x, y, width, height = tonumber(x), tonumber(y), tonumber(width), tonumber(height)
    if not x or not y or not width or not height or width <= 0 or height <= 0 then return nil end
    return {x = x, y = y, width = width, height = height}
end

function SUBFIX_WINDOW_GEOMETRY.centered_geometry(fallback_geometry)
    local fallback_x = tonumber(fallback_geometry and fallback_geometry[1])
    local fallback_y = tonumber(fallback_geometry and fallback_geometry[2])
    local width = tonumber(fallback_geometry and fallback_geometry[3])
    local height = tonumber(fallback_geometry and fallback_geometry[4])
    if not fallback_x or not fallback_y or not width or not height then return fallback_geometry end

    local screen = SUBFIX_WINDOW_GEOMETRY.resolve_screen_bounds()
    if not screen then return fallback_geometry end

    local x = screen.x
    local y = screen.y
    if width <= screen.width then x = math.floor(screen.x + (screen.width - width) / 2) end
    if height <= screen.height then y = math.floor(screen.y + (screen.height - height) / 2) end
    return {x, y, width, height}
end

end

local function read_text_file(path)
    local p = tostring(path or "")
    local file = io.open(p, "r")
    if file then
        local content = file:read("*a")
        file:close()
        return content
    end
    -- Windows ANSI 打不开非当前代码页路径时，回退 CreateFileW 宽字符读取（字节按 UTF-8 解读）。
    if subfix_winio.read then
        return subfix_winio.read(p)
    end
    return nil
end

local function write_text_file(path, content)
    local p = tostring(path or "")
    local text = tostring(content or "")
    local file = io.open(p, "w")
    if file then
        file:write(text)
        file:close()
        return true
    end
    -- Windows 宽字符写出兜底（CreateFileW + WriteFile，按原始 UTF-8 字节）。
    if subfix_winio.write then
        return subfix_winio.write(p, text)
    end
    return false
end

-- 二进制读写（.cmd/.ps1/日志拷贝需要逐字节，避免 io.open 文本模式把 \n 翻译成 \r\n）。
local function read_binary_file(path)
    local p = tostring(path or "")
    local file = io.open(p, "rb")
    if file then
        local content = file:read("*a")
        file:close()
        return content
    end
    if subfix_winio.read then
        return subfix_winio.read(p)
    end
    return nil
end

local function write_binary_file(path, data)
    local p = tostring(path or "")
    local bytes = tostring(data or "")
    local file = io.open(p, "wb")
    if file then
        file:write(bytes)
        file:close()
        return true
    end
    if subfix_winio.write then
        return subfix_winio.write(p, bytes)
    end
    return false
end

local function json_escape(value)
    -- 补全 JSON 必需转义：\\ \" \n \r \t \b \f，以及其余 U+0000-001F 控制字符
    -- （Lua 字符串里可能带入 NUL/ESC 等，直接拼进 JSON 会导致 Python json.loads 失败）。
    local result = tostring(value or "")
        :gsub("\\", "\\\\")
        :gsub('"', '\\"')
        :gsub("\n", "\\n")
        :gsub("\r", "\\r")
        :gsub("\t", "\\t")
        :gsub("\b", "\\b")
        :gsub("\f", "\\f")
    result = result:gsub("[\1-\31]", function(ch)
        return string.format("\\u%04x", string.byte(ch))
    end)
    return result
end

local function set_tree_item_text(item, column, text)
    pcall(function() item.Text[column] = tostring(text or "") end)
end



local function get_tree_event_value(ev, keys)
    for _, key in ipairs(keys or {}) do
        if ev and ev[key] ~= nil then return ev[key] end
    end
    return nil
end

local function get_selected_tree_node(tree)
    if not tree then return nil end
    local attempts = {
        function() return tree.CurrentItem end,
        function() return tree.SelectedItem end,
        function()
            local selected = tree:SelectedItems()
            return selected and selected[1] or nil
        end,
    }
    for _, getter in ipairs(attempts) do
        local ok, item = pcall(getter)
        if ok and item then return item end
    end
    return nil
end

local function safe_refresh_tree_widget(tree)
    if not tree then return end
    pcall(function() tree:Update() end)
    pcall(function() tree:Repaint() end)
end

local function trim_text(value)
    return tostring(value or ""):match("^%s*(.-)%s*$") or ""
end

-- 从 .srt 文本中抽取字幕正文：跳过序号行与 "-->" 时间轴行，保留其余文本行。
local function extract_srt_text(content)
    if not content then return "" end
    local out = {}
    for line in (content .. "\n"):gmatch("(.-)\n") do
        local s = trim_text(line)
        if s ~= "" then
            if string.match(s, "^%d+$") then
                -- 序号行，跳过
            elseif string.match(s, "%d+:%d+:%d+[%.,]%d+%s*%-%->%s*%d+:%d+:%d+[%.,]%d+") then
                -- 时间轴行，跳过
            else
                out[#out + 1] = s
            end
        end
    end
    return table.concat(out, "\n")
end

local function decode_json_text(json_text)
    if type(json_text) ~= "string" or json_text == "" then return nil, "JSON 为空" end
    local pos = 1
    local len = #json_text
    local parse_value

    local function fail(msg)
        error(msg .. " at " .. tostring(pos), 0)
    end

    local function skip_ws()
        while pos <= len and json_text:sub(pos, pos):match("%s") do
            pos = pos + 1
        end
    end

    local function parse_string()
        if json_text:sub(pos, pos) ~= '"' then fail("expected string") end
        pos = pos + 1
        local parts = {}
        while pos <= len do
            local ch = json_text:sub(pos, pos)
            if ch == '"' then
                pos = pos + 1
                return table.concat(parts)
            elseif ch == "\\" then
                local esc = json_text:sub(pos + 1, pos + 1)
                local mapped = ({['"']='"', ["\\"]="\\", ["/"]="/", b="\b", f="\f", n="\n", r="\r", t="\t"})[esc]
                if mapped then
                    parts[#parts + 1] = mapped
                    pos = pos + 2
                elseif esc == "u" then
                    -- 正确解码 \uXXXX（含代理对），编码回 UTF-8 字节，避免中文变成乱码/问号。
                    local code = tonumber(json_text:sub(pos + 2, pos + 5), 16)
                    if not code then fail("bad unicode escape") end
                    pos = pos + 6
                    if code >= 0xD800 and code <= 0xDBFF and json_text:sub(pos, pos + 1) == "\\u" then
                        local low = tonumber(json_text:sub(pos + 2, pos + 5), 16)
                        if low and low >= 0xDC00 and low <= 0xDFFF then
                            code = 0x10000 + (code - 0xD800) * 0x400 + (low - 0xDC00)
                            pos = pos + 6
                        end
                    end
                    if code < 0x80 then
                        parts[#parts + 1] = string.char(code)
                    elseif code < 0x800 then
                        parts[#parts + 1] = string.char(
                            0xC0 + math.floor(code / 0x40),
                            0x80 + (code % 0x40)
                        )
                    elseif code < 0x10000 then
                        parts[#parts + 1] = string.char(
                            0xE0 + math.floor(code / 0x1000),
                            0x80 + (math.floor(code / 0x40) % 0x40),
                            0x80 + (code % 0x40)
                        )
                    else
                        parts[#parts + 1] = string.char(
                            0xF0 + math.floor(code / 0x40000),
                            0x80 + (math.floor(code / 0x1000) % 0x40),
                            0x80 + (math.floor(code / 0x40) % 0x40),
                            0x80 + (code % 0x40)
                        )
                    end
                else
                    fail("bad escape")
                end
            else
                parts[#parts + 1] = ch
                pos = pos + 1
            end
        end
        fail("unterminated string")
    end

    local function parse_number()
        local text = json_text:sub(pos):match("^%-?%d+%.?%d*[eE]?[%+%-]?%d*")
        if not text or text == "" then fail("expected number") end
        pos = pos + #text
        return tonumber(text)
    end

    local function parse_array()
        pos = pos + 1
        skip_ws()
        local result = {}
        if json_text:sub(pos, pos) == "]" then pos = pos + 1; return result end
        while true do
            result[#result + 1] = parse_value()
            skip_ws()
            local ch = json_text:sub(pos, pos)
            if ch == "," then
                pos = pos + 1
                skip_ws()
            elseif ch == "]" then
                pos = pos + 1
                return result
            else
                fail("expected array separator")
            end
        end
    end

    local function parse_object()
        pos = pos + 1
        skip_ws()
        local result = {}
        if json_text:sub(pos, pos) == "}" then pos = pos + 1; return result end
        while true do
            skip_ws()
            local key = parse_string()
            skip_ws()
            if json_text:sub(pos, pos) ~= ":" then fail("expected colon") end
            pos = pos + 1
            result[key] = parse_value()
            skip_ws()
            local ch = json_text:sub(pos, pos)
            if ch == "," then
                pos = pos + 1
                skip_ws()
            elseif ch == "}" then
                pos = pos + 1
                return result
            else
                fail("expected object separator")
            end
        end
    end

    parse_value = function()
        skip_ws()
        local ch = json_text:sub(pos, pos)
        if ch == '"' then return parse_string() end
        if ch == "{" then return parse_object() end
        if ch == "[" then return parse_array() end
        if ch == "t" and json_text:sub(pos, pos + 3) == "true" then pos = pos + 4; return true end
        if ch == "f" and json_text:sub(pos, pos + 4) == "false" then pos = pos + 5; return false end
        if ch == "n" and json_text:sub(pos, pos + 3) == "null" then pos = pos + 4; return nil end
        if ch == "-" or ch:match("%d") then return parse_number() end
        fail("unexpected JSON value")
    end

    local ok, result = pcall(function()
        local value = parse_value()
        skip_ws()
        return value
    end)
    if ok then return result end
    return nil, tostring(result)
end

-- Windows 下 Resolve 自带 Lua（LuaJIT）用 ANSI 的 io.open，打不开中文/Unicode 路径。
-- 这里尝试用宽字符 API GetFileAttributesW 作为回退，并用 ffi 调用。
local _subfix_ffi = nil
local _subfix_get_file_attributes_w = nil
do
    local ok_ffi, f = pcall(require, "ffi")
    if ok_ffi and f then
        _subfix_ffi = f
        pcall(function()
            -- 原型与 SubFix.lua 保持完全一致（embedded 同 VM 下为兼容的重复声明）。
            _subfix_ffi.cdef[[ unsigned long GetFileAttributesW(const wchar_t* lpFileName); ]]
            _subfix_get_file_attributes_w = _subfix_ffi.C.GetFileAttributesW
        end)
    end
end

-- Fusion 文本框（中文 Windows）返回的是系统 ANSI/GBK 字节，而本模块与下游 Python 均按 UTF-8 处理。
-- 这里用 FFI 把 ANSI/GBK 文本转成 UTF-8；若已是合法 UTF-8 或无法转换，则原样返回（交由 Python 端按系统编码兜底）。
local _subfix_mbwc, _subfix_wcmb = nil, nil
if _subfix_ffi then
    pcall(function()
        _subfix_ffi.cdef[[
            int __stdcall MultiByteToWideChar(unsigned int CodePage, unsigned long dwFlags, const char* lpMultiByteStr, int cbMultiByte, wchar_t* lpWideCharStr, int cchWideChar);
            int __stdcall WideCharToMultiByte(unsigned int CodePage, unsigned long dwFlags, const wchar_t* lpWideCharStr, int cchWideChar, char* lpMultiByteStr, int cbMultiByte, const char* lpDefaultChar, int* lpUsedDefaultChar);
        ]]
        _subfix_mbwc = _subfix_ffi.C.MultiByteToWideChar
        _subfix_wcmb = _subfix_ffi.C.WideCharToMultiByte
    end)
end

local function subfix_is_utf8(s)
    if type(s) ~= "string" then return true end
    local i, n = 1, #s
    while i <= n do
        local b = string.byte(s, i)
        local len
        if b < 0x80 then len = 1
        elseif b >= 0xC2 and b <= 0xDF then len = 2
        elseif b >= 0xE0 and b <= 0xEF then len = 3
        elseif b >= 0xF0 and b <= 0xF4 then len = 4
        else return false end
        if i + len - 1 > n then return false end
        for k = 1, len - 1 do
            local b2 = string.byte(s, i + k)
            if b2 < 0x80 or b2 > 0xBF then return false end
        end
        i = i + len
    end
    return true
end

local function subfix_ansi_to_utf8(s)
    if type(s) ~= "string" or s == "" then return s end
    if subfix_is_utf8(s) then
        return s
    end
    if not _subfix_ffi or not _subfix_mbwc or not _subfix_wcmb then
        return s
    end
    local CP_ACP, CP_UTF8 = 0, 65001
    local wlen = _subfix_mbwc(CP_ACP, 0, s, -1, nil, 0)
    if not wlen or wlen <= 0 then return s end
    local wbuf = _subfix_ffi.new("wchar_t[?]", wlen)
    _subfix_mbwc(CP_ACP, 0, s, -1, wbuf, wlen)
    local blen = _subfix_wcmb(CP_UTF8, 0, wbuf, -1, nil, 0, nil, nil)
    if not blen or blen <= 0 then return s end
    local bbuf = _subfix_ffi.new("char[?]", blen)
    _subfix_wcmb(CP_UTF8, 0, wbuf, -1, bbuf, blen, nil, nil)
    local out = _subfix_ffi.string(bbuf, blen - 1)
    -- 仅当转换结果是合法 UTF-8 才采用；否则原样返回，交由 Python 端容错解码，
    -- 避免把“以 UTF-8 为主、夹带少量损坏字节”的文本当成纯 GBK 转坏。
    if subfix_is_utf8(out) then
        return out
    end
    return s
end

-- 将 UTF-8 字符串转为 UTF-16LE 字节串。
local function subfix_utf8_to_utf16le(s)
    if type(s) ~= "string" then return nil end
    local out = {}
    local i, n = 1, #s
    while i <= n do
        local b = string.byte(s, i)
        local cp
        if b < 0x80 then
            cp, i = b, i + 1
        elseif b < 0xC0 then
            cp, i = b, i + 1
        elseif b < 0xE0 then
            cp = ((b - 0xC0) * 0x40) + (string.byte(s, i + 1) - 0x80)
            i = i + 2
        elseif b < 0xF0 then
            cp = ((b - 0xE0) * 0x1000) + ((string.byte(s, i + 1) - 0x80) * 0x40) + (string.byte(s, i + 2) - 0x80)
            i = i + 3
        else
            cp = ((b - 0xF0) * 0x40000) + ((string.byte(s, i + 1) - 0x80) * 0x1000) + ((string.byte(s, i + 2) - 0x80) * 0x40) + (string.byte(s, i + 3) - 0x80)
            i = i + 4
        end
        if cp < 0x10000 then
            out[#out + 1] = string.char(cp % 256)
            out[#out + 1] = string.char(math.floor(cp / 256))
        else
            cp = cp - 0x10000
            local hi = 0xD800 + math.floor(cp / 0x400)
            local lo = 0xDC00 + (cp % 0x400)
            out[#out + 1] = string.char(hi % 256); out[#out + 1] = string.char(math.floor(hi / 256))
            out[#out + 1] = string.char(lo % 256); out[#out + 1] = string.char(math.floor(lo / 256))
        end
    end
    return table.concat(out)
end

local function file_exists(path)
    path = tostring(path or "")
    if path == "" then return false end
    local file = io.open(path, "rb")
    if file then
        file:close()
        return true
    end
    -- ANSI io.open 打不开中文路径：用 GetFileAttributesW 宽字符判定。
    if _subfix_get_file_attributes_w and _subfix_ffi then
        local ok, w = pcall(subfix_utf8_to_utf16le, path)
        if ok and w then
            local buf = _subfix_ffi.new("char[?]", #w + 2)
            -- 显式长度：字符串源默认拷 #src+1（多 1 个隐式 NUL），会越界写穿相邻 GC 对象。
            _subfix_ffi.copy(buf, w .. "\0\0", #w + 2)
            local attr = _subfix_get_file_attributes_w(_subfix_ffi.cast("wchar_t*", buf))
            -- INVALID_FILE_ATTRIBUTES == (DWORD)-1 == 0xFFFFFFFF；用无符号常量比较，
            -- 避免 uint32 cdata 与负数字面量比较时被提升成 64 位而误判。
            if tonumber(attr) and tonumber(attr) ~= 0xFFFFFFFF then return true end
        end
    end
    return false
end

-- ============================================================================
-- Windows 宽字符文件 I/O 与无窗口进程启动（ShellExecuteW 范式，与 SubFix.lua 一致）。
-- 背景：Resolve 自带 LuaJIT 的 io.open/os.execute 按系统 ANSI 代码页解释路径，当
-- TEMP/脚本根/媒体路径含当前代码页之外的字符（如非中文系统上的中文用户名）时会失败。
-- 下列能力统一吃 UTF-8，内部转 UTF-16 调 W 系列 API；每个符号独立 pcall 声明：
-- embedded 同 VM 下宿主可能已声明相同原型（C 允许兼容的重复声明），即使个别符号声明
-- 失败也只让对应能力为 nil，调用方一律回退 ANSI io.open / os.execute，绝不影响主流程。
-- 之所以用 ShellExecuteW 而非 CreateProcessW：无需 STARTUPINFO/PROCESS_INFORMATION
-- 结构体，崩溃面更小，且已在 SubFix.lua 的 AI 链路长期验证；二者都走 UTF-16，均满足
-- “宽字符启动 + pcall + ANSI 回退”的目标。
do
    if SUBFIX_IS_WINDOWS and _subfix_ffi then
        local F = _subfix_ffi
        local function cdef_safe(decl) pcall(function() F.cdef(decl) end) end
        cdef_safe[[unsigned long GetLastError(void);]]
        cdef_safe[[void Sleep(unsigned long dwMilliseconds);]]
        cdef_safe[[void* ShellExecuteW(void* hwnd, const wchar_t* lpOperation, const wchar_t* lpFile, const wchar_t* lpParameters, const wchar_t* lpDirectory, int nShowCmd);]]
        cdef_safe[[void* CreateFileW(const wchar_t* lpFileName, unsigned long dwDesiredAccess, unsigned long dwShareMode, void* lpSecurityAttributes, unsigned long dwCreationDisposition, unsigned long dwFlagsAndAttributes, void* hTemplateFile);]]
        cdef_safe[[int WriteFile(void* hFile, const char* lpBuffer, unsigned long nNumberOfBytesToWrite, unsigned long* lpNumberOfBytesWritten, void* lpOverlapped);]]
        cdef_safe[[int ReadFile(void* hFile, void* lpBuffer, unsigned long nNumberOfBytesToRead, unsigned long* lpNumberOfBytesRead, void* lpOverlapped);]]
        cdef_safe[[unsigned long GetFileSize(void* hFile, unsigned long* lpFileSizeHigh);]]
        cdef_safe[[int CloseHandle(void* hObject);]]
        cdef_safe[[int CreateDirectoryW(const wchar_t* lpPathName, void* lpSecurityAttributes);]]
        cdef_safe[[int DeleteFileW(const wchar_t* lpFileName);]]
        local C = F.C

        -- 直接索引 ffi.C 里“已 cdef 但当前进程未加载对应 DLL”的符号会立即抛错（而非返回
        -- nil，实测 ffi.C.ShellExecuteW 在未加载 shell32 的宿主里就会抛）。统一用 pcall
        -- 安全取符号，解析不到就是 nil；kernel32 系列恒可用，ShellExecuteW 额外 load shell32。
        local function sym(name)
            local value
            pcall(function() value = C[name] end)
            return value
        end
        local CreateFileWFn = sym("CreateFileW")
        local WriteFileFn = sym("WriteFile")
        local ReadFileFn = sym("ReadFile")
        local GetFileSizeFn = sym("GetFileSize")
        local CloseHandleFn = sym("CloseHandle")
        local CreateDirectoryWFn = sym("CreateDirectoryW")
        local DeleteFileWFn = sym("DeleteFileW")
        local GetLastErrorFn = sym("GetLastError")
        local SleepFn = sym("Sleep")
        local ShellExecuteWFn = sym("ShellExecuteW")
        if not ShellExecuteWFn then
            local ok32, shell32 = pcall(function() return F.load("shell32") end)
            if ok32 and shell32 then
                pcall(function() ShellExecuteWFn = shell32.ShellExecuteW end)
            end
        end

        -- 多宽字符参数专用：把每个底层数组锚定在 anchors 表中并返回对应指针。
        -- ffi.cast 的指针不持有 ffi.new 的数组，连续构造多个 wptr 时前一个可能被 GC
        -- 回收形成悬垂指针；多参数调用（ShellExecuteW）必须用本函数，调用方以 local
        -- 持有 anchors 覆盖整个 C 调用。
        local function wargs(...)
            local anchors, ptrs = {}, {}
            for i, s in ipairs({ ... }) do
                local bytes = subfix_utf8_to_utf16le(tostring(s or ""))
                local buffer = F.new("char[?]", #bytes + 2)
                F.copy(buffer, bytes .. "\0\0", #bytes + 2)
                anchors[i] = buffer
                ptrs[i] = F.cast("wchar_t*", buffer)
            end
            return anchors, ptrs
        end

        -- 单宽字符参数的安全调用包装。ffi.cast 出的指针不持有 ffi.new 数组：
        -- 若「先构造指针返回、再在外层发起原生调用」，两次调用之间解释器的 GC
        -- 进度点（函数 call/return 边界）可能回收该数组，原生函数随即读写已被
        -- 重新分配的内存，踩坏 LuaJIT 的 C 堆（lj_alloc unlink 处随机 c0000005）。
        -- 本函数把数组锚定在自己的栈帧 local 上，覆盖整个 fn(ptr, ...) 调用，
        -- 原生调用返回前数组绝不可能被回收。所有单路径参数的 W 系列 API 必须走它。
        local function wcall(fn, s, ...)
            if not fn then return nil end
            local bytes = subfix_utf8_to_utf16le(tostring(s or ""))
            local buffer = F.new("char[?]", #bytes + 2)
            F.copy(buffer, bytes .. "\0\0", #bytes + 2)
            return fn(F.cast("wchar_t*", buffer), ...)
        end

        if CreateFileWFn and WriteFileFn and CloseHandleFn then
            -- 精确字节写出。返回 pcall 的真假。
            subfix_winio.write = function(path, data)
                local p, payload = tostring(path or ""), tostring(data or "")
                if p == "" then return false end
                return pcall(function()
                    local invalid = F.cast("void*", -1)
                    local handle = wcall(CreateFileWFn, p, 0x40000000, 0x00000001, nil, 2, 0x00000080, nil)
                    if handle == nil or handle == invalid then error("CreateFileW failed") end
                    local written_dword = F.new("unsigned long[1]")
                    local total, length = 0, #payload
                    while total < length do
                        local chunk = payload:sub(total + 1, math.min(total + 262144, length))
                        if WriteFileFn(handle, chunk, #chunk, written_dword, nil) == 0 then
                            CloseHandleFn(handle)
                            error("WriteFile failed")
                        end
                        local n = tonumber(written_dword[0]) or 0
                        if n <= 0 then break end
                        total = total + n
                    end
                    CloseHandleFn(handle)
                end)
            end
        end

        if CreateFileWFn and ReadFileFn and GetFileSizeFn and CloseHandleFn then
            -- 读取整个文件为字节串；失败返回 nil。
            subfix_winio.read = function(path)
                local p = tostring(path or "")
                if p == "" then return nil end
                local ok, data = pcall(function()
                    local invalid = F.cast("void*", -1)
                    local handle = wcall(CreateFileWFn, p, 0x80000000, 0x00000003, nil, 3, 0x00000080, nil)
                    if handle == nil or handle == invalid then return nil end
                    local size = tonumber(GetFileSizeFn(handle, nil)) or 0
                    local buffer = F.new("char[?]", math.max(size, 1))
                    local got_dword = F.new("unsigned long[1]")
                    local total = 0
                    while total < size do
                        if ReadFileFn(handle, buffer + total, size - total, got_dword, nil) == 0 then break end
                        local n = tonumber(got_dword[0]) or 0
                        if n <= 0 then break end
                        total = total + n
                    end
                    CloseHandleFn(handle)
                    return F.string(buffer, total)
                end)
                if ok then return data end
                return nil
            end
        end

        if DeleteFileWFn then
            subfix_winio.remove = function(path)
                pcall(function() wcall(DeleteFileWFn, path) end)
            end
        end

        if CreateDirectoryWFn then
            -- 目录已存在(ERROR_ALREADY_EXISTS=183)也算成功，其余错误返回 false 让调用方回退。
            subfix_winio.mkdir = function(path)
                local p = tostring(path or "")
                if p == "" then return false end
                local ok, result = pcall(function()
                    if wcall(CreateDirectoryWFn, p, nil) ~= 0 then return true end
                    local err = GetLastErrorFn and tonumber(GetLastErrorFn()) or 0
                    return err == 183
                end)
                return ok and result == true
            end
        end

        if ShellExecuteWFn then
            -- open 动词 + SW_HIDE(0)：UTF-16 启动，不经过 ANSI cmd，不弹窗。
            -- 四个宽字符参数的底层数组由 anchors 锚定，避免连续构造时被 GC 回收。
            -- 必须校验返回值：ShellExecuteW 成功返回 >32 的伪句柄；<=32 是错误码
            -- （如 2=找不到文件、3=路径不存在、5=拒绝访问），旧代码只看 pcall，
            -- 启动失败也报成功，调用方会傻等到超时。
            subfix_winio.hidden_shell = function(program, parameters)
                local launch_result
                local ok = pcall(function()
                    local anchors, wp = wargs("open", program, parameters or "", "")
                    launch_result = ShellExecuteWFn(nil, wp[1], wp[2], wp[3], wp[4], 0)
                end)
                if not ok then return false end
                return (tonumber(launch_result) or 0) > 32
            end
        end

        if SleepFn then
            -- 让出当前线程（毫秒），替代忙转，避免占满一个 CPU 核心。
            subfix_winio.sleep = function(milliseconds)
                local ms = math.max(0, math.floor(tonumber(milliseconds) or 0))
                pcall(function() SleepFn(ms) end)
            end
        end
    end
end

-- Windows 同步隐藏执行：落盘 UTF-8 .cmd（chcp 65001）+ 带 BOM 的 -Wait .ps1，
-- 用 ShellExecuteW 宽字符启动后轮询 done 标记（宽字符判定），最多等 timeout_seconds。
-- 供 qwen 状态检测等必须立刻拿结果、且 python/输出路径可能含中文的场景。
-- 返回：true=已完成；nil=已发起但超时；false=前置准备失败（调用方应回退 ANSI os.execute）。
local function windows_run_sync_hidden(command, stdout_file, timeout_seconds)
    if not (SUBFIX_IS_WINDOWS and subfix_winio.hidden_shell) then return false end
    local cmd_text, sink = tostring(command or ""), tostring(stdout_file or "")
    if cmd_text == "" or sink == "" then return false end
    local runner_path, ps1_path, done_file = sink .. ".cmd", sink .. ".ps1", sink .. ".done"
    local runner_content = "@echo off\r\n"
        .. "chcp 65001 >nul\r\n"
        .. 'set "PYTHONHOME="\r\n'
        .. 'set "PYTHONPATH="\r\n'
        .. 'set "PYTHONIOENCODING=utf-8"\r\n'
        .. cmd_text .. " > " .. subfix_dq(sink) .. " 2>&1\r\n"
        .. "echo done > " .. subfix_dq(done_file) .. "\r\n"
    -- .ps1 加 UTF-8 BOM，避免 Windows PowerShell 5.1 按 ANSI 读坏其中的中文路径。
    local ps1_content = "\239\187\191"
        .. "Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c','" .. runner_path
        .. "') -WindowStyle Hidden -Wait\r\n"
    if not (write_binary_file(runner_path, runner_content) and write_binary_file(ps1_path, ps1_content)) then
        return false
    end
    local powershell = tostring(os.getenv("SystemRoot") or "C:\\Windows")
        .. "\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
    local launch_ok = subfix_winio.hidden_shell(powershell, "-NoProfile -ExecutionPolicy Bypass -File " .. subfix_dq(ps1_path))
    -- ShellExecuteW 的返回值在达芬奇内嵌 LuaJIT 下可能误报 <=32（进程实际已启动，
    -- 详见 launch_background_windows 的注释）。误报时若立即返回 false，调用方会再走
    -- ANSI 回退，造成同一检测命令并发两遍。这里以 runner 重定向文件（启动瞬间创建）或
    -- done 标记为佐证：3 秒内出现任一即确认宽字符链路已启动。
    if not launch_ok then
        local proof_deadline = (os.time() or 0) + 3
        while (os.time() or 0) < proof_deadline do
            if file_exists(done_file) or file_exists(sink) then
                launch_ok = true
                break
            end
            if subfix_winio.sleep then
                subfix_winio.sleep(100)
            else
                local until_clock = (os.clock() or 0) + 0.1
                while (os.clock() or 0) < until_clock do end
            end
        end
    end
    if not launch_ok then
        return false
    end
    local deadline = (os.time() or 0) + math.max(1, tonumber(timeout_seconds) or 60)
    while (os.time() or 0) < deadline do
        if file_exists(done_file) then return true end
        -- 用 kernel32 Sleep 让出线程等待约 100ms，绝不忙转占满 CPU；
        -- 也避免“宽字符 file_exists + 高频循环”在个别 LuaJIT 构建上被 JIT 误编译。
        if subfix_winio.sleep then
            subfix_winio.sleep(100)
        else
            local until_clock = (os.clock() or 0) + 0.05
            while (os.clock() or 0) < until_clock do end
        end
    end
    if file_exists(done_file) then return true end
    return nil
end

-- 该函数含“轮询 + 宽字符存在性判定”的热循环。Resolve 自带 LuaJIT 对这种与外部进程并存
-- 的热 FFI 路径在个别情况下会编译出错误代码，故在“定义时（热点形成前）”就将其排除出
-- JIT；解释执行足以胜任这种低频、毫秒级轮询，且不影响宿主其它代码继续走 JIT。
do
    local ok_jit, jit_mod = pcall(require, "jit")
    if ok_jit and jit_mod then
        pcall(function() jit_mod.off(windows_run_sync_hidden) end)
    end
end

local function register_ui_timer(timer, handler)
    if not timer or type(handler) ~= "function" then return false end
    local timer_id = tostring(timer.ID or "")
    if timer_id == "" then return false end
    -- embedded 模式注册进宿主路由表（宿主 dispatcher 的 On.Timeout 统一分发）；
    -- standalone 模式注册进本模块的路由表。
    if EMBEDDED and type(host_env) == "table" and type(host_env.register_timer) == "function" then
        return host_env.register_timer(timer, handler) == true
    end
    ui_timer_handlers[timer_id] = handler
    return true
end

-- 注销 timer 路由：embedded 模式必须同时清掉宿主表中的闭包，否则高频 yield/poll timer
-- （每次生成成千上万次）会在宿主 ui_timer_handlers 里堆积死闭包。
local function unregister_ui_timer(timer_or_id)
    local timer_id = ""
    if type(timer_or_id) == "string" then
        timer_id = timer_or_id
    elseif timer_or_id ~= nil then
        timer_id = tostring(timer_or_id.ID or "")
    end
    if timer_id == "" then return end
    if EMBEDDED and type(host_env) == "table" and type(host_env.unregister_timer) == "function" then
        pcall(host_env.unregister_timer, timer_id)
        return
    end
    ui_timer_handlers[timer_id] = nil
end

local function basename(path)
    local text = tostring(path or "")
    return text:match("([^/\\]+)$") or text
end

local function script_dir()
    local source = debug and debug.getinfo and debug.getinfo(1, "S").source or ""
    source = tostring(source or ""):gsub("^@", "")
    local dir = source:match("^(.*[/\\])")
    if dir and dir ~= "" then
        return dir:gsub("[/\\]$", "")
    end
    return os.getenv("PWD") or "."
end

local function configured_script_root()
    local root = runtime_options and runtime_options.script_root
    root = tostring(root or "")
    if root ~= "" then
        return root:gsub("[/\\]$", "")
    end
    return script_dir()
end

local function resolve_asr_paths()
    local root = configured_script_root()
    local helper_dir = subfix_pjoin(root, ".subfix_support")
    local setup_ext = SUBFIX_IS_WINDOWS and ".ps1" or ".sh"
    local utility = subfix_dr_utility_root()
    local user_support_dir = utility.user and subfix_support_dir(utility.user) or helper_dir
    local user_python = utility.user and subfix_venv_python(user_support_dir) or ""
    local user_runtime_python = utility.user and subfix_runtime_python(user_support_dir) or ""
    local paths = {
        helper = subfix_pjoin(helper_dir, "subfix_asr_transcribe.py"),
        qwen_manager = subfix_pjoin(helper_dir, "subfix_qwen_local_manager.py"),
        process_group = subfix_pjoin(helper_dir, "subfix_process_group.py"),
        setup = subfix_pjoin(helper_dir, "setup_asr_env" .. setup_ext),
        python = subfix_venv_python(helper_dir),
        runtime_python = subfix_runtime_python(helper_dir),
        diagnostic = subfix_pjoin(user_support_dir, "last_generate_diagnostic.json"),
        hotwords = subfix_pjoin(user_support_dir, "hotwords.json")
    }
    if not file_exists(paths.helper) and file_exists(subfix_pjoin(root, "subfix_asr_transcribe.py")) then
        paths.helper = subfix_pjoin(root, "subfix_asr_transcribe.py")
        paths.qwen_manager = subfix_pjoin(root, "subfix_qwen_local_manager.py")
        paths.process_group = subfix_pjoin(root, "subfix_process_group.py")
        paths.setup = subfix_pjoin(root, "setup_asr_env" .. setup_ext)
        paths.python = subfix_venv_python(root)
        paths.runtime_python = subfix_runtime_python(root)
    end
    local module_dir = script_dir()
    if not file_exists(paths.helper) and file_exists(subfix_pjoin(module_dir, "subfix_asr_transcribe.py")) then
        paths.helper = subfix_pjoin(module_dir, "subfix_asr_transcribe.py")
        paths.qwen_manager = subfix_pjoin(module_dir, "subfix_qwen_local_manager.py")
        paths.process_group = subfix_pjoin(module_dir, "subfix_process_group.py")
        paths.setup = subfix_pjoin(module_dir, "setup_asr_env" .. setup_ext)
        paths.python = subfix_venv_python(module_dir)
        paths.runtime_python = subfix_runtime_python(module_dir)
    end
    if not file_exists(paths.python) and file_exists(user_python) then
        paths.python = user_python
    end
    if not file_exists(paths.runtime_python) and file_exists(user_runtime_python) then
        paths.runtime_python = user_runtime_python
    end
    return paths
end

local function build_qwen_status_command(paths, output_path)
    if not paths or not file_exists(paths.runtime_python) or not file_exists(paths.qwen_manager) then
        return nil, "未找到本地 Qwen 安装管理器"
    end
    return table.concat({
        shell_quote(paths.runtime_python), "-B", shell_quote(paths.qwen_manager),
        "--action", "status", "--output", shell_quote(output_path),
    }, " "), nil
end

-- P5：本地 Qwen 状态检测缓存 + 后台预热。整体封进 do 块，主 chunk 只占 1 个 local
-- （qwen_check 表），避免触碰 Lua 5.1「主函数最多 200 个 local」上限。
-- qwen_check.inspect(paths, force)：8 秒 TTL 缓存；force=true 强制复检。
-- qwen_check.preheat()：attach 时隐藏进程异步预热；qwen_check.finish()：停止轮询。
local qwen_check
do
local qwen_inspect_cache = { result = nil, checked_at = -100, signature = nil }
local QWEN_INSPECT_TTL_SECONDS = 8
local qwen_lfs_ok, qwen_lfs = pcall(require, "lfs")

local function qwen_paths_signature(paths)
    local keys = {}
    for key, value in pairs(paths or {}) do
        if type(value) == "string" then
            keys[#keys + 1] = key
        end
    end
    table.sort(keys)
    local parts = {}
    for _, key in ipairs(keys) do
        local value = paths[key]
        local exists = file_exists(value)
        local mtime = ""
        if exists and qwen_lfs_ok and qwen_lfs then
            local ok_attr, attrs = pcall(function() return qwen_lfs.attributes(value) end)
            if ok_attr and type(attrs) == "table" then
                mtime = tostring(attrs.modification or "")
            end
        end
        parts[#parts + 1] = key .. "=" .. value .. "|exists=" .. tostring(exists) .. "|mtime=" .. mtime
    end
    return table.concat(parts, "\n")
end

local function inspect_local_qwen_uncached(paths)
    local temporary_root = temp_dir()
    local output_path = temporary_root .. "/subfix_qwen_status_" .. tostring(os.time()) .. "_" .. tostring(math.random(100000, 999999)) .. ".json"
    local cmd = build_qwen_status_command(paths, output_path)
    if not cmd then return {state = "missing", ready = false} end
    if SUBFIX_IS_WINDOWS then
        -- 优先走宽字符隐藏执行（UTF-8 .cmd + chcp 65001 + ShellExecuteW）：
        -- python/管理器/输出路径含中文或空格都能正确传递，且不弹控制台。
        -- 批处理内以 call 引导带引号的程序路径，避免 cmd 剥掉首尾引号把路径劈断。
        local sink = output_path .. ".sync.log"
        local ran = windows_run_sync_hidden("call " .. cmd, sink, 60)
        if ran == false then
            -- 宽字符链路不可用：回退原 ANSI system()（cmd /c call ...）路径。
            os.execute("call " .. cmd .. " >nul 2>nul")
        elseif ran == nil then
            print("[SubFix Generate] 本地 Qwen 状态检测宽字符执行超时：" .. tostring(cmd))
        end
        for _, suffix in ipairs({ "", ".cmd", ".ps1", ".done" }) do
            local target = sink .. suffix
            pcall(function() os.remove(target) end)
            if file_exists(target) and subfix_winio.remove then subfix_winio.remove(target) end
        end
    else
        os.execute(cmd .. " >/dev/null 2>&1")
    end
    local payload = decode_json_text(read_text_file(output_path) or "")
    pcall(function() os.remove(output_path) end)
    if file_exists(output_path) and subfix_winio.remove then subfix_winio.remove(output_path) end
    if type(payload) ~= "table" then return {state = "missing", ready = false} end
    payload.ready = payload.ready == true
    return payload
end

-- 带缓存的检测入口：force=true 时无条件同步复检（用于安装成功后的校验）。
local function inspect_local_qwen(paths, force)
    local signature = qwen_paths_signature(paths)
    local now = os.time()
    if force ~= true
        and type(qwen_inspect_cache.result) == "table"
        and qwen_inspect_cache.signature == signature
        and (now - (tonumber(qwen_inspect_cache.checked_at) or -100)) < QWEN_INSPECT_TTL_SECONDS then
        return qwen_inspect_cache.result
    end
    local result = inspect_local_qwen_uncached(paths)
    qwen_inspect_cache.result = result
    qwen_inspect_cache.checked_at = now
    qwen_inspect_cache.signature = signature
    return result
end

-- P5：后台预热 Qwen 状态。attach 面板时把状态检测命令以隐藏进程异步发起，
-- 轮询 done 标记，完成后直接回填缓存——此后任何同步 inspect_local_qwen 都零阻塞命中缓存。
-- 仅 Windows + 宽字符隐藏执行可用时预热；任何前置失败静默返回 false，调用方按原同步路径走。
local qwen_preheat = {
    running = false,
    timer = nil,
    output = nil,
    sink = nil,
    done = nil,
    signature = nil,
    deadline = 0,
}

local function qwen_preheat_remove_files()
    local candidates = {
        qwen_preheat.output,
        qwen_preheat.sink,
        qwen_preheat.sink and (qwen_preheat.sink .. ".cmd"),
        qwen_preheat.sink and (qwen_preheat.sink .. ".ps1"),
        qwen_preheat.done,
    }
    for _, target in ipairs(candidates) do
        if target then
            pcall(function() os.remove(target) end)
            if file_exists(target) and subfix_winio.remove then
                pcall(subfix_winio.remove, target)
            end
        end
    end
end

local function qwen_preheat_finish()
    if qwen_preheat.timer then
        pcall(function() qwen_preheat.timer:Stop() end)
        unregister_ui_timer(qwen_preheat.timer)
        qwen_preheat.timer = nil
    end
    qwen_preheat_remove_files()
    qwen_preheat.running = false
    qwen_preheat.output = nil
    qwen_preheat.sink = nil
    qwen_preheat.done = nil
    qwen_preheat.signature = nil
end

local function preheat_local_qwen_status()
    if not (SUBFIX_IS_WINDOWS and subfix_winio and subfix_winio.hidden_shell and ui) then
        return false
    end
    if qwen_preheat.running then
        return true
    end
    local paths = resolve_asr_paths()
    local signature = qwen_paths_signature(paths)
    local now = os.time()
    if type(qwen_inspect_cache.result) == "table"
        and qwen_inspect_cache.signature == signature
        and (now - (tonumber(qwen_inspect_cache.checked_at) or -100)) < QWEN_INSPECT_TTL_SECONDS then
        return true
    end
    local temporary_root = temp_dir()
    local uid = tostring(os.time()) .. "_" .. tostring(math.random(100000, 999999))
    local output_path = temporary_root .. "/subfix_qwen_status_preheat_" .. uid .. ".json"
    local cmd = build_qwen_status_command(paths, output_path)
    if not cmd then
        return false
    end
    local sink = output_path .. ".sync.log"
    local runner_path, ps1_path, done_file = sink .. ".cmd", sink .. ".ps1", sink .. ".done"
    -- 与 windows_run_sync_hidden 相同的 UTF-8 宽字符启动套路，但 ps1 不加 -Wait：
    -- powershell 启动隐藏 cmd 后立即退出，不阻塞 UI 线程。
    local runner_content = "@echo off\r\n"
        .. "chcp 65001 >nul\r\n"
        .. 'set "PYTHONHOME="\r\n'
        .. 'set "PYTHONPATH="\r\n'
        .. 'set "PYTHONIOENCODING=utf-8"\r\n'
        .. "call " .. cmd .. " > " .. subfix_dq(sink) .. " 2>&1\r\n"
        .. "echo done > " .. subfix_dq(done_file) .. "\r\n"
    local ps1_content = "\239\187\191"
        .. "Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c','" .. runner_path
        .. "') -WindowStyle Hidden\r\n"
    if not (write_binary_file(runner_path, runner_content) and write_binary_file(ps1_path, ps1_content)) then
        return false
    end
    local powershell = tostring(os.getenv("SystemRoot") or "C:\\Windows")
        .. "\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
    local launch_ok = subfix_winio.hidden_shell(powershell, "-NoProfile -ExecutionPolicy Bypass -File " .. subfix_dq(ps1_path))
    if not launch_ok then
        qwen_preheat_remove_files()
        return false
    end

    qwen_preheat.running = true
    qwen_preheat.output = output_path
    qwen_preheat.sink = sink
    qwen_preheat.done = done_file
    qwen_preheat.signature = signature
    qwen_preheat.deadline = (os.time() or 0) + 90

    local timer_id = "SubFixQwenPreheatTimer_" .. uid
    local timer = ui:Timer({ ID = timer_id, Interval = 500, SingleShot = false })
    qwen_preheat.timer = timer
    local registered = register_ui_timer(timer, function()
        if not qwen_preheat.running then
            return
        end
        if file_exists(qwen_preheat.done) then
            local payload = decode_json_text(read_text_file(qwen_preheat.output) or "")
            if type(payload) == "table" then
                payload.ready = payload.ready == true
                qwen_inspect_cache.result = payload
                qwen_inspect_cache.checked_at = os.time()
                qwen_inspect_cache.signature = qwen_preheat.signature
            end
            qwen_preheat_finish()
            return
        end
        if (os.time() or 0) > qwen_preheat.deadline then
            print("[SubFix Generate] Qwen 状态后台预热超时，放弃本次预热")
            qwen_preheat_finish()
        end
    end)
    if not registered then
        qwen_preheat_finish()
        return false
    end
    local started_ok = pcall(function() timer:Start() end)
    if not started_ok then
        qwen_preheat_finish()
        return false
    end
    return true
end

qwen_check = {
    inspect = inspect_local_qwen,
    preheat = preheat_local_qwen_status,
    finish = qwen_preheat_finish,
}
end

local function build_qwen_install_command(paths, output_path, progress_path)
    if not file_exists(paths.runtime_python) then return nil, "未找到 SubFix 内置 Python" end
    if not file_exists(paths.qwen_manager) then return nil, "缺少本地 Qwen 安装管理器" end
    return table.concat({
        shell_quote(paths.runtime_python), "-B", shell_quote(paths.qwen_manager),
        "--action", "install", "--output", shell_quote(output_path),
        "--progress-json", shell_quote(progress_path),
    }, " "), nil
end

-- 豆包（火山引擎）云端 ASR 密钥文件路径：必须与「实际被执行的」
-- subfix_asr_transcribe.py 同目录，才能与 Python 端
-- _doubao_credentials_path()（Path(__file__).with_name(...)）指向一致，
-- 保证 UI 写盘后立即可读、无需再跑 sync。
local function doubao_credentials_file_path()
    local helper = tostring(resolve_asr_paths().helper or "")
    -- 同时识别 / 与 \：Windows 下 helper 可能是反斜杠路径，旧写法只匹配 "/" 会截取失败。
    local dir = helper:gsub("[/\\][^/\\]*$", "")
    if dir == "" or dir == helper then
        dir = subfix_pjoin(configured_script_root(), ".subfix_support")
    end
    return subfix_pjoin(dir, "doubao_credentials.json")
end

-- 读回已存 API Key（缺失/解析失败/字段空 → 返回空串），用于单字段预填。
local function read_doubao_api_key()
    local text = read_text_file(doubao_credentials_file_path())
    if not text or text == "" then return "" end
    local data = decode_json_text(text)
    if type(data) ~= "table" then return "" end
    return trim_text(tostring(data.api_key or ""))
end

-- 是否已配置豆包密钥：仅接受 API Key，旧 appid/token 永不视为有效。
local function doubao_credentials_configured()
    local env_api_key = trim_text(os.getenv("SUBFIX_DOUBAO_API_KEY") or "")
    if env_api_key ~= "" then return true end
    local api_key = read_doubao_api_key()
    return api_key ~= ""
end

-- 保存单 API Key；覆盖旧格式，防止界面再次回到两字段。
local function save_doubao_api_key(api_key)
    local content = string.format('{\n  "api_key": "%s"\n}\n', json_escape(api_key))
    local path = doubao_credentials_file_path()
    if not write_text_file(path, content) then
        return false, "无法写入密钥文件: " .. path
    end
    return true
end

-- 生成偏好文件（记住上次选的识别模型等），与 helper 同级。
local function generate_prefs_file_path()
    local helper = tostring(resolve_asr_paths().helper or "")
    -- 同时识别 / 与 \：Windows 下 helper 可能是反斜杠路径，旧写法只匹配 "/" 会截取失败。
    local dir = helper:gsub("[/\\][^/\\]*$", "")
    if dir == "" or dir == helper then
        dir = subfix_pjoin(configured_script_root(), ".subfix_support")
    end
    return subfix_pjoin(dir, "subfix_generate_prefs.json")
end

local function normalize_doubao_backend(backend)
    if trim_text(tostring(backend or "")) == "doubao_asr_v2" then
        return "doubao_asr_v2"
    end
    return "doubao_asr"
end

local function read_generate_preferences()
    local text = read_text_file(generate_prefs_file_path())
    local data = text and text ~= "" and decode_json_text(text) or nil
    return type(data) == "table" and data or {}
end

-- 读上次选择的识别模型 backend（无偏好/解析失败 → nil）。
local function read_last_engine_backend()
    local data = read_generate_preferences()
    local b = trim_text(tostring(data.last_engine_backend or ""))
    return b ~= "" and b or nil
end

-- 兼容旧偏好：V2 保留，其余值（含缺失/未知）默认极速版。
local function read_doubao_backend_preference()
    local data = read_generate_preferences()
    return normalize_doubao_backend(data.doubao_backend or data.last_engine_backend)
end

local function write_generate_preferences(data)
    data = type(data) == "table" and data or {}
    local last_engine_backend = trim_text(tostring(data.last_engine_backend or ""))
    local doubao_backend = normalize_doubao_backend(data.doubao_backend)
    local content = string.format(
        '{\n  "last_engine_backend": "%s",\n  "doubao_backend": "%s"\n}\n',
        json_escape(last_engine_backend),
        json_escape(doubao_backend)
    )
    return write_text_file(generate_prefs_file_path(), content)
end

-- 记住本次选择的识别模型 backend，同时固化旧偏好中的豆包版本。
local function save_last_engine_backend(backend)
    local data = read_generate_preferences()
    data.doubao_backend = read_doubao_backend_preference()
    data.last_engine_backend = backend
    return write_generate_preferences(data)
end

local function save_doubao_backend_preference(backend)
    local data = read_generate_preferences()
    data.doubao_backend = normalize_doubao_backend(backend)
    return write_generate_preferences(data)
end

local function load_generate_hotword_entries()
    local text = read_text_file(resolve_asr_paths().hotwords)
    local payload = text and decode_json_text(text) or nil
    local raw_entries = type(payload) == "table" and payload.entries or {}
    if type(raw_entries) ~= "table" then return {} end
    local entries, seen_terms = {}, {}
    for _, raw_entry in ipairs(raw_entries) do
        if type(raw_entry) == "table" then
            local term = trim_text(tostring(raw_entry.term or ""))
            if term ~= "" and not seen_terms[term] then
                local aliases, seen_aliases = {}, {}
                if type(raw_entry.aliases) == "table" then
                    for _, raw_alias in ipairs(raw_entry.aliases) do
                        local alias = trim_text(tostring(raw_alias or ""))
                        if alias ~= "" and alias ~= term and not seen_aliases[alias] then
                            aliases[#aliases + 1] = alias
                            seen_aliases[alias] = true
                        end
                    end
                end
                entries[#entries + 1] = {term = term, aliases = aliases}
                seen_terms[term] = true
            end
        end
    end
    return entries
end

local function save_generate_hotword_entries(entries)
    local path = resolve_asr_paths().hotwords
    local parts = {}
    for _, entry in ipairs(entries or {}) do
        local aliases = {}
        for _, alias in ipairs(entry.aliases or {}) do
            aliases[#aliases + 1] = '"' .. json_escape(alias) .. '"'
        end
        parts[#parts + 1] = string.format(
            '{"term":"%s","aliases":[%s]}',
            json_escape(entry.term),
            table.concat(aliases, ",")
        )
    end
    return write_text_file(path, '{"version":1,"entries":[' .. table.concat(parts, ",") .. "]}\n")
end

local function parse_fps(value)
    local text = tostring(value or "")
    if text == "29.97" then return 30000 / 1001 end
    if text == "23.976" or text == "23.98" then return 24000 / 1001 end
    if text == "59.94" then return 60000 / 1001 end
    local fps = tonumber(value)
    if fps and fps > 0 then return fps end
    print(string.format("[SubFix Generate] 无法解析时间线帧率 %q，回退 30.0", text))
    return 30.0
end

local function frames_to_srt_time(frame, fps, base_frame)
    local rel_frame = math.max(0, (tonumber(frame) or 0) - (tonumber(base_frame) or 0))
    local ms = math.floor((rel_frame / math.max(1, tonumber(fps) or 30)) * 1000 + 0.5)
    local hours = math.floor(ms / 3600000)
    ms = ms % 3600000
    local minutes = math.floor(ms / 60000)
    ms = ms % 60000
    local seconds = math.floor(ms / 1000)
    local millis = ms % 1000
    return string.format("%02d:%02d:%02d,%03d", hours, minutes, seconds, millis)
end

local function timecode_to_frame(value, fps)
    -- 捕获第 4 个分隔符：";" 表示丢帧时间码（29.97 DF / 59.94 DF）。
    local hh, mm, ss, sep, ff = tostring(value or ""):match("^(%d+):(%d+):(%d+)([:;])(%d+)$")
    if not hh then return nil end
    local rate = math.max(1, tonumber(fps) or 30)
    local h, m, s, f = tonumber(hh) or 0, tonumber(mm) or 0, tonumber(ss) or 0, tonumber(ff) or 0
    if sep == ";" and (
        math.abs(rate - 30000 / 1001) < 0.02 or math.abs(rate - 60000 / 1001) < 0.02
    ) then
        -- 标准 SMPTE 丢帧换算：
        --   dropFrames = round(rate/15)（29.97→2，59.94→4），timeBase = round(rate)
        --   frameNumber = timeBase*60*总分钟数
        --                - dropFrames*9*floor(总分钟数/10)
        --                - dropFrames*(总分钟数%10)
        --                + 秒*timeBase + 帧号
        local time_base = math.floor(rate + 0.5)
        local drop_frames = math.floor(rate / 15 + 0.5)
        local total_minutes = h * 60 + m
        return time_base * 60 * total_minutes
            - drop_frames * 9 * math.floor(total_minutes / 10)
            - drop_frames * (total_minutes % 10)
            + s * time_base + f
    end
    local total_seconds = h * 3600 + m * 60 + s
    return math.floor(total_seconds * rate + f + 0.5)
end

-- Resolve 的 TimelineItem:GetStart()/GetEnd() 在不同版本/平台可能返回帧数或时间码字符串。
-- 统一转成帧：数值直接用，时间码用 timecode_to_frame。这样与 read_generation_scope
-- 通过 normalize_mark_frame 算出的选区帧处于同一坐标系（时间线起点为 0 时二者一致）。
local function subfix_item_frame_bounds(item, fps)
    local ok_s, s = pcall(function() return item:GetStart() end)
    local ok_e, e = pcall(function() return item:GetEnd() end)
    s = ok_s and (tonumber(s) or timecode_to_frame(s, fps)) or nil
    e = ok_e and (tonumber(e) or timecode_to_frame(e, fps)) or nil
    return s, e
end

local function get_resolve()
    if resolve then return resolve end
    if bmd and bmd.scriptapp then
        return bmd.scriptapp("Resolve")
    end
    return nil
end

local function get_mark_value(mark, kind)
    if type(mark) ~= "table" then return nil end
    local keys = kind == "in"
        and {"in", "markIn", "mark_in", "MarkIn", "start", "Start", "startFrame", "start_frame", 1}
        or {"out", "markOut", "mark_out", "MarkOut", "end", "End", "endFrame", "end_frame", 2}
    for _, key in ipairs(keys) do
        if mark[key] ~= nil then return mark[key] end
    end
    return nil
end

local function normalize_mark_frame(value, timeline_start, timeline_end, fps)
    local parsed_from_timecode = false
    local frame = tonumber(value)
    if not frame then
        frame = timecode_to_frame(value, fps)
        parsed_from_timecode = frame ~= nil
    end
    if not frame then return nil end
    local start_frame = tonumber(timeline_start) or 0
    local end_frame = tonumber(timeline_end)
    if parsed_from_timecode then
        if end_frame and frame >= start_frame and frame <= end_frame then
            return math.floor(frame + 0.5)
        end
        local shifted_timecode_frame = frame + start_frame
        if end_frame and shifted_timecode_frame >= start_frame and shifted_timecode_frame <= end_frame then
            return math.floor(shifted_timecode_frame + 0.5)
        end
        return math.floor(frame + 0.5)
    end
    local shifted_frame = frame + start_frame

    -- Resolve may return In/Out as elapsed timeline frames on timelines that start
    -- at 01:00:00. Prefer the shifted candidate when it still lands on the timeline;
    -- this mirrors SubFix's "absolute rows, relative SRT" writeback model and avoids
    -- the classic one-hour-left placement.
    if end_frame and start_frame > 0 and shifted_frame >= start_frame and shifted_frame <= end_frame then
        return math.floor(shifted_frame + 0.5)
    end
    if end_frame and frame >= start_frame and frame <= end_frame then
        return math.floor(frame + 0.5)
    end
    return math.floor(shifted_frame + 0.5)
end

local timeline_item_is_selected

local function get_timeline_frame_bounds(timeline)
    local ok_start, timeline_start = pcall(function() return timeline:GetStartFrame() end)
    local ok_end, timeline_end = pcall(function() return timeline:GetEndFrame() end)
    timeline_start = ok_start and tonumber(timeline_start) or 0
    timeline_end = ok_end and tonumber(timeline_end) or timeline_start
    return timeline_start, timeline_end
end

local function read_timeline_in_out_scope(timeline, fps)
    local timeline_start, timeline_end = get_timeline_frame_bounds(timeline)
    local ok_marks, marks = pcall(function() return timeline:GetMarkInOut() end)
    if not ok_marks or type(marks) ~= "table" then
        return nil, "请先用 I/O 设置 In/Out 选区"
    end
    local mark_candidates = {}
    if marks.video ~= nil then mark_candidates[#mark_candidates + 1] = marks.video end
    if marks.audio ~= nil then mark_candidates[#mark_candidates + 1] = marks.audio end
    if marks.all ~= nil then mark_candidates[#mark_candidates + 1] = marks.all end
    mark_candidates[#mark_candidates + 1] = marks
    for _, mark in ipairs(mark_candidates) do
        local mark_in = get_mark_value(mark, "in")
        local mark_out = get_mark_value(mark, "out")
        local source_label = "In/Out"
        if mark_in == nil and mark_out ~= nil then
            mark_in = timeline_start
            source_label = "In/Out (Out only)"
        elseif mark_in ~= nil and mark_out == nil then
            mark_out = timeline_end
            source_label = "In/Out (In only)"
        end
        if mark_in ~= nil and mark_out ~= nil then
            local start_frame = normalize_mark_frame(mark_in, timeline_start, timeline_end, fps)
            local end_frame = normalize_mark_frame(mark_out, timeline_start, timeline_end, fps)
            if source_label == "In/Out (Out only)" then
                start_frame = timeline_start
            elseif source_label == "In/Out (In only)" then
                end_frame = timeline_end
            end
            if start_frame and end_frame and end_frame > start_frame then
                print(string.format(
                    "[SubFix Generate] In/Out raw=%s-%s timeline_start=%s normalized=%s-%s",
                    tostring(mark_in),
                    tostring(mark_out),
                    tostring(timeline_start),
                    tostring(start_frame),
                    tostring(end_frame)
                ))
                return {
                    mode = WORK_SCOPE_MODE_SELECTION,
                    source = "in_out",
                    source_label = source_label,
                    start_frame = start_frame,
                    end_frame = end_frame,
                    timeline_start_frame = timeline_start,
                    timeline_end_frame = timeline_end
                }
            end
        end
    end
    return nil, "未读取到 O 点；请按 O 设置结束点后再生成"
end

local function read_generation_scope(timeline, fps)
    local scope, scope_err = read_timeline_in_out_scope(timeline, fps)
    if scope and scope.mode == WORK_SCOPE_MODE_SELECTION then
        return scope
    end
    -- 主信息 + \n 细节：信息 Label 按显式两行分配高度，避免长句折行溢出叠到音频树。
    return nil, tostring(scope_err or "未检测到 In/Out 选区")
        .. "\n灰色选择/鼠标框选 Resolve 脚本读不到，请用 I/O 打点后再检测"
end

local function get_audio_item_source_offset_frames(item)
    local ok_source_start, source_start_frames = pcall(function() return item:GetSourceStartFrame() end)
    local offset = ok_source_start and tonumber(source_start_frames) or nil
    if offset and offset >= 0 then return offset end
    local ok_left_offset, left_offset_frames = pcall(function() return item:GetLeftOffset(false) end)
    if not ok_left_offset then
        ok_left_offset, left_offset_frames = pcall(function() return item:GetLeftOffset() end)
    end
    offset = ok_left_offset and tonumber(left_offset_frames) or 0
    if not offset or offset < 0 then return 0 end
    return offset
end

local function get_audio_item_file_path(item)
    local ok_media, media_item = pcall(function() return item:GetMediaPoolItem() end)
    if not ok_media or not media_item then
        print("[SubFix Generate] 音频片段 GetMediaPoolItem 失败，无法定位媒体文件")
        return nil
    end
    local ok_path, raw_path = pcall(function() return media_item:GetClipProperty("File Path") end)
    if ok_path and raw_path and tostring(raw_path) ~= "" then
        local file_path = tostring(raw_path)
        if file_exists(file_path) then return file_path end
        print("[SubFix Generate] 文件存在性检查失败，原始路径=[" .. file_path .. "]")
    else
        print("[SubFix Generate] 无法读取音频片段的 File Path 属性")
    end
    return nil
end

-- TimelineItem:GetSourceStartFrame()/GetLeftOffset() return frame numbers counted in the
-- SOURCE MEDIA's own frame rate (camera clips here are 50 fps, the voice-over mp4 is 30 fps),
-- NOT the timeline frame rate (25 fps). Dividing those frame numbers by the timeline fps shifts
-- every extraction point and can push it past the file's end (silence -> missing lines).
-- Resolve the media fps from the pool item and only fall back to the timeline fps when unknown.
local function read_media_source_fps(media_item, fallback_fps)
    if media_item then
        local fps_keys = {"FPS", "Frame Rate", "Video Frame Rate", "FPS tag", "Content FPS"}
        for _, key in ipairs(fps_keys) do
            local ok, raw = pcall(function() return media_item:GetClipProperty(key) end)
            if ok and raw ~= nil then
                local num = tonumber(tostring(raw):match("[-+]?%d+%.?%d*"))
                if num and num > 0 then
                    return num
                end
            end
        end
    end
    return math.max(1, tonumber(fallback_fps) or 30)
end

local function parse_source_audio_channel_mapping(item, media_item, fps, source_offset_frames, fallback_path)
    local result = {
        file_path = fallback_path,
        audio_mapping_source = "media_pool_file",
        audio_mapping_fallback_reason = "",
        linked_offset_samples = nil,
        audio_channel_index = nil,
        audio_mapping_muted = false
    }
    if not item then
        result.audio_mapping_fallback_reason = "mapping_item_missing"
        return result
    end

    local ok_mapping, raw_mapping = pcall(function() return item:GetSourceAudioChannelMapping() end)
    if not ok_mapping or not raw_mapping or tostring(raw_mapping) == "" then
        result.audio_mapping_fallback_reason = ok_mapping and "mapping_empty" or "mapping_api_unavailable"
        return result
    end

    local mapping = decode_json_text(tostring(raw_mapping))
    if type(mapping) ~= "table" then
        result.audio_mapping_fallback_reason = "mapping_json_error"
        return result
    end

    local track_mapping = mapping.track_mapping or {}
    local mapped_track = track_mapping[tostring(1)] or track_mapping[1]
    if type(mapped_track) ~= "table" then
        for _, candidate in pairs(track_mapping) do
            if type(candidate) == "table" then
                mapped_track = candidate
                break
            end
        end
    end
    if type(mapped_track) ~= "table" then
        result.audio_mapping_fallback_reason = "track_mapping_missing"
        return result
    end

    local channel_idx = nil
    if type(mapped_track.channel_idx) == "table" then
        channel_idx = tonumber(mapped_track.channel_idx[1])
    end
    result.audio_mapping_muted = mapped_track.mute == true
        or tostring(mapped_track.mute or "") == "1"
        or tostring(mapped_track.mute or ""):lower() == "true"
    local linked_audio = mapping.linked_audio or {}
    local embedded_count = tonumber(mapping.embedded_audio_channels) or 0
    local linked_channel_number = channel_idx and (channel_idx - embedded_count) or nil
    local linked_keys = {}
    for key, _ in pairs(linked_audio) do
        linked_keys[#linked_keys + 1] = key
    end
    table.sort(linked_keys, function(a, b) return tonumber(a) < tonumber(b) end)

    local linked_info = nil
    local linked_key = nil
    local linked_local_channel_index = nil
    if linked_channel_number and linked_channel_number > 0 then
        local remaining_channel = linked_channel_number
        for _, key in ipairs(linked_keys) do
            local candidate = linked_audio[key]
            local candidate_channels = tonumber(candidate and candidate.channels) or 1
            if remaining_channel <= candidate_channels then
                linked_info = candidate
                linked_key = key
                linked_local_channel_index = remaining_channel
                break
            end
            remaining_channel = remaining_channel - candidate_channels
        end
    end
    if type(linked_info) ~= "table" or not linked_info.path or tostring(linked_info.path) == "" then
        result.audio_mapping_fallback_reason = "linked_audio_missing"
        return result
    end

    local linked_path = tostring(linked_info.path)
    if not file_exists(linked_path) then
        result.audio_mapping_fallback_reason = "linked_audio_not_found"
        return result
    end

    local sample_rate = 48000
    if media_item then
        local ok_sample_rate, raw_sample_rate = pcall(function() return media_item:GetClipProperty("Sample Rate") end)
        sample_rate = tonumber(ok_sample_rate and raw_sample_rate) or sample_rate
    end
    local linked_offset_samples = tonumber(linked_info.offset) or 0
    local effective_fps = math.max(1, tonumber(fps) or 30)
    local source_fps = read_media_source_fps(media_item, effective_fps)
    local source_frames = math.max(0, tonumber(source_offset_frames) or 0)
    result.file_path = linked_path
    result.audio_mapping_source = "linked_audio"
    result.audio_mapping_fallback_reason = ""
    result.linked_offset_samples = linked_offset_samples
    result.source_fps = source_fps
    result.source_start_seconds = math.max(0, (source_frames / source_fps) + (linked_offset_samples / math.max(1, sample_rate)))
    local linked_path_channel_index = math.max(1, tonumber(linked_local_channel_index) or 1)
    local linked_path_channel_count = 0
    for _, key in ipairs(linked_keys) do
        local candidate = linked_audio[key]
        if type(candidate) == "table" and candidate.path == linked_info.path then
            local candidate_channels = tonumber(candidate.channels) or 1
            linked_path_channel_count = linked_path_channel_count + candidate_channels
            if tonumber(key) < tonumber(linked_key) then
                linked_path_channel_index = linked_path_channel_index + candidate_channels
            end
        end
    end
    result.audio_channel_index = linked_path_channel_count > 1 and linked_path_channel_index or nil
    return result
end

local range_intersects_selection

function timeline_item_is_selected(item)
    local keys = {"Selected", "IsSelected", "selected", "isSelected"}
    for _, key in ipairs(keys) do
        local ok, value = pcall(function() return item:GetProperty(key) end)
        if ok and (value == true or tostring(value) == "1" or tostring(value):lower() == "true") then
            return true
        end
    end
    local ok_props, props = pcall(function() return item:GetProperty() end)
    if ok_props and type(props) == "table" then
        for _, key in ipairs(keys) do
            local value = props[key]
            if value == true or tostring(value) == "1" or tostring(value):lower() == "true" then
                return true
            end
        end
    end
    return false
end

local function get_audio_track_display_name(timeline, track_index)
    local ok_name, raw_name = pcall(function() return timeline:GetTrackName("audio", track_index) end)
    local name = ok_name and trim_text(raw_name) or ""
    if name == "" then
        return string.format("A%d", tonumber(track_index) or 0)
    end
    return string.format("A%d %s", tonumber(track_index) or 0, name)
end

local function compare_audio_source_priority(a, b)
    if a.is_selected ~= b.is_selected then return a.is_selected == true end
    local a_muted = a.audio_mapping_muted == true
    local b_muted = b.audio_mapping_muted == true
    if a_muted ~= b_muted then return a_muted == false end
    local a_overlap = tonumber(a.overlap_frames) or 0
    local b_overlap = tonumber(b.overlap_frames) or 0
    if a_overlap ~= b_overlap then return a_overlap > b_overlap end
    return (tonumber(a.item_index) or 0) < (tonumber(b.item_index) or 0)
end

local function collect_audio_sources_for_scope(timeline, scope, fps)
    if type(scope) ~= "table" or scope.mode ~= WORK_SCOPE_MODE_SELECTION then
        return nil, "请先用 I/O 设置 In/Out 选区"
    end
    local scope_start = tonumber(scope.start_frame)
    local scope_end = tonumber(scope.end_frame)
    if not scope_start or not scope_end or scope_end <= scope_start then
        return nil, "请先用 I/O 设置 In/Out 选区"
    end
    local ok_track_count, track_count = pcall(function() return timeline:GetTrackCount("audio") end)
    track_count = ok_track_count and tonumber(track_count) or 0
    if track_count <= 0 then
        return nil, "时间线没有音频轨"
    end

    local audio_sources = {}
    local effective_fps = math.max(1, tonumber(fps) or 30)
    local seen_items, items_with_path = 0, 0
    for track_index = 1, track_count do
        local track_display_name = get_audio_track_display_name(timeline, track_index)
        local ok_items, items = pcall(function() return timeline:GetItemListInTrack("audio", track_index) end)
        items = ok_items and items or {}
        for item_index, item in ipairs(items or {}) do
            local item_start, item_end = subfix_item_frame_bounds(item, fps)
            seen_items = seen_items + 1
            if item_start and item_end and item_end > item_start then
                local overlap_start = math.max(scope_start, item_start)
                local overlap_end = math.min(scope_end, item_end)
                local overlap_frames = overlap_end - overlap_start
                if overlap_frames > 0 then
                    local file_path = get_audio_item_file_path(item)
                    if file_path then
                        items_with_path = items_with_path + 1
                        local source_offset_frames = get_audio_item_source_offset_frames(item)
                        local ok_media, media_item = pcall(function() return item:GetMediaPoolItem() end)
                        media_item = ok_media and media_item or nil
                        local mapped_audio = parse_source_audio_channel_mapping(item, media_item, effective_fps, source_offset_frames, file_path)
                        file_path = mapped_audio.file_path or file_path
                        -- Source offset frames are counted in the media's own fps; timeline
                        -- increments (overlap relative to the item) stay in the timeline fps.
                        local source_fps = tonumber(mapped_audio.source_fps) or read_media_source_fps(media_item, effective_fps)
                        local item_source_start_seconds = tonumber(mapped_audio.source_start_seconds) or (source_offset_frames / source_fps)
                        local source_start_seconds = item_source_start_seconds + ((overlap_start - item_start) / effective_fps)
                        local source_end_seconds = source_start_seconds + (overlap_frames / effective_fps)
                        local candidate = {
                            file_path = file_path,
                            file_name = basename(file_path),
                            track_index = track_index,
                            track_name = track_display_name,
                            item_index = item_index,
                            item_start_frame = item_start,
                            item_end_frame = item_end,
                            start_frame = overlap_start,
                            end_frame = overlap_end,
                            source_offset_frames = source_offset_frames,
                            source_fps = source_fps,
                            source_start_seconds = source_start_seconds,
                            source_end_seconds = source_end_seconds,
                            overlap_frames = overlap_frames
                        }
                        candidate.audio_mapping_source = mapped_audio.audio_mapping_source or "media_pool_file"
                        candidate.audio_mapping_fallback_reason = mapped_audio.audio_mapping_fallback_reason or ""
                        candidate.linked_offset_samples = mapped_audio.linked_offset_samples
                        candidate.audio_channel_index = mapped_audio.audio_channel_index
                        candidate.audio_mapping_muted = mapped_audio.audio_mapping_muted == true
                        candidate.is_selected = timeline_item_is_selected(item)
                        audio_sources[#audio_sources + 1] = candidate
                    end
                end
            end
        end
    end

    table.sort(audio_sources, function(a, b)
        local a_track = tonumber(a.track_index) or 0
        local b_track = tonumber(b.track_index) or 0
        if a_track ~= b_track then return a_track < b_track end
        return compare_audio_source_priority(a, b)
    end)

    if #audio_sources == 0 then
        return nil, string.format(
            "未找到与选区重叠的本地音频片段（已扫描 %d 条音频片段，%d 条能定位到文件；音频轨 %d 条，fps %s）",
            seen_items, items_with_path, track_count, tostring(fps))
    end
    return audio_sources
end

local function build_audio_track_options_for_dialog(audio_sources)
    if type(audio_sources) ~= "table" then
        return audio_sources
    end

    local sources_by_track = {}
    for _, source in ipairs(audio_sources) do
        local track_index = tonumber(source.track_index) or 0
        if not sources_by_track[track_index] then sources_by_track[track_index] = {} end
        sources_by_track[track_index][#sources_by_track[track_index] + 1] = source
    end

    local track_options = {}
    for _, grouped_sources in pairs(sources_by_track) do
        local usable_sources = {}
        for _, source in ipairs(grouped_sources) do
            if source.audio_mapping_muted ~= true then
                usable_sources[#usable_sources + 1] = source
            end
        end
        local sources = #usable_sources > 0 and usable_sources or grouped_sources
        table.sort(sources, function(a, b)
            local a_start = tonumber(a.start_frame) or 0
            local b_start = tonumber(b.start_frame) or 0
            if a_start ~= b_start then return a_start < b_start end
            local a_end = tonumber(a.end_frame) or 0
            local b_end = tonumber(b.end_frame) or 0
            if a_end ~= b_end then return a_end < b_end end
            return (tonumber(a.item_index) or 0) < (tonumber(b.item_index) or 0)
        end)

        local source = sources[1]
        local label = tostring(source.track_name or "")
        if label == "" then
            label = string.format("A%d", tonumber(source.track_index) or 0)
        end
        if #usable_sources == 0 and source.audio_mapping_muted == true then
            label = label .. "（静音）"
        end
        source.display_label = label
        source.track_sources = sources
        track_options[#track_options + 1] = source
    end

    table.sort(track_options, function(a, b)
        return (tonumber(a.track_index) or 0) < (tonumber(b.track_index) or 0)
    end)
    if #track_options > 0 then
        return track_options
    end
    return audio_sources
end

function range_intersects_selection(item_start, item_end, scope)
    local scope_start = tonumber(scope and scope.start_frame)
    local scope_end = tonumber(scope and scope.end_frame)
    if not scope_start or not scope_end then return false end
    local start_frame = tonumber(item_start)
    local end_frame = tonumber(item_end)
    if not start_frame or not end_frame then return false end
    return math.max(start_frame, end_frame) > scope_start and math.min(start_frame, end_frame) < scope_end
end

local get_subtitle_track_items

local function clear_subtitle_track_clips(timeline, track_index)
    local items, items_err = get_subtitle_track_items(track_index, timeline)
    if not items then return false, items_err end
    if #items == 0 then return true, 0 end
    local initial_count = #items
    local delete_ok = false
    local attempts = {
        function() return timeline:DeleteClips(items, false) end,
        function() return timeline:DeleteClips(items) end,
    }
    for _, delete_fn in ipairs(attempts) do
        local ok, ret = pcall(delete_fn)
        if ok and ret ~= false then
            delete_ok = true
            break
        end
    end
    if not delete_ok then
        return false, "清空目标字幕轨失败"
    end
    local remaining_items = select(1, get_subtitle_track_items(track_index, timeline)) or {}
    if #remaining_items > 0 then
        return false, "轨道 " .. tostring(track_index) .. " 仍残留 " .. tostring(#remaining_items) .. " 条字幕"
    end
    return true, initial_count
end

local function backup_target_track(timeline, track_index, fps, base_frame, progress_callback)
    local items, items_err = get_subtitle_track_items(track_index, timeline)
    if not items then return nil, items_err or "无法读取目标字幕轨", nil end
    if #items == 0 then return nil, nil, {} end

    local snapshot_rows = {}
    for item_position, item in ipairs(items) do
        local ok_start, start_frame = pcall(function() return item:GetStart() end)
        local ok_end, end_frame = pcall(function() return item:GetEnd() end)
        local ok_name, name = pcall(function() return item:GetName() end)
        if not ok_start or not ok_end then
            return nil, string.format("读取目标字幕轨第 %d 条失败，已停止写回", item_position), nil
        end
        snapshot_rows[#snapshot_rows + 1] = {
            start_frame = tonumber(start_frame) or 0,
            end_frame = tonumber(end_frame) or 0,
            text = tostring(ok_name and name or "")
        }

        local completed = item_position
        if progress_callback and (
            completed % GENERATE_BACKUP_PROGRESS_CHUNK_SIZE == 0 or completed == #items
        ) then
            if progress_callback(completed, #items) == false then
                return nil, "已取消", snapshot_rows, "cancelled"
            end
        end
    end

    local path = temp_dir() .. "/Backup_GenerateSelection_" .. os.date("%Y%m%d_%H%M%S") .. ".srt"
    local backup_parts = {}
    for index, row in ipairs(snapshot_rows) do
        backup_parts[#backup_parts + 1] = tostring(index) .. "\n"
        backup_parts[#backup_parts + 1] = frames_to_srt_time(row.start_frame, fps, base_frame) .. " --> " .. frames_to_srt_time(row.end_frame, fps, base_frame) .. "\n"
        backup_parts[#backup_parts + 1] = tostring(row.text or "") .. "\n\n"
    end
    -- write_text_file 先走 ANSI io.open，失败回退 CreateFileW 宽字符（TEMP 含中文也能写）。
    if not write_text_file(path, table.concat(backup_parts)) then
        return nil, "无法创建目标字幕轨备份", snapshot_rows
    end
    return path, nil, snapshot_rows
end

local function get_subtitle_track_type_and_count(timeline)
    if not timeline then return "subtitle", 0 end
    local candidates = {"subtitle", 3, "Subtitle"}
    for _, track_type in ipairs(candidates) do
        local ok, track_count = pcall(function() return timeline:GetTrackCount(track_type) end)
        if ok and track_count ~= nil and track_count ~= false then
            return track_type, tonumber(track_count) or 0
        end
    end
    return "subtitle", 0
end

function get_subtitle_track_items(track_index, timeline)
    if not track_index or track_index < 1 then
        return nil, "字幕轨索引无效"
    end
    local track_type, track_count = get_subtitle_track_type_and_count(timeline)
    if track_index > track_count then
        return nil, "轨道 " .. tostring(track_index) .. " 不存在", track_count, track_type
    end
    local tried = {}
    for _, candidate in ipairs({track_type, "subtitle", 3, "Subtitle"}) do
        local key = type(candidate) .. ":" .. tostring(candidate)
        if not tried[key] then
            tried[key] = true
            local ok_items, items = pcall(function() return timeline:GetItemListInTrack(candidate, track_index) end)
            if ok_items then
                return items or {}, nil, track_count, candidate
            end
        end
    end
    return nil, "无法读取轨道 " .. tostring(track_index) .. " 的字幕片段", track_count, track_type
end

local function add_subtitle_track(timeline)
    for _, track_type in ipairs({"subtitle", "Subtitle", 3}) do
        local ok, ret = pcall(function() return timeline:AddTrack(track_type) end)
        if ok and ret ~= false then return true end
    end
    return false
end

local function ensure_subtitle_track_exists(track_index, timeline)
    if not track_index or track_index < 1 then
        return nil, "字幕轨索引无效"
    end
    local _, track_count = get_subtitle_track_type_and_count(timeline)
    while track_count < track_index do
        local previous_count = track_count
        if not add_subtitle_track(timeline) then
            return nil, "无法创建字幕轨 " .. tostring(track_index)
        end
        _, track_count = get_subtitle_track_type_and_count(timeline)
        if track_count <= previous_count then
            return nil, "创建字幕轨后轨道数未变化"
        end
    end
    return get_subtitle_track_items(track_index, timeline)
end

local function unlock_all_subtitle_tracks(timeline)
    local track_type, track_count = get_subtitle_track_type_and_count(timeline)
    for track_index = 1, track_count do
        pcall(function() return timeline:SetTrackLock(track_type, track_index, false) end)
    end
    return true
end

local function isolate_subtitle_target_track(track_index, timeline)
    local track_type, track_count = get_subtitle_track_type_and_count(timeline)
    if track_index > track_count then
        return false, "目标字幕轨不存在"
    end
    for index = 1, track_count do
        local desired_enabled = index == track_index
        pcall(function() return timeline:SetTrackEnable(track_type, index, desired_enabled) end)
    end
    local ok_enabled, enabled = pcall(function() return timeline:GetIsTrackEnabled(track_type, track_index) end)
    if not ok_enabled or enabled == false then
        return false, "目标字幕轨未成功启用"
    end
    return true
end

-- 快照各字幕轨的启用状态，供 isolate 结束（成功或失败）后原样恢复。
local function snapshot_subtitle_track_enabled(timeline)
    local track_type, track_count = get_subtitle_track_type_and_count(timeline)
    local snapshot = {track_type = track_type, track_count = track_count, enabled = {}}
    for index = 1, track_count do
        local ok, enabled = pcall(function() return timeline:GetIsTrackEnabled(track_type, index) end)
        snapshot.enabled[index] = ok and (enabled and true or false) or nil
    end
    return snapshot
end

-- 按快照恢复字幕轨启用状态。keep_track_index 非 nil 时该轨保持当前状态
-- （刚写回的目标轨必须保持可见）；快照之后新增的轨保持启用不动。
local function restore_subtitle_track_enabled_states(snapshot, keep_track_index, timeline)
    if not snapshot or not snapshot.track_count then return true end
    local _, track_count = get_subtitle_track_type_and_count(timeline)
    local failures = {}
    for index = 1, track_count do
        if index ~= keep_track_index then
            local original_enabled = snapshot.enabled and snapshot.enabled[index]
            if original_enabled ~= nil then
                local ok, ret = pcall(function()
                    return timeline:SetTrackEnable(snapshot.track_type, index, original_enabled)
                end)
                if not ok or ret == false then
                    table.insert(failures, tostring(index))
                end
            end
        end
    end
    if #failures > 0 then
        return false, "恢复字幕轨启用状态失败（轨道 " .. table.concat(failures, ", ") .. "）"
    end
    return true
end

local function format_audio_source_label(audio_source, fps)
    if audio_source.display_label and tostring(audio_source.display_label) ~= "" then
        return tostring(audio_source.display_label)
    end
    local overlap_seconds = (tonumber(audio_source.overlap_frames) or 0) / math.max(1, tonumber(fps) or 30)
    local marks = {}
    if audio_source.is_selected then marks[#marks + 1] = "选中" end
    if audio_source.audio_mapping_muted == true then marks[#marks + 1] = "mute" end
    if tostring(audio_source.audio_mapping_source or "") ~= "" then marks[#marks + 1] = tostring(audio_source.audio_mapping_source) end
    if tonumber(audio_source.audio_channel_index) then marks[#marks + 1] = "ch" .. tostring(math.floor(tonumber(audio_source.audio_channel_index) or 0)) end
    local prefix = #marks > 0 and ("[" .. table.concat(marks, "/") .. "] ") or ""
    return string.format(
        "%sA%d  #%d  %s  %.1fs  %.3f-%.3fs",
        prefix,
        tonumber(audio_source.track_index) or 0,
        tonumber(audio_source.item_index) or 0,
        tostring(audio_source.file_name or audio_source.file_path or ""),
        overlap_seconds,
        tonumber(audio_source.source_start_seconds) or 0,
        tonumber(audio_source.source_end_seconds) or 0
    )
end

local function write_selected_audio_source_diagnostic(path, audio_source, scope, source_optimization)
    if not path or not audio_source then return false end
    local selected_audio_sources = audio_source[1] and audio_source or {audio_source}
    local first_source = selected_audio_sources[1] or audio_source
    local sources = {}
    for _, selected_source in ipairs(selected_audio_sources) do
        for _, source in ipairs(selected_source.track_sources or {selected_source}) do
            sources[#sources + 1] = source
        end
    end
    local lines = {
        "{",
        '  "audio_source_mode": "source_audio",',
        string.format('  "track_index": %d,', tonumber(first_source.track_index) or 0),
        string.format('  "track_name": "%s",', json_escape(first_source.track_name or first_source.display_label)),
        '  "selected_tracks": ['
    }
    for index, selected_source in ipairs(selected_audio_sources) do
        lines[#lines + 1] = string.format(
            '    {"track_index": %d, "track_name": "%s", "source_count": %d}%s',
            tonumber(selected_source.track_index) or 0,
            json_escape(selected_source.track_name or selected_source.display_label),
            #(selected_source.track_sources or {selected_source}),
            index < #selected_audio_sources and "," or ""
        )
    end
    lines[#lines + 1] = "  ],"
    lines[#lines + 1] = string.format('  "source_count": %d,', #sources)
    local optimization = source_optimization or {}
    lines[#lines + 1] = '  "source_optimization": {'
    lines[#lines + 1] = string.format('    "raw_source_count": %d,', tonumber(optimization.raw_source_count) or #sources)
    lines[#lines + 1] = string.format('    "deduped_source_count": %d,', tonumber(optimization.deduped_source_count) or #sources)
    lines[#lines + 1] = string.format('    "merged_source_count": %d,', tonumber(optimization.merged_source_count) or #sources)
    lines[#lines + 1] = string.format('    "duplicate_source_count": %d,', tonumber(optimization.duplicate_source_count) or 0)
    lines[#lines + 1] = string.format('    "merged_source_group_count": %d,', tonumber(optimization.merged_source_group_count) or 0)
    lines[#lines + 1] = string.format('    "merged_child_source_count": %d', tonumber(optimization.merged_child_source_count) or 0)
    lines[#lines + 1] = "  },"
    lines[#lines + 1] =
        '  "sources": ['
    for index, source in ipairs(sources) do
        lines[#lines + 1] = "    {"
        lines[#lines + 1] = string.format('      "track_index": %d,', tonumber(source.track_index) or 0)
        lines[#lines + 1] = string.format('      "track_name": "%s",', json_escape(source.track_name or first_source.display_label))
        lines[#lines + 1] = string.format('      "item_index": %d,', tonumber(source.item_index) or 0)
        lines[#lines + 1] = string.format('      "file_path": "%s",', json_escape(source.file_path))
        lines[#lines + 1] = string.format('      "file_name": "%s",', json_escape(source.file_name))
        lines[#lines + 1] = string.format('      "audio_mapping_source": "%s",', json_escape(source.audio_mapping_source))
        lines[#lines + 1] = string.format('      "audio_mapping_fallback_reason": "%s",', json_escape(source.audio_mapping_fallback_reason))
        lines[#lines + 1] = string.format('      "audio_mapping_muted": %s,', source.audio_mapping_muted == true and "true" or "false")
        lines[#lines + 1] = string.format('      "audio_channel_index": %s,', tonumber(source.audio_channel_index) and tostring(math.floor(tonumber(source.audio_channel_index) or 0)) or "null")
        lines[#lines + 1] = string.format('      "linked_offset_samples": %s,', tonumber(source.linked_offset_samples) and tostring(tonumber(source.linked_offset_samples)) or "null")
        lines[#lines + 1] = string.format('      "source_offset_frames": %d,', tonumber(source.source_offset_frames) or 0)
        lines[#lines + 1] = string.format('      "source_start_seconds": %.6f,', tonumber(source.source_start_seconds) or 0)
        lines[#lines + 1] = string.format('      "source_end_seconds": %.6f,', tonumber(source.source_end_seconds) or 0)
        lines[#lines + 1] = string.format('      "timeline_start_frame": %d,', tonumber(source.start_frame) or 0)
        lines[#lines + 1] = string.format('      "timeline_end_frame": %d', tonumber(source.end_frame) or 0)
        lines[#lines + 1] = index < #sources and "    }," or "    }"
    end
    lines[#lines + 1] = "  ],"
    lines[#lines + 1] = string.format('  "selected_timeline_start_frame": %d,', tonumber(first_source.start_frame) or 0)
    lines[#lines + 1] = string.format('  "selected_timeline_end_frame": %d,', tonumber(first_source.end_frame) or 0)
    lines[#lines + 1] = string.format('  "timeline_start_frame": %d,', tonumber(scope and scope.timeline_start_frame) or 0)
    lines[#lines + 1] = string.format('  "scope_start_frame": %d,', tonumber(scope and scope.start_frame) or 0)
    lines[#lines + 1] = string.format('  "scope_end_frame": %d', tonumber(scope and scope.end_frame) or 0)
    lines[#lines + 1] = "}"
    return write_text_file(path, table.concat(lines, "\n") .. "\n")
end

local function collect_selected_track_sources(selected_audio_sources)
    local selected_track_sources = {}
    for track_order, audio_source in ipairs(selected_audio_sources or {}) do
        for _, source in ipairs(audio_source.track_sources or {audio_source}) do
            source.track_order = track_order
            source.source_order = #selected_track_sources + 1
            selected_track_sources[#selected_track_sources + 1] = source
        end
    end
    table.sort(selected_track_sources, function(a, b)
        local a_order = tonumber(a.track_order) or 0
        local b_order = tonumber(b.track_order) or 0
        if a_order ~= b_order then return a_order < b_order end
        local a_start = tonumber(a.start_frame) or 0
        local b_start = tonumber(b.start_frame) or 0
        if a_start ~= b_start then return a_start < b_start end
        return (tonumber(a.item_index) or 0) < (tonumber(b.item_index) or 0)
    end)
    return selected_track_sources
end

local function clone_audio_source(source)
    local cloned = {}
    for key, value in pairs(source or {}) do
        if key ~= "track_sources" then cloned[key] = value end
    end
    return cloned
end

local function rounded_number_key(value, precision)
    local multiplier = 10 ^ (tonumber(precision) or 3)
    return tostring(math.floor((tonumber(value) or 0) * multiplier + 0.5) / multiplier)
end

local function duplicate_audio_source_key(source)
    return table.concat({
        tostring(source and source.file_path or ""),
        rounded_number_key(source and source.source_start_seconds, 3),
        rounded_number_key(source and source.source_end_seconds, 3),
        tostring(math.floor(tonumber(source and source.start_frame) or 0)),
        tostring(math.floor(tonumber(source and source.audio_channel_index) or 0)),
    }, "|")
end

local function optimize_selected_track_sources_for_generation(selected_track_sources, fps)
    -- Only exact cross-track duplicates are removed here; keeping clip boundaries avoids long-window ASR drift.
    local stats = {
        raw_source_count = #(selected_track_sources or {}),
        deduped_source_count = 0,
        duplicate_source_count = 0,
        merged_source_count = 0,
        merged_source_group_count = 0,
        merged_child_source_count = 0
    }
    local deduped = {}
    local seen = {}
    for _, source in ipairs(selected_track_sources or {}) do
        local key = duplicate_audio_source_key(source)
        if seen[key] then
            stats.duplicate_source_count = stats.duplicate_source_count + 1
        else
            seen[key] = true
            deduped[#deduped + 1] = clone_audio_source(source)
        end
    end
    stats.deduped_source_count = #deduped
    table.sort(deduped, function(a, b)
        local a_order = tonumber(a.track_order) or 0
        local b_order = tonumber(b.track_order) or 0
        if a_order ~= b_order then return a_order < b_order end
        local a_start = tonumber(a.start_frame) or 0
        local b_start = tonumber(b.start_frame) or 0
        if a_start ~= b_start then return a_start < b_start end
        return (tonumber(a.item_index) or 0) < (tonumber(b.item_index) or 0)
    end)

    for _, source in ipairs(deduped) do
        source.merged_source_count = 1
        source.merged_item_indices = tostring(source.item_index or "")
    end
    stats.merged_source_count = #deduped
    return deduped, stats
end

local function write_generate_batch_plan(path, selected_track_sources, fps)
    local lines = {"{", '  "batches": ['}
    for index, source in ipairs(selected_track_sources or {}) do
        local audio_channel_index = tonumber(source.audio_channel_index)
        lines[#lines + 1] = "    {"
        lines[#lines + 1] = string.format('      "batch_id": "track%d_item%d_%d",', tonumber(source.track_index) or 0, tonumber(source.item_index) or 0, index)
        lines[#lines + 1] = string.format('      "audio": "%s",', json_escape(source.file_path))
        lines[#lines + 1] = string.format('      "source_start": %.6f,', tonumber(source.source_start_seconds) or 0)
        lines[#lines + 1] = string.format('      "source_end": %.6f,', tonumber(source.source_end_seconds) or 0)
        lines[#lines + 1] = string.format('      "timeline_start_frame": %d,', tonumber(source.start_frame) or 0)
        lines[#lines + 1] = string.format('      "timeline_end_frame": %d,', tonumber(source.end_frame) or 0)
        lines[#lines + 1] = string.format('      "fps": %.6f,', tonumber(fps) or 30)
        lines[#lines + 1] = string.format('      "audio_channel_index": %s,', audio_channel_index and tostring(math.floor(audio_channel_index)) or "null")
        lines[#lines + 1] = string.format('      "track_order": %d,', tonumber(source.track_order) or 0)
        lines[#lines + 1] = string.format('      "track_index": %d,', tonumber(source.track_index) or 0)
        lines[#lines + 1] = string.format('      "track_name": "%s",', json_escape(source.track_name or ""))
        lines[#lines + 1] = string.format('      "item_index": %d', tonumber(source.item_index) or 0)
        lines[#lines + 1] = index < #(selected_track_sources or {}) and "    }," or "    }"
    end
    lines[#lines + 1] = "  ]"
    lines[#lines + 1] = "}"
    return write_text_file(path, table.concat(lines, "\n") .. "\n")
end

local function parse_generated_json_subtitle_rows(json_path)
    local payload_text = read_text_file(json_path)
    if not payload_text then return nil, "无法读取生成 JSON" end
    local payload, decode_err = decode_json_text(payload_text)
    if not payload then return nil, decode_err or "生成 JSON 解析失败" end
    if payload.ok == false then return nil, tostring(payload.error or "ASR helper 执行失败") end
    local rows = {}
    for _, row in ipairs(payload.subtitle_rows or {}) do
        local start_frame = tonumber(row.start_frame)
        local end_frame = tonumber(row.end_frame)
        local text = trim_text(row.text or "")
        if start_frame and end_frame and end_frame > start_frame and text ~= "" then
            rows[#rows + 1] = {
                index = #rows + 1,
                start_frame = start_frame,
                end_frame = end_frame,
                text = text,
                batch_id = tostring(row.batch_id or ""),
                track_order = tonumber(row.track_order) or 0,
                track_index = tonumber(row.track_index) or 0,
                item_index = tonumber(row.item_index) or 0,
                speaker_track_index = tonumber(row.speaker_track_index),
                speaker_score_db = tonumber(row.speaker_score_db),
                speaker_decision = tostring(row.speaker_decision or "")
            }
        end
    end
    if #rows == 0 then return nil, "生成 JSON 没有可写回字幕" end
    return rows
end

local function subtract_existing_ranges(row, accepted_ranges)
    local intervals = {{start_frame = tonumber(row.start_frame) or 0, end_frame = tonumber(row.end_frame) or 0}}
    for _, range in ipairs(accepted_ranges or {}) do
        local next_intervals = {}
        local range_start = tonumber(range.start_frame) or 0
        local range_end = tonumber(range.end_frame) or 0
        for _, interval in ipairs(intervals) do
            local start_frame = tonumber(interval.start_frame) or 0
            local end_frame = tonumber(interval.end_frame) or 0
            if range_end <= start_frame or range_start >= end_frame then
                next_intervals[#next_intervals + 1] = interval
            else
                if range_start > start_frame then
                    next_intervals[#next_intervals + 1] = {start_frame = start_frame, end_frame = math.min(range_start, end_frame)}
                end
                if range_end < end_frame then
                    next_intervals[#next_intervals + 1] = {start_frame = math.max(range_end, start_frame), end_frame = end_frame}
                end
            end
        end
        intervals = next_intervals
        if #intervals == 0 then break end
    end
    return intervals
end

local function merge_generated_rows_with_track_priority(rows, scope)
    local sorted_rows = {}
    for _, row in ipairs(rows or {}) do sorted_rows[#sorted_rows + 1] = row end
    table.sort(sorted_rows, function(a, b)
        local a_order = tonumber(a.track_order) or tonumber(a.track_index) or 0
        local b_order = tonumber(b.track_order) or tonumber(b.track_index) or 0
        if a_order ~= b_order then return a_order < b_order end
        local a_start = tonumber(a.start_frame) or 0
        local b_start = tonumber(b.start_frame) or 0
        if a_start ~= b_start then return a_start < b_start end
        return (tonumber(a.end_frame) or 0) < (tonumber(b.end_frame) or 0)
    end)

    local accepted_ranges = {}
    local merged_rows = {}
    local scope_start = tonumber(scope and scope.start_frame)
    local scope_end = tonumber(scope and scope.end_frame)
    for _, row in ipairs(sorted_rows) do
        local row_start = tonumber(row.start_frame)
        local row_end = tonumber(row.end_frame)
        if row_start and row_end and row_end > row_start then
            if scope_start then row_start = math.max(row_start, scope_start) end
            if scope_end then row_end = math.min(row_end, scope_end) end
            if row_end > row_start then
                local candidate = {
                    start_frame = row_start,
                    end_frame = row_end,
                    text = row.text,
                    track_order = row.track_order,
                    track_index = row.track_index,
                    item_index = row.item_index
                }
                for _, interval in ipairs(subtract_existing_ranges(candidate, accepted_ranges)) do
                    if interval.end_frame > interval.start_frame then
                        local kept = {
                            start_frame = interval.start_frame,
                            end_frame = interval.end_frame,
                            text = candidate.text,
                            track_order = candidate.track_order,
                            track_index = candidate.track_index,
                            item_index = candidate.item_index
                        }
                        merged_rows[#merged_rows + 1] = kept
                        accepted_ranges[#accepted_ranges + 1] = {start_frame = kept.start_frame, end_frame = kept.end_frame}
                    end
                end
            end
        end
    end
    table.sort(merged_rows, function(a, b)
        local a_start = tonumber(a.start_frame) or 0
        local b_start = tonumber(b.start_frame) or 0
        if a_start ~= b_start then return a_start < b_start end
        return (tonumber(a.end_frame) or 0) < (tonumber(b.end_frame) or 0)
    end)
    for index, row in ipairs(merged_rows) do row.index = index end
    if #merged_rows == 0 then return nil, "按轨优先去重后没有可写回字幕" end
    return merged_rows
end

local function merge_generated_rows_for_live_writeback(rows, scope)
    for _, row in ipairs(rows or {}) do
        if not tonumber(row.speaker_track_index) then
            return merge_generated_rows_with_track_priority(rows, scope)
        end
    end

    local sorted_rows = {}
    local scope_start = tonumber(scope and scope.start_frame)
    local scope_end = tonumber(scope and scope.end_frame)
    for _, row in ipairs(rows or {}) do
        local start_frame = tonumber(row.start_frame)
        local end_frame = tonumber(row.end_frame)
        if start_frame and end_frame then
            if scope_start then start_frame = math.max(start_frame, scope_start) end
            if scope_end then end_frame = math.min(end_frame, scope_end) end
            if end_frame > start_frame then
                local candidate = {}
                for key, value in pairs(row) do candidate[key] = value end
                candidate.start_frame = start_frame
                candidate.end_frame = end_frame
                sorted_rows[#sorted_rows + 1] = candidate
            end
        end
    end
    table.sort(sorted_rows, function(a, b)
        local a_start = tonumber(a.start_frame) or 0
        local b_start = tonumber(b.start_frame) or 0
        if a_start ~= b_start then return a_start < b_start end
        local a_score = tonumber(a.speaker_score_db) or 0
        local b_score = tonumber(b.speaker_score_db) or 0
        if a_score ~= b_score then return a_score > b_score end
        return (tonumber(a.end_frame) or 0) < (tonumber(b.end_frame) or 0)
    end)

    local merged_rows = {}
    for _, row in ipairs(sorted_rows) do
        local previous = merged_rows[#merged_rows]
        if previous and tonumber(row.start_frame) < tonumber(previous.end_frame) then
            if tonumber(row.end_frame) <= tonumber(previous.end_frame) then
                if (tonumber(row.speaker_score_db) or 0) > (tonumber(previous.speaker_score_db) or 0) then
                    merged_rows[#merged_rows] = row
                end
                goto continue_live_row
            end
            previous.end_frame = math.max(tonumber(previous.start_frame) + 1, tonumber(row.start_frame))
            row.start_frame = tonumber(previous.end_frame)
        end
        if tonumber(row.end_frame) > tonumber(row.start_frame) then
            merged_rows[#merged_rows + 1] = row
        end
        ::continue_live_row::
    end
    for index, row in ipairs(merged_rows) do row.index = index end
    if #merged_rows == 0 then return nil, "现场模式没有可写回字幕" end
    return merged_rows
end

-- ============================================================================
-- 生成面板控制器（GenerationPanelController）
--
-- 同一个控制器同时服务两种 UI 形态：
--   * standalone：「生成选区字幕.lua」调试入口使用的强模态选择窗口（行为与旧版一致）；
--   * embedded ：SubFix.lua 主窗口右侧常驻生成面板（复用宿主 dispatcher，不弹窗）。
-- 两种形态共用全部选项状态、音频轨道树、热词库/文稿/豆包/Qwen 子编辑器与事件逻辑，
-- 只是 build_descriptor() 的外层容器与 confirm/cancel 的出口不同。
--
-- 注意：本块全部 local 包在 do...end 内，避免触碰 Lua 5.1 主 chunk 200 local 上限；
-- 仅把 standalone 包装函数与控制器构造器挂到 SubFixGenerateSelectionCore 表上跨块使用。
-- ============================================================================
do
local SUBTITLE_LENGTH_OPTIONS = {
    {label = "标准（≤25字）", max_chars = 25},
    {label = "短视频（≤10字）", max_chars = 10},
}
local SUBTITLE_ENGINE_OPTIONS = {
    {label = "Qwen（本地）", backend = "auto"},
    {label = "豆包（云端）", backend = "doubao"},
}
local DOUBAO_API_KEY_GUIDE_URL = "https://console.volcengine.com/speech/new/setting/apikeys"
local DOUBAO_DOUBLE_CLICK_SECONDS = 1
local panel_table_unpack = unpack or table.unpack

local GenerationPanelController = {}
GenerationPanelController.__index = GenerationPanelController

local function new_generation_panel_controller(mode)
    local self_obj = setmetatable({}, GenerationPanelController)
    self_obj.mode = (mode == "embedded") and "embedded" or "standalone"
    self_obj.window = nil
    self_obj.items = nil
    self_obj.callbacks = nil

    self_obj.audio_sources = {}
    self_obj.scope = nil
    self_obj.fps = 24

    -- 模式选择下拉已移除，固定使用现场模式（单轨自然退化为单人识别）
    self_obj.subtitle_mode = "live"
    self_obj.selected_length_index = 1
    self_obj.selected_engine_index = 1
    self_obj.selected_doubao_backend = read_doubao_backend_preference()
    local last_engine_backend = read_last_engine_backend()
    if last_engine_backend == "doubao_asr" or last_engine_backend == "doubao_asr_v2" then
        self_obj.selected_engine_index = 2
    elseif last_engine_backend then
        for i, opt in ipairs(SUBTITLE_ENGINE_OPTIONS) do
            if opt.backend == last_engine_backend then
                self_obj.selected_engine_index = i
                break
            end
        end
    end

    self_obj.hotword_entries = load_generate_hotword_entries()
    self_obj.hotwords_enabled = #self_obj.hotword_entries > 0
    self_obj.script_match_enabled = false
    self_obj.script_match_text = ""

    self_obj.track_rows = {}
    self_obj.item_map = {}

    self_obj.dialog_cancelled = false
    self_obj.qwen_install_requested = false
    self_obj.confirmed_options = nil

    -- 子窗口按需创建并缓存
    self_obj.hotword_library_window = nil
    self_obj.hotword_clear_confirm_window = nil
    self_obj.hotword_library_closing = false
    self_obj.script_match_window = nil
    self_obj.doubao_key_window = nil
    self_obj.doubao_reminder_window = nil
    self_obj.qwen_download_window = nil
    self_obj.qwen_download_is_ready = false
    self_obj.last_doubao_click_time = nil
    self_obj.last_qwen_click_time = nil
    self_obj.doubao_reminder_shown = false
    return self_obj
end

function GenerationPanelController:embedded()
    return self.mode == "embedded"
end

-- 面板描述符：与旧版强模态窗口保持相同控件 ID 与业务语义；
-- embedded 模式额外提供重新检测按钮与内嵌进度区块（GenerateEmbedded* 前缀）。
-- wide=true（宿主三步工作台的「选区识别」页）：根容器占满页面、选项两列网格、
-- 音频树纵向伸展；narrow（standalone 弹窗 / 旧右栏兜底）保持原窄栏布局。
function GenerationPanelController:build_descriptor(wide)
    local embedded = self:embedded()
    self.wide_layout = wide == true
    wide = self.wide_layout
    local title_font = ui:Font{PixelSize = (host_design and host_design.font and host_design.font.title) or 16}
    local meta_font = ui:Font{PixelSize = (host_design and host_design.font and host_design.font.meta) or 12}
    -- 12px 分组小标题（弱化灰，不与 16px 页面标题抢层级）。
    local function section_label(text)
        return ui:Label{Text = tostring(text), Weight = 0, MinimumSize = {0, 16}, Font = meta_font}
    end
    local children = {}

    if embedded then
        children[#children + 1] = ui:HGroup{
            Weight = 0,
            Spacing = 8,
            MinimumSize = {0, 26},
            ui:Label{Text = "选区识别", Weight = 1, Font = title_font},
            ui:Button{ID = "GenerateEmbeddedRescanBtn", Text = "重新检测选区", Weight = 0, MinimumSize = {104, 26}},
        }
    end

    children[#children + 1] = ui:Label{
        ID = "GenerateSelectionInfoLabel",
        Text = "选择用于识别的音频轨道",
        Weight = 0,
        WordWrap = true,
        MinimumSize = {0, 20}
    }

    children[#children + 1] = ui:Tree{
        ID = "GenerateAudioTrackTree",
        Weight = 1,
        MinimumSize = {0, (embedded and not wide) and 150 or 120},
        Events = {ItemClicked = true}
    }
    -- 多选依赖树的 ExtendedSelection，没有复选框列，用 12px 元信息行明确提示操作方式。
    -- 必须 Weight=0 + 固定一行最小高：树 Weight=1 伸缩时，这行只占自身高度，绝不被树拉伸，
    -- 也不会在音轨很多时与树的最后一行或下方「生成选项」叠字。
    children[#children + 1] = ui:Label{
        Text = "按住 Ctrl / Shift 可多选音频轨",
        Weight = 0,
        Font = meta_font,
        MinimumSize = {0, 16},
    }

    -- 选项控件（wide/narrow 共用，ID 与语义完全一致；仅排列不同）。
    local length_row = ui:HGroup{
        Weight = 1,
        Spacing = 8,
        MinimumSize = {0, 26},
        ui:Label{Text = "长度：", Weight = 0, MinimumSize = {46, 0}, Alignment = {AlignRight = true, AlignVCenter = true}},
        ui:Button{ID = "GenerateSubtitleLengthStandardBtn", Text = "标准≤25字", Weight = 1, MinimumSize = {0, 26}, Checkable = true, Checked = true, StyleSheet = SUBFIX_BTN_TOGGLE},
        ui:Button{ID = "GenerateSubtitleLengthShortBtn", Text = "短≤10字", Weight = 1, MinimumSize = {0, 26}, Checkable = true, Checked = false, StyleSheet = SUBFIX_BTN_TOGGLE}
    }
    local engine_row = ui:HGroup{
        Weight = 1,
        Spacing = 8,
        MinimumSize = {0, 26},
        ui:Label{Text = "模型：", Weight = 0, MinimumSize = {46, 0}, Alignment = {AlignRight = true, AlignVCenter = true}},
        ui:Button{ID = "GenerateSubtitleEngineQwenBtn", Text = "Qwen 本地", Weight = 1, MinimumSize = {0, 26}, Checkable = true, Checked = true, StyleSheet = SUBFIX_BTN_TOGGLE},
        ui:Button{ID = "GenerateSubtitleEngineDoubaoBtn", Text = "豆包云端", Weight = 1, MinimumSize = {0, 26}, Checkable = true, Checked = false, StyleSheet = SUBFIX_BTN_TOGGLE}
    }
    local hotword_row = ui:HGroup{
        Weight = 1,
        Spacing = 8,
        MinimumSize = {0, 26},
        ui:Label{Text = "热词：", Weight = 0, MinimumSize = {46, 0}, Alignment = {AlignRight = true, AlignVCenter = true}},
        ui:Button{ID = "GenerateHotwordToggleBtn", Text = "", Weight = 1, MinimumSize = {0, 26}, Checkable = true, Checked = false, StyleSheet = SUBFIX_BTN_TOGGLE},
        ui:Button{ID = "GenerateHotwordManageBtn", Text = "管理…", Weight = 0, MinimumSize = {72, 26}}
    }
    local script_row = ui:HGroup{
        Weight = 1,
        Spacing = 8,
        MinimumSize = {0, 26},
        ui:Label{Text = "文稿：", Weight = 0, MinimumSize = {46, 0}, Alignment = {AlignRight = true, AlignVCenter = true}},
        ui:Button{ID = "GenerateScriptMatchToggleBtn", Text = "", Weight = 1, MinimumSize = {0, 26}, Checkable = true, Checked = false, StyleSheet = SUBFIX_BTN_TOGGLE},
        ui:Button{ID = "GenerateScriptMatchEditBtn", Text = "文稿…", Weight = 0, MinimumSize = {72, 26}}
    }

    if wide then
        -- 选项区整体包一层：生成中折叠（进度块接管纵向空间），避免识别页超高导致按钮被裁。
        children[#children + 1] = ui:VGroup{
            ID = "GenerateEmbeddedOptionsBlock",
            Weight = 0,
            Spacing = 6,
            section_label("生成选项"),
            ui:HGroup{
                Weight = 0,
                Spacing = 16,
                ui:VGroup{Weight = 1, Spacing = 8, length_row, hotword_row},
                ui:VGroup{Weight = 1, Spacing = 8, engine_row, script_row}
            },
            ui:HGroup{
                Weight = 0,
                Spacing = 16,
                ui:Label{ID = "GenerateScriptMatchStatusLabel", Text = "关闭：使用识别模型生成字幕", Weight = 1, WordWrap = true},
                ui:Label{ID = "GenerateSelectionRangeLabel", Text = "未检测到 In/Out 选区", Weight = 1, WordWrap = true}
            }
        }
    else
        children[#children + 1] = length_row
        children[#children + 1] = engine_row
        children[#children + 1] = hotword_row
        children[#children + 1] = script_row
        children[#children + 1] = ui:Label{
            ID = "GenerateScriptMatchStatusLabel",
            Text = "关闭：使用识别模型生成字幕",
            Weight = 0,
            WordWrap = true
        }
        children[#children + 1] = ui:Label{
            ID = "GenerateSelectionRangeLabel",
            Text = "未检测到 In/Out 选区",
            Weight = 0,
            WordWrap = true
        }
    end

    if embedded then
        -- 内嵌进度区块：不画条形（运行时改 Weight/像素宽在 Fusion 里都不可靠），
        -- 只保留状态行 + 橙色加粗的「进度 X% · 用时 Ns」文字行。
        children[#children + 1] = ui:VGroup{
            ID = "GenerateEmbeddedProgressBlock",
            Weight = 0,
            Spacing = 6,
            Hidden = true,
            ui:Label{ID = "GenerateEmbeddedStatusLabel", Text = "准备中", Weight = 0, WordWrap = true, MinimumSize = {0, 20}},
            ui:Label{ID = "GenerateEmbeddedProgressMetaLabel", Text = "进度 0%  ·  用时 0s", Weight = 0, MinimumSize = {0, 18},
                StyleSheet = SUBFIX_PROGRESS_META_STYLE},
        }
    end

    if embedded then
        -- Quiet Pro：主按钮不再贯穿整行。收成 200×34 的居中胶囊；
        -- 两侧 HGap 均分剩余宽度。跑批时「取消生成」取消隐藏，与主按钮成组居中。
        children[#children + 1] = ui:HGroup{
            Weight = 0,
            Spacing = 10,
            MinimumSize = {0, 40},
            ui:HGap(0, 1.0),
            ui:Button{
                ID = "GenerateSelectionConfirmBtn",
                Text = "开始生成",
                Weight = 0,
                MinimumSize = {200, 34},
                StyleSheet = SUBFIX_BTN_ACCENT
            },
            ui:Button{
                ID = "GenerateSelectionCancelBtn",
                Text = "取消生成",
                Weight = 0,
                MinimumSize = {120, 34},
                -- idle 时隐藏，仅生成中出现，避免常驻置灰按钮制造噪音。
                Hidden = true
            },
            ui:HGap(0, 1.0),
        }
    else
        children[#children + 1] = ui:HGroup{
            Weight = 0,
            Spacing = 8,
            MinimumSize = {0, 36},
            ui:Button{
                ID = "GenerateSelectionConfirmBtn",
                Text = "生成",
                Weight = 1,
                MinimumSize = {0, 30},
                StyleSheet = SUBFIX_BTN_ACCENT
            },
            ui:Button{
                ID = "GenerateSelectionCancelBtn",
                Text = "取消",
                Weight = wide and 0 or 1,
                MinimumSize = {wide and 120 or 0, 30},
                Enabled = true
            }
        }
    end
    children[#children + 1] = ui:VGap(wide and 4 or 8, 0)

    return ui:VGroup{
        ID = "GenerateSelectionPanel",
        Font = DIALOG_FONT,
        StyleSheet = SUBFIX_ROOT_STYLESHEET,
        -- wide 行距收紧到 6：识别页在 150% 缩放（逻辑高 680）下也要完整放下。
        Spacing = wide and 6 or (embedded and 8 or 11),
        ContentsMargins = wide and {0, 0, 0, 0} or (embedded and {12, 12, 12, 12} or {16, 16, 16, 16}),
        Weight = (embedded and wide) and 1 or (embedded and 0 or 1),
        MinimumSize = (embedded and not wide) and {330, 0} or nil,
        MaximumSize = (embedded and not wide) and {390, 16777215} or nil,
        panel_table_unpack(children)
    }
end

-- 按折行容量估算 WordWrap Label 所需行数：CJK/全角权重 2、ASCII 权重 1，
-- 显式 \n 另起行（含空行）；Fusion 的 WordWrap Label 不会按折行后高度重新布局，
-- 必须显式给 MinimumSize，否则多行文字溢出叠到下方控件。
function subfix_wrapped_line_count(text, capacity)
    local total = 0
    -- 末尾补 \n：每个显式行（含空行）恰好产出一次捕获。
    for line in (tostring(text or "") .. "\n"):gmatch("([^\n]*)\n") do
        -- Lua 5.1 模式区间不能以 \0 为端点（%z 除外）；字幕文本不会含 NUL，用 1-127。
        local ascii_count = select(2, line:gsub("[\1-\127]", ""))
        -- UTF-8：ASCII 1 字节，常用 CJK 3 字节（4 字节 emoji 近似按 CJK 处理，少算高度可接受）。
        local cjk_count = (#line - ascii_count) / 3
        local weight = ascii_count + cjk_count * 2
        total = total + math.max(1, math.ceil(weight / capacity))
    end
    return math.max(1, total)
end

function GenerationPanelController:set_info(text)
    local label = self.items and self.items.GenerateSelectionInfoLabel or nil
    if label and text ~= nil then
        local content = tostring(text)
        pcall(function() label.Text = content end)
        -- 容量按全角字数估（14px 字宽 ≈14px）：wide 页约 1180px 可用宽→78；
        -- narrow 弹窗/窄栏约 400px→28。宁少勿多：高度估大只是留白，估小会叠字。
        local capacity = self.wide_layout and 78 or 28
        local lines = subfix_wrapped_line_count(content, capacity)
        pcall(function() label.MinimumSize = {0, lines * 18 + 6} end)
    end
end

function GenerationPanelController:update_scope_label()
    local label = self.items and self.items.GenerateSelectionRangeLabel or nil
    if not label then return end
    local scope = self.scope
    if type(scope) ~= "table" or not scope.start_frame then
        pcall(function() label.Text = "未检测到 In/Out 选区" end)
        return
    end
    pcall(function()
        label.Text = string.format(
            "%s：%d - %d%s",
            tostring(scope.source_label or "范围"),
            tonumber(scope.start_frame) or 0,
            tonumber(scope.end_frame) or 0,
            scope.item_name and scope.item_name ~= "" and (" · " .. tostring(scope.item_name)) or ""
        )
    end)
end

function GenerationPanelController:populate_tracks(audio_sources)
    local tree = self.items and self.items.GenerateAudioTrackTree or nil
    self.audio_sources = type(audio_sources) == "table" and audio_sources or {}
    self.track_rows = {}
    self.item_map = {}
    if not tree then return end
    pcall(function() tree:Clear() end)
    for index, source in ipairs(self.audio_sources) do
        local ok_item, item = pcall(function() return tree:NewItem() end)
        if ok_item and item then
            self.track_rows[index] = {source = source, item = item}
            set_tree_item_text(item, 0, format_audio_source_label(source, self.fps))
            pcall(function() tree:AddTopLevelItem(item) end)
            self.item_map[item] = index
        end
    end
    -- 默认选中第 1 条轨道；ExtendedSelection 由 Qt 原生绘制多选。
    pcall(function()
        if self.track_rows[1] and self.track_rows[1].item then
            self.track_rows[1].item.Selected = true
            pcall(function() tree:SetSelection(self.track_rows[1].item) end)
        end
    end)
    safe_refresh_tree_widget(tree)
end

function GenerationPanelController:set_context(audio_sources, scope, fps)
    self.scope = scope
    self.fps = tonumber(fps) or 24
    self:update_scope_label()
    self:populate_tracks(audio_sources)
end

function GenerationPanelController:collect_checked_audio_sources()
    local tree = self.items and self.items.GenerateAudioTrackTree or nil
    local checked_sources = {}
    local selected = {}
    if tree then
        pcall(function() selected = tree:SelectedItems() or {} end)
    end
    if not selected or #selected == 0 then
        for _, row in ipairs(self.track_rows) do
            if row.item and row.item.Selected then
                selected[#selected + 1] = row.item
            end
        end
    end
    for _, item in ipairs(selected) do
        local idx = self.item_map[item]
        if idx and self.track_rows[idx] then
            checked_sources[#checked_sources + 1] = self.track_rows[idx].source
        end
    end
    return checked_sources
end

function GenerationPanelController:refresh_length_buttons()
    local standard_btn = self.items and self.items.GenerateSubtitleLengthStandardBtn or nil
    local short_btn = self.items and self.items.GenerateSubtitleLengthShortBtn or nil
    if standard_btn then
        local selected = self.selected_length_index == 1
        pcall(function() standard_btn.Text = SUBTITLE_LENGTH_OPTIONS[1].label end)
        pcall(function() standard_btn.Checked = selected end)
        pcall(function() standard_btn:SetAttrs({Checked = selected}) end)
        pcall(function() standard_btn:SetAttrs({StyleSheet = SUBFIX_BTN_TOGGLE}) end)
    end
    if short_btn then
        local selected = self.selected_length_index == 2
        pcall(function() short_btn.Text = SUBTITLE_LENGTH_OPTIONS[2].label end)
        pcall(function() short_btn.Checked = selected end)
        pcall(function() short_btn:SetAttrs({Checked = selected}) end)
        pcall(function() short_btn:SetAttrs({StyleSheet = SUBFIX_BTN_TOGGLE}) end)
    end
end

function GenerationPanelController:select_length(index)
    self.selected_length_index = index
    self:refresh_length_buttons()
end

function GenerationPanelController:refresh_engine_buttons()
    local qwen_btn = self.items and self.items.GenerateSubtitleEngineQwenBtn or nil
    local doubao_btn = self.items and self.items.GenerateSubtitleEngineDoubaoBtn or nil
    if qwen_btn then
        local selected = self.selected_engine_index == 1
        pcall(function() qwen_btn.Text = SUBTITLE_ENGINE_OPTIONS[1].label end)
        pcall(function() qwen_btn.Checked = selected end)
        pcall(function() qwen_btn:SetAttrs({Checked = selected}) end)
        pcall(function() qwen_btn:SetAttrs({StyleSheet = SUBFIX_BTN_TOGGLE}) end)
    end
    if doubao_btn then
        local selected = self.selected_engine_index == 2
        pcall(function() doubao_btn.Text = SUBTITLE_ENGINE_OPTIONS[2].label end)
        pcall(function() doubao_btn.Checked = selected end)
        pcall(function() doubao_btn:SetAttrs({Checked = selected}) end)
        pcall(function() doubao_btn:SetAttrs({StyleSheet = SUBFIX_BTN_TOGGLE}) end)
    end
end

function GenerationPanelController:select_engine(index)
    self.selected_engine_index = index
    self:refresh_engine_buttons()
end

function GenerationPanelController:read_max_chars()
    local option = SUBTITLE_LENGTH_OPTIONS[self.selected_length_index]
    return option and option.max_chars or SUBTITLE_LENGTH_OPTIONS[1].max_chars
end

function GenerationPanelController:read_backend()
    if self.selected_engine_index == 2 then
        return self.selected_doubao_backend
    end
    return "auto"
end

-- 读取面板当前全部选项；返回 options 表或 nil, 错误信息。
function GenerationPanelController:collect_current_options()
    local sources = self:collect_checked_audio_sources()
    if #sources < 1 then
        return nil, "现场模式至少选择一条音频轨道"
    end
    local options = {
        sources = sources,
        subtitle_mode = self.subtitle_mode,
        max_chars = self:read_max_chars(),
        backend = self:read_backend(),
        hotwords_json = self.hotwords_enabled and resolve_asr_paths().hotwords or nil,
        script_match_enabled = self.script_match_enabled,
        script_match_text = self.script_match_text,
    }
    -- 记住本次识别模型，下次默认选它。
    save_last_engine_backend(options.backend)
    return options
end

-- 点击「生成/开始生成」时的统一校验：需要 Qwen（文稿匹配或本地引擎）但未安装时返回 "need_qwen"。
function GenerationPanelController:collect_start_options()
    local options, err = self:collect_current_options()
    if not options then
        return nil, err
    end
    if options.script_match_enabled or options.backend == "auto" then
        local qwen_status = qwen_check.inspect(resolve_asr_paths())
        if not qwen_status.ready then
            return nil, "need_qwen"
        end
    end
    return options
end

function GenerationPanelController:refresh_hotword_toggle()
    local btn = self.items and self.items.GenerateHotwordToggleBtn or nil
    if not btn then return end
    local count = #self.hotword_entries
    local count_text = tostring(count) .. " 条"
    if count > 200 then count_text = count_text .. "（本次前200条）" end
    -- 开关文字同时表达开/关状态与条数，避免仅凭橙边框色传递状态（色觉冗余）。
    local label_text = (self.hotwords_enabled and "热词：开 · " or "热词：关 · ") .. count_text
    pcall(function() btn.Text = label_text end)
    pcall(function() btn.Checked = self.hotwords_enabled end)
    pcall(function() btn:SetAttrs({Checked = self.hotwords_enabled}) end)
    pcall(function() btn:SetAttrs({StyleSheet = SUBFIX_BTN_TOGGLE}) end)
end

function GenerationPanelController:refresh_script_match_toggle()
    local btn = self.items and self.items.GenerateScriptMatchToggleBtn or nil
    if btn then
        local detail = ""
        if self.script_match_enabled then
            local n = 0
            for _ in (self.script_match_text .. "\n"):gmatch("(.-)\n") do
                if trim_text(_) ~= "" then n = n + 1 end
            end
            detail = string.format("（已启用 %d 行）", n)
        end
        pcall(function() btn.Text = "启用文稿匹配" .. detail end)
        pcall(function() btn.Checked = self.script_match_enabled end)
        pcall(function() btn:SetAttrs({Checked = self.script_match_enabled}) end)
        pcall(function() btn:SetAttrs({StyleSheet = SUBFIX_BTN_TOGGLE}) end)
    end
    local status_label = self.items and self.items.GenerateScriptMatchStatusLabel or nil
    if status_label then
        if self.script_match_enabled then
            pcall(function() status_label.Text = "已启用：用 Qwen3 强制对齐文稿（忽略识别模型），字幕文字将严格等于文稿" end)
        else
            pcall(function() status_label.Text = "关闭：使用识别模型生成字幕" end)
        end
    end
    local qwen_btn = self.items and self.items.GenerateSubtitleEngineQwenBtn or nil
    local doubao_btn = self.items and self.items.GenerateSubtitleEngineDoubaoBtn or nil
    if qwen_btn then pcall(function() qwen_btn.Enabled = not self.script_match_enabled end) end
    if doubao_btn then pcall(function() doubao_btn.Enabled = not self.script_match_enabled end) end
end

-- 热词库管理子窗口（standalone 与 embedded 共用，按需创建）。
function GenerationPanelController:open_hotword_library()
    if self.hotword_library_window then
        self.hotword_library_closing = false
        pcall(function() self.hotword_library_window:Show() end)
        return
    end
    local controller = self
    local hotword_library_window = dispatcher:AddWindow({
        ID = "GenerateHotwordLibraryWindow",
        WindowTitle = "SubFix · 热词库",
        Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({620, 330, 520, 290}),
    },
    ui:VGroup{
        Font = DIALOG_FONT,
        StyleSheet = SUBFIX_ROOT_STYLESHEET,
        Spacing = 6,
        ContentsMargins = 10,
        ui:Tree{ID = "GenerateHotwordTree", Weight = 1, MinimumSize = {0, 90}, Events = {ItemClicked = true}},
        ui:HGroup{
            Weight = 0, Spacing = 8,
            ui:Label{Text = "标准词：", Weight = 0},
            ui:LineEdit{ID = "GenerateHotwordTermInput", PlaceholderText = "例如 DaVinci Resolve", Weight = 1}
        },
        ui:HGroup{
            Weight = 0, Spacing = 8,
            MinimumSize = {0, 34},
            ui:Button{ID = "GenerateHotwordAddBtn", Text = "新增", Weight = 1, MinimumSize = {0, 28}},
            ui:Button{ID = "GenerateHotwordDeleteBtn", Text = "删除", Weight = 1, MinimumSize = {0, 28}},
            ui:Button{ID = "GenerateHotwordClearBtn", Text = "清空", Weight = 1, MinimumSize = {0, 28}}
        },
        ui:VGap(10),
        ui:HGroup{
            Weight = 0, Spacing = 8,
            MinimumSize = {0, 34},
            ui:Button{ID = "GenerateHotwordCloseBtn", Text = "完成", Weight = 1, MinimumSize = {0, 28}}
        },
        ui:Label{ID = "GenerateHotwordStatusLabel", Text = "", Weight = 0}
    })
    self.hotword_library_window = hotword_library_window
    local library_items = hotword_library_window:GetItems()
    local hotword_tree = library_items and library_items.GenerateHotwordTree or nil
    local term_input = library_items and library_items.GenerateHotwordTermInput or nil
    local status_label = library_items and library_items.GenerateHotwordStatusLabel or nil
    local item_map, selected_hotword_index = {}, nil
    local hotword_library_dirty = false
    if hotword_tree then
        pcall(function() hotword_tree.ColumnCount = 1 end)
        pcall(function() hotword_tree.HeaderHidden = true end)
    end

    local function set_hotword_status(text)
        if status_label then pcall(function() status_label.Text = tostring(text or "") end) end
    end
    local function refresh_hotword_tree()
        item_map = {}
        if hotword_tree then pcall(function() hotword_tree:Clear() end) end
        for index, entry in ipairs(controller.hotword_entries) do
            local ok_item, item = pcall(function() return hotword_tree:NewItem() end)
            if ok_item and item then
                set_tree_item_text(item, 0, tostring(entry.term))
                pcall(function() hotword_tree:AddTopLevelItem(item) end)
                item_map[item] = index
            end
        end
        safe_refresh_tree_widget(hotword_tree)
        controller:refresh_hotword_toggle()
    end
    local function append_hotword_tree_entry(index, entry)
        if not hotword_tree or not entry then return end
        local ok_item, item = pcall(function() return hotword_tree:NewItem() end)
        if not ok_item or not item then return end
        set_tree_item_text(item, 0, tostring(entry.term))
        pcall(function() hotword_tree:AddTopLevelItem(item) end)
        item_map[item] = index
        safe_refresh_tree_widget(hotword_tree)
    end
    local function save_hotword_library()
        if not hotword_library_dirty then return true end
        if not save_generate_hotword_entries(controller.hotword_entries) then
            set_hotword_status("保存失败，请检查本机插件支持目录权限")
            return false
        end
        hotword_library_dirty = false
        return true
    end
    function hotword_library_window.On.GenerateHotwordTree.ItemClicked(ev)
        local item = get_tree_event_value(ev, {"item", "Item", "currentItem", "CurrentItem"}) or get_selected_tree_node(hotword_tree)
        selected_hotword_index = item and item_map[item] or nil
    end
    function hotword_library_window.On.GenerateHotwordAddBtn.Clicked(ev)
        local term = trim_text(term_input and term_input.Text or "")
        if term == "" then set_hotword_status("请填写标准词") return end
        controller.hotword_entries[#controller.hotword_entries + 1] = {term = term, aliases = {}}
        selected_hotword_index = #controller.hotword_entries
        hotword_library_dirty = true
        controller.hotwords_enabled = #controller.hotword_entries > 0
        append_hotword_tree_entry(selected_hotword_index, controller.hotword_entries[selected_hotword_index])
        if term_input then pcall(function() term_input.Text = "" end) end
        controller:refresh_hotword_toggle()
        set_hotword_status("已添加，点击完成保存")
    end
    function hotword_library_window.On.GenerateHotwordDeleteBtn.Clicked(ev)
        if not selected_hotword_index then set_hotword_status("请先选择要删除的词条") return end
        table.remove(controller.hotword_entries, selected_hotword_index)
        selected_hotword_index = nil
        if term_input then pcall(function() term_input.Text = "" end) end
        hotword_library_dirty = true
        controller.hotwords_enabled = #controller.hotword_entries > 0
        refresh_hotword_tree()
        set_hotword_status("已删除，点击完成保存")
    end
    local function hide_hotword_clear_confirmation()
        if controller.hotword_clear_confirm_window then
            pcall(function() controller.hotword_clear_confirm_window:Hide() end)
        end
    end
    local function show_hotword_clear_confirmation()
        if #controller.hotword_entries == 0 then
            set_hotword_status("当前没有热词可清空")
            return
        end
        if not controller.hotword_clear_confirm_window then
            controller.hotword_clear_confirm_window = dispatcher:AddWindow({
                ID = "GenerateHotwordClearConfirmWindow",
                WindowTitle = "SubFix · 清空热词库",
                Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({720, 390, 360, 80}),
            },
            ui:VGroup{
                Font = DIALOG_FONT,
                StyleSheet = SUBFIX_ROOT_STYLESHEET,
                Spacing = 10,
                ContentsMargins = 14,
                ui:Label{Text = "确定清空全部热词吗？此操作无法撤销。", WordWrap = true, Weight = 0},
                ui:HGroup{
                    Weight = 0, Spacing = 8,
                    MinimumSize = {0, 34},
                    ui:Button{ID = "GenerateHotwordClearCancelBtn", Text = "取消", Weight = 1, MinimumSize = {0, 28}},
                    ui:Button{ID = "GenerateHotwordClearConfirmBtn", Text = "清空全部", Weight = 1, MinimumSize = {0, 28}}
                },
                ui:VGap(8, 0)
            })
            function controller.hotword_clear_confirm_window.On.GenerateHotwordClearCancelBtn.Clicked(ev)
                hide_hotword_clear_confirmation()
            end
            function controller.hotword_clear_confirm_window.On.GenerateHotwordClearConfirmBtn.Clicked(ev)
                controller.hotword_entries = {}
                selected_hotword_index = nil
                if term_input then pcall(function() term_input.Text = "" end) end
                hotword_library_dirty = true
                controller.hotwords_enabled = false
                refresh_hotword_tree()
                set_hotword_status("已清空，点击完成保存")
                hide_hotword_clear_confirmation()
            end
            function controller.hotword_clear_confirm_window.On.GenerateHotwordClearConfirmWindow.Close(ev)
                hide_hotword_clear_confirmation()
            end
        end
        pcall(function() controller.hotword_clear_confirm_window:Show() end)
    end
    function hotword_library_window.On.GenerateHotwordClearBtn.Clicked(ev)
        show_hotword_clear_confirmation()
    end
    local function close_hotword_library()
        if controller.hotword_library_closing then return end
        controller.hotword_library_closing = true
        hide_hotword_clear_confirmation()
        if not save_hotword_library() then
            controller.hotword_library_closing = false
            return
        end
        pcall(function() hotword_library_window:Hide() end)
    end
    function hotword_library_window.On.GenerateHotwordCloseBtn.Clicked(ev)
        close_hotword_library()
    end
    function hotword_library_window.On.GenerateHotwordLibraryWindow.Close(ev)
        close_hotword_library()
    end
    refresh_hotword_tree()
    pcall(function() hotword_library_window:Show() end)
end

-- 文稿匹配编辑子窗口。
function GenerationPanelController:open_script_match_dialog()
    if self.script_match_window then
        pcall(function() self.script_match_window:Show() end)
        return
    end
    local controller = self
    local script_match_window = dispatcher:AddWindow({
        ID = "GenerateScriptMatchWindow",
        WindowTitle = "SubFix · 文稿匹配",
        Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({620, 340, 560, 420}),
    },
    ui:VGroup{
        Font = DIALOG_FONT,
        StyleSheet = SUBFIX_ROOT_STYLESHEET,
        Spacing = 8,
        ContentsMargins = 12,
        ui:Label{Text = "粘贴文稿，或从 .txt/.srt 导入。字幕文字将严格等于文稿，并按选区音频时间轴对齐。", WordWrap = true, Weight = 0, MinimumSize = {0, 46}},
        ui:TextEdit{ID = "ScriptMatchInput", PlainText = controller.script_match_text or "", Weight = 1, MinimumSize = {0, 150}},
        ui:HGroup{
            Weight = 0, Spacing = 8,
            ui:Label{Text = "或输入文件路径：", Weight = 0},
            ui:LineEdit{ID = "ScriptMatchPathInput", PlaceholderText = "例如 C:\\script.txt", Weight = 1}
        },
        ui:HGroup{
            Weight = 0, Spacing = 8, MinimumSize = {0, 34},
            ui:Button{ID = "ScriptMatchImportBtn", Text = "浏览…", Weight = 0, MinimumSize = {0, 28}},
            ui:Button{ID = "ScriptMatchReadBtn", Text = "读取文件", Weight = 0, MinimumSize = {0, 28}},
            ui:Button{ID = "ScriptMatchClearBtn", Text = "清空", Weight = 0, MinimumSize = {0, 28}}
        },
        ui:Label{ID = "ScriptMatchStatusLabel", Text = "", Weight = 0},
        ui:HGroup{
            Weight = 0, Spacing = 8, MinimumSize = {0, 34},
            ui:Button{ID = "ScriptMatchConfirmBtn", Text = "确定", Weight = 1, MinimumSize = {0, 28}, StyleSheet = SUBFIX_BTN_ACCENT},
            ui:Button{ID = "ScriptMatchCancelBtn", Text = "取消", Weight = 1, MinimumSize = {0, 28}}
        },
        ui:VGap(8, 0)
    })
    self.script_match_window = script_match_window
    local sm_items = script_match_window:GetItems()
    local sm_input = sm_items and sm_items.ScriptMatchInput or nil
    local sm_path = sm_items and sm_items.ScriptMatchPathInput or nil
    local sm_status = sm_items and sm_items.ScriptMatchStatusLabel or nil
    local function set_status(msg) if sm_status then pcall(function() sm_status.Text = msg or "" end) end end
    local function import_file(path)
        if not path or path == "" then return end
        local content = read_text_file(path)
        if not content then set_status("无法读取文件：" .. tostring(path)) return end
        if string.match(string.lower(path), "%.srt$") then
            content = extract_srt_text(content)
        end
        if sm_input then pcall(function() sm_input.PlainText = content end) end
        if sm_path then pcall(function() sm_path.Text = path end) end
        set_status("已导入：" .. tostring(path))
    end
    function script_match_window.On.ScriptMatchReadBtn.Clicked(ev)
        import_file(trim_text(sm_path and sm_path.Text or ""))
    end
    function script_match_window.On.ScriptMatchImportBtn.Clicked(ev)
        local ok, path = pcall(function() return fusion:RequestFile("Open", "", "Text (*.txt *.srt)") end)
        if ok and path and path ~= "" then
            import_file(path)
        elseif not ok then
            set_status("无法打开文件选择对话框，请手动粘贴或输入路径后点“读取文件”")
        end
    end
    function script_match_window.On.ScriptMatchClearBtn.Clicked(ev)
        if sm_input then pcall(function() sm_input.PlainText = "" end) end
        if sm_path then pcall(function() sm_path.Text = "" end) end
        set_status("已清空")
    end
    function script_match_window.On.ScriptMatchConfirmBtn.Clicked(ev)
        local text = sm_input and sm_input.PlainText or ""
        controller.script_match_text = text
        if trim_text(text) ~= "" then
            controller.script_match_enabled = true
        end
        controller:refresh_script_match_toggle()
        pcall(function() script_match_window:Hide() end)
    end
    function script_match_window.On.ScriptMatchCancelBtn.Clicked(ev)
        pcall(function() script_match_window:Hide() end)
    end
    function script_match_window.On.GenerateScriptMatchWindow.Close(ev)
        pcall(function() script_match_window:Hide() end)
    end
    pcall(function() script_match_window:Show() end)
end

-- 豆包 Key 配置子窗口（按需创建）。
function GenerationPanelController:ensure_doubao_key_window()
    if self.doubao_key_window then return self.doubao_key_window end
    local controller = self
    local doubao_key_window = dispatcher:AddWindow({
        ID = "GenerateDoubaoKeyWindow",
        WindowTitle = "SubFix · 配置豆包 API Key",
        Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({500, 300, 420, 228}),
        MinimumSize = {420, 228},
    },
    ui:VGroup{
        Font = DIALOG_FONT,
        StyleSheet = SUBFIX_ROOT_STYLESHEET,
        Spacing = 8,
        ContentsMargins = 12,
        ui:Label{Text = "填写火山引擎（豆包）语音识别 API Key", Weight = 0},
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            ui:Label{Text = "API Key：", Weight = 0, MinimumSize = {96, 0}},
            ui:LineEdit{ID = "GenerateDoubaoApiKeyInput", PlaceholderText = "火山引擎 API Key", Weight = 1}
        },
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            MinimumSize = {0, 36},
            ui:Label{Text = "识别版本：", Weight = 0, MinimumSize = {96, 0}},
            ui:ComboBox{ID = "GenerateDoubaoBackendSelector", Weight = 1, MinimumSize = {0, 32}}
        },
        ui:VGap(8),
        ui:VGroup{
            Weight = 0,
            Spacing = 0,
            ui:HGroup{
                Weight = 0,
                MinimumSize = {0, 28},
                ui:Button{
                    ID = "GenerateDoubaoApiKeyGuideBtn",
                    Text = "获取豆包语音 API Key ↗",
                    Weight = 1,
                    MinimumSize = {0, 28},
                },
            },
            ui:VGap(12),
            ui:Label{ID = "GenerateDoubaoKeyStatusLabel", Text = "密钥仅保存在本机，不会上传或进入版本库。", Weight = 0, MinimumSize = {0, 18}},
            ui:VGap(4),
        },
        ui:HGroup{
            Weight = 0,
            MinimumSize = {0, 36},
            Spacing = 8,
            ui:Button{ID = "GenerateDoubaoKeySaveBtn", Text = "保存", Weight = 1, MinimumSize = {0, 36}, StyleSheet = SUBFIX_BTN_ACCENT},
            ui:Button{ID = "GenerateDoubaoKeyCancelBtn", Text = "取消", Weight = 1, MinimumSize = {0, 36}}
        }
    })
    self.doubao_key_window = doubao_key_window
    local doubao_items = doubao_key_window:GetItems()
    local api_key_input = doubao_items and doubao_items.GenerateDoubaoApiKeyInput or nil
    local backend_selector = doubao_items and doubao_items.GenerateDoubaoBackendSelector or nil
    local status_label = doubao_items and doubao_items.GenerateDoubaoKeyStatusLabel or nil
    self.pending_doubao_backend = self.selected_doubao_backend
    if backend_selector then
        backend_selector:AddItem("极速版")
        backend_selector:AddItem("2.0 标准版")
    end

    local function set_key_status(text)
        if status_label then
            pcall(function() status_label.Text = tostring(text or "") end)
        end
    end
    local function refresh_backend_selector()
        if not backend_selector then return end
        local backend_index = controller.pending_doubao_backend == "doubao_asr_v2" and 1 or 0
        pcall(function() backend_selector.CurrentIndex = backend_index end)
    end
    self.refresh_doubao_backend_selector = refresh_backend_selector

    function doubao_key_window.On.GenerateDoubaoApiKeyGuideBtn.Clicked(ev)
        local ok = pcall(subfix_open_url, DOUBAO_API_KEY_GUIDE_URL)
        if not ok then
            set_key_status("无法打开浏览器，请手动访问火山引擎豆包语音控制台")
        end
    end
    function doubao_key_window.On.GenerateDoubaoBackendSelector.CurrentIndexChanged(ev)
        if not backend_selector then return end
        local backend_index = tonumber(backend_selector.CurrentIndex) or 0
        controller.pending_doubao_backend = backend_index == 1 and "doubao_asr_v2" or "doubao_asr"
    end
    function doubao_key_window.On.GenerateDoubaoKeySaveBtn.Clicked(ev)
        local api_key = trim_text(api_key_input and api_key_input.Text or "")
        if api_key == "" then
            set_key_status("请输入 API Key")
            return
        end
        local previous_api_key = read_doubao_api_key()
        local previous_doubao_backend = controller.selected_doubao_backend
        local ok, err = save_doubao_api_key(api_key)
        if not ok then
            set_key_status(tostring(err or "保存失败"))
            return
        end
        controller.selected_doubao_backend = normalize_doubao_backend(controller.pending_doubao_backend)
        local pref_ok, pref_err = save_doubao_backend_preference(controller.selected_doubao_backend)
        if not pref_ok then
            local rollback_ok, rollback_err = save_doubao_api_key(previous_api_key)
            controller.selected_doubao_backend = previous_doubao_backend
            local preference_error = tostring(pref_err or "模型偏好保存失败，请检查本机插件支持目录权限")
            if not rollback_ok then
                set_key_status(preference_error .. "；API Key 回滚失败：" .. tostring(rollback_err or "未知错误"))
            else
                set_key_status(preference_error)
            end
            return
        end
        pcall(function() doubao_key_window:Hide() end)
    end
    local function cancel_doubao_key_dialog()
        pcall(function() doubao_key_window:Hide() end)
        if not doubao_credentials_configured() then
            controller:select_engine(1)
            controller:set_info("未配置豆包密钥，已切回 Qwen（本地）")
        end
    end
    function doubao_key_window.On.GenerateDoubaoKeyCancelBtn.Clicked(ev)
        cancel_doubao_key_dialog()
    end
    function doubao_key_window.On.GenerateDoubaoKeyWindow.Close(ev)
        cancel_doubao_key_dialog()
    end
    return doubao_key_window
end

function GenerationPanelController:open_doubao_key_dialog()
    local doubao_key_window = self:ensure_doubao_key_window()
    local doubao_items = doubao_key_window:GetItems()
    local api_key_input = doubao_items and doubao_items.GenerateDoubaoApiKeyInput or nil
    local status_label = doubao_items and doubao_items.GenerateDoubaoKeyStatusLabel or nil
    local api_key = read_doubao_api_key()
    if api_key_input then pcall(function() api_key_input.Text = api_key end) end
    self.pending_doubao_backend = self.selected_doubao_backend
    if self.refresh_doubao_backend_selector then self.refresh_doubao_backend_selector() end
    if status_label then pcall(function() status_label.Text = "密钥仅保存在本机，不会上传或进入版本库。" end) end
    pcall(function() doubao_key_window:Show() end)
end

function GenerationPanelController:ensure_doubao_reminder_window()
    if self.doubao_reminder_window then return self.doubao_reminder_window end
    local doubao_reminder_window = dispatcher:AddWindow({
        ID = "GenerateDoubaoReminderWindow",
        WindowTitle = "SubFix · 豆包密钥未配置",
        Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({520, 320, 380, 150}),
    },
    ui:VGroup{
        Font = DIALOG_FONT,
        StyleSheet = SUBFIX_ROOT_STYLESHEET,
        Spacing = 8,
        ContentsMargins = 12,
        ui:Label{Text = "已选择「豆包（云端）」，但尚未配置密钥。", Weight = 0},
        ui:Label{Text = "请双击「豆包（云端）」按钮填写 API Key。", Weight = 0},
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            ui:HGap(0, 1),
            ui:Button{ID = "GenerateDoubaoReminderOkBtn", Text = "知道了", Weight = 0, MinimumSize = {88, 28}}
        }
    })
    self.doubao_reminder_window = doubao_reminder_window
    function doubao_reminder_window.On.GenerateDoubaoReminderOkBtn.Clicked(ev)
        pcall(function() doubao_reminder_window:Hide() end)
    end
    function doubao_reminder_window.On.GenerateDoubaoReminderWindow.Close(ev)
        pcall(function() doubao_reminder_window:Hide() end)
    end
    return doubao_reminder_window
end

-- Qwen 下载/安装确认子窗口。
function GenerationPanelController:ensure_qwen_download_window()
    if self.qwen_download_window then return self.qwen_download_window end
    local controller = self
    local qwen_download_window = dispatcher:AddWindow({
        ID = "GenerateQwenDownloadWindow",
        WindowTitle = "SubFix · 安装本地 Qwen",
        Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({560, 330, 420, 100}),
    },
    ui:VGroup{
        Font = DIALOG_FONT,
        StyleSheet = SUBFIX_ROOT_STYLESHEET,
        Spacing = 8,
        ContentsMargins = 14,
        ui:Label{ID = "GenerateQwenDownloadStatusLabel", Text = "本地 Qwen 识别需要下载运行环境与 Qwen3-ASR-1.7B 模型。", Weight = 0},
        ui:Label{ID = "GenerateQwenDownloadDetailLabel", Text = "下载完成后会自动选中 Qwen；规整字幕长度无需下载。", Weight = 0},
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            MinimumSize = {0, 34},
            ui:Button{ID = "GenerateQwenDownloadConfirmBtn", Text = "下载并使用", Weight = 1, MinimumSize = {0, 28}, StyleSheet = SUBFIX_BTN_ACCENT},
            ui:Button{ID = "GenerateQwenDownloadCancelBtn", Text = "取消", Weight = 1, MinimumSize = {0, 28}}
        }
    })
    self.qwen_download_window = qwen_download_window
    local qwen_items = qwen_download_window:GetItems()
    local status_label = qwen_items and qwen_items.GenerateQwenDownloadStatusLabel or nil
    local detail_label = qwen_items and qwen_items.GenerateQwenDownloadDetailLabel or nil
    local confirm_btn = qwen_items and qwen_items.GenerateQwenDownloadConfirmBtn or nil

    local function show_qwen_status(qwen_status)
        controller.qwen_download_is_ready = qwen_status and qwen_status.ready == true
        if controller.qwen_download_is_ready then
            if status_label then pcall(function() status_label.Text = "本地 Qwen 已安装，可直接开始生成。" end) end
            if detail_label then pcall(function() detail_label.Text = "Qwen3-ASR-1.7B 与运行环境均已就绪，无需重复下载。" end) end
            if confirm_btn then
                pcall(function() confirm_btn.Text = "知道了" end)
                pcall(function() confirm_btn.Enabled = true end)
            end
        else
            if status_label then pcall(function() status_label.Text = "本地 Qwen 识别需要下载运行环境与 Qwen3-ASR-1.7B 模型。" end) end
            if detail_label then pcall(function() detail_label.Text = "下载完成后会自动选中 Qwen；规整字幕长度无需下载。" end) end
            if confirm_btn then
                pcall(function() confirm_btn.Text = "下载并使用" end)
                pcall(function() confirm_btn.Enabled = true end)
            end
        end
    end

    function qwen_download_window.On.GenerateQwenDownloadConfirmBtn.Clicked(ev)
        if controller.qwen_download_is_ready then
            pcall(function() qwen_download_window:Hide() end)
            return
        end
        if controller:embedded() then
            -- embedded：不退出任何主循环，隐藏本窗后交给宿主回调执行安装，安装成功后直接开跑。
            pcall(function() qwen_download_window:Hide() end)
            if type(controller.callbacks) == "table" and type(controller.callbacks.request_qwen_install) == "function" then
                controller.callbacks.request_qwen_install()
            end
            return
        end
        -- standalone：通知外层 RunLoop 退出并返回安装哨兵，由 generate_selection_subtitles 循环安装。
        controller.qwen_install_requested = true
        pcall(function() qwen_download_window:Hide() end)
        pcall(function() controller.window:Hide() end)
        pcall(function() dispatcher:ExitLoop() end)
    end
    function qwen_download_window.On.GenerateQwenDownloadCancelBtn.Clicked(ev)
        pcall(function() qwen_download_window:Hide() end)
    end
    function qwen_download_window.On.GenerateQwenDownloadWindow.Close(ev)
        pcall(function() qwen_download_window:Hide() end)
    end

    self.show_qwen_download_status = show_qwen_status
    return qwen_download_window
end

function GenerationPanelController:open_qwen_download_dialog()
    local qwen_download_window = self:ensure_qwen_download_window()
    self.qwen_download_is_ready = false
    if self.show_qwen_download_status then
        local qwen_items = qwen_download_window:GetItems()
        local status_label = qwen_items and qwen_items.GenerateQwenDownloadStatusLabel or nil
        local detail_label = qwen_items and qwen_items.GenerateQwenDownloadDetailLabel or nil
        local confirm_btn = qwen_items and qwen_items.GenerateQwenDownloadConfirmBtn or nil
        if status_label then pcall(function() status_label.Text = "正在检查本地 Qwen 安装状态…" end) end
        if detail_label then pcall(function() detail_label.Text = "请稍候，窗口会自动更新。" end) end
        if confirm_btn then
            pcall(function() confirm_btn.Text = "检查中…" end)
            pcall(function() confirm_btn.Enabled = false end)
        end
        pcall(function() qwen_download_window:Show() end)
        self.show_qwen_download_status(qwen_check.inspect(resolve_asr_paths()))
    else
        pcall(function() qwen_download_window:Show() end)
    end
end

function GenerationPanelController:hide_all_subwindows()
    pcall(function() if self.hotword_library_window then self.hotword_library_window:Hide() end end)
    pcall(function() if self.hotword_clear_confirm_window then self.hotword_clear_confirm_window:Hide() end end)
    pcall(function() if self.script_match_window then self.script_match_window:Hide() end end)
    pcall(function() if self.doubao_key_window then self.doubao_key_window:Hide() end end)
    pcall(function() if self.doubao_reminder_window then self.doubao_reminder_window:Hide() end end)
    pcall(function() if self.qwen_download_window then self.qwen_download_window:Hide() end end)
end

-- 绑定主面板事件。standalone 下 window 为选择窗口（同时绑定窗口 Close 退出 RunLoop）；
-- embedded 下 window 为宿主主窗口，绝不绑定窗口根 Close（归属宿主）。
function GenerationPanelController:bind(window, callbacks)
    self.window = window
    self.callbacks = callbacks or {}
    self.items = window:GetItems()

    local tree = self.items and self.items.GenerateAudioTrackTree or nil
    if tree then
        pcall(function() tree.ColumnCount = 1 end)
        pcall(function() tree.HeaderHidden = true end)
        pcall(function() tree.RootIsDecorated = false end)
        pcall(function() tree.ItemsExpandable = false end)
        pcall(function() tree.ColumnWidth[0] = 360 end)
        pcall(function() tree.SelectionMode = "ExtendedSelection" end)
        pcall(function() tree.SelectionBehavior = "SelectRows" end)
    end

    self:refresh_length_buttons()
    self:refresh_engine_buttons()
    self:refresh_hotword_toggle()
    self:refresh_script_match_toggle()

    -- 字幕长度
    function window.On.GenerateSubtitleLengthStandardBtn.Clicked(ev)
        self:select_length(1)
    end
    function window.On.GenerateSubtitleLengthShortBtn.Clicked(ev)
        self:select_length(2)
    end

    -- 热词库
    function window.On.GenerateHotwordToggleBtn.Clicked(ev)
        if #self.hotword_entries == 0 then
            self:open_hotword_library()
            return
        end
        self.hotwords_enabled = not self.hotwords_enabled
        self:refresh_hotword_toggle()
    end
    function window.On.GenerateHotwordManageBtn.Clicked(ev)
        self:open_hotword_library()
    end

    -- 文稿匹配
    function window.On.GenerateScriptMatchToggleBtn.Clicked(ev)
        self.script_match_enabled = not self.script_match_enabled
        if self.script_match_enabled and trim_text(self.script_match_text) == "" then
            self:open_script_match_dialog()
        end
        self:refresh_script_match_toggle()
    end
    function window.On.GenerateScriptMatchEditBtn.Clicked(ev)
        self:open_script_match_dialog()
    end

    -- 识别模型（秒级近似双击：Qwen 双击打开安装窗；豆包双击打开 Key 配置窗）
    function window.On.GenerateSubtitleEngineQwenBtn.Clicked(ev)
        local now = os.time()
        local is_double = self.last_qwen_click_time ~= nil and (now - self.last_qwen_click_time) <= DOUBAO_DOUBLE_CLICK_SECONDS
        self.last_qwen_click_time = now
        self:select_engine(1)
        if is_double then
            self.last_qwen_click_time = nil
            self:open_qwen_download_dialog()
        end
    end
    function window.On.GenerateSubtitleEngineDoubaoBtn.Clicked(ev)
        local now = os.time()
        local is_double = self.last_doubao_click_time ~= nil and (now - self.last_doubao_click_time) <= DOUBAO_DOUBLE_CLICK_SECONDS
        self.last_doubao_click_time = now
        self:select_engine(2)
        if is_double then
            self.last_doubao_click_time = nil
            pcall(function() local reminder = self:ensure_doubao_reminder_window(); reminder:Hide() end)
            self:open_doubao_key_dialog()
        elseif not doubao_credentials_configured() and not self.doubao_reminder_shown then
            self.doubao_reminder_shown = true
            local reminder = self:ensure_doubao_reminder_window()
            pcall(function() reminder:Show() end)
        end
    end

    -- 生成/开始生成
    function window.On.GenerateSelectionConfirmBtn.Clicked(ev)
        if type(self.callbacks) == "table" and type(self.callbacks.request_confirm) == "function" then
            self.callbacks.request_confirm()
            return
        end
        -- standalone 默认出口：校验通过后退出 RunLoop
        local options, why = self:collect_start_options()
        if why == "need_qwen" then
            self:open_qwen_download_dialog()
            return
        end
        if not options then
            self:set_info(why)
            return
        end
        self.confirmed_options = options
        pcall(function() window:Hide() end)
        pcall(function() dispatcher:ExitLoop() end)
    end

    function window.On.GenerateSelectionCancelBtn.Clicked(ev)
        if self:embedded() then
            if type(self.callbacks) == "table" and type(self.callbacks.request_cancel) == "function" then
                self.callbacks.request_cancel()
            end
            return
        end
        self.dialog_cancelled = true
        self.confirmed_options = nil
        pcall(function() window:Hide() end)
        pcall(function() dispatcher:ExitLoop() end)
    end

    if self:embedded() then
        function window.On.GenerateEmbeddedRescanBtn.Clicked(ev)
            if type(self.callbacks) == "table" and type(self.callbacks.rescan) == "function" then
                self.callbacks.rescan()
            end
        end
    else
        function window.On.GenerateSelectionWindow.Close(ev)
            self.dialog_cancelled = true
            self.confirmed_options = nil
            pcall(function() window:Hide() end)
            pcall(function() dispatcher:ExitLoop() end)
        end
    end
end

-- standalone 弹窗的最终返回值（严格 8 值：第 8 个才是错误信息/哨兵，
-- 旧版错误信息错位在第 6 位会导致误报「已取消」、Qwen 安装哨兵失效）。
function GenerationPanelController:build_dialog_result()
    if self.qwen_install_requested then
        return nil, nil, nil, nil, nil, nil, nil, "__subfix_install_qwen__"
    end

    local options = self.confirmed_options
    if not options and not self.dialog_cancelled then
        -- 窗口未点确认就退出时，沿用当前树勾选作为默认值。
        local fallback, fallback_err = self:collect_current_options()
        if fallback then
            options = fallback
            print("[SubFix Generate] 音频轨选择窗口提前退出，使用默认勾选轨道")
        elseif fallback_err and fallback_err ~= "现场模式至少选择一条音频轨道" then
            return nil, nil, nil, nil, nil, nil, nil, fallback_err
        end
    end

    if not options then
        return nil, nil, nil, nil, nil, nil, nil, "已取消"
    end
    if #options.sources == 0 then
        return nil, nil, nil, nil, nil, nil, nil, "请至少选择一个音频轨道"
    end
    return options.sources, options.subtitle_mode, options.max_chars, options.backend,
        options.hotwords_json, options.script_match_enabled, options.script_match_text, nil
end

-- standalone 包装：保持旧版 run(options) 的强模态流程与返回值约定。
local function show_audio_track_selection_dialog(audio_sources, scope, fps)
    if not dispatcher or not ui then
        return nil, nil, nil, nil, nil, nil, nil, "无法初始化 Resolve UI"
    end
    if type(audio_sources) ~= "table" or #audio_sources == 0 then
        return nil, nil, nil, nil, nil, nil, nil, "未找到与选区重叠的本地音频片段"
    end

    local controller = new_generation_panel_controller("standalone")
    local selection_window = dispatcher:AddWindow({
        ID = "GenerateSelectionWindow",
        WindowTitle = "SubFix · 生成选区字幕",
        Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({460, 250, 440, 560}),
    }, controller:build_descriptor())
    controller:bind(selection_window, nil)
    if not controller.items or not controller.items.GenerateAudioTrackTree then
        return nil, nil, nil, nil, nil, nil, nil, "无法初始化音频轨道列表"
    end
    controller:set_context(audio_sources, scope, fps)

    selection_window:Show()
    dispatcher:RunLoop()
    pcall(function() selection_window:Hide() end)
    controller:hide_all_subwindows()
    return controller:build_dialog_result()
end

-- 跨块暴露（do...end 外的管线/embedded API 需要）：
SubFixGenerateSelectionCore._new_generation_panel_controller = new_generation_panel_controller
SubFixGenerateSelectionCore._show_audio_track_selection_dialog = show_audio_track_selection_dialog
end


local function progress_elapsed_text(started_at)
    local elapsed = math.max(0, os.time() - (tonumber(started_at) or os.time()))
    if elapsed >= 60 then
        return string.format("%dm%02ds", math.floor(elapsed / 60), math.floor(elapsed % 60))
    end
    return string.format("%ds", math.floor(elapsed + 0.5))
end

local function parse_progress_payload(progress_text)
    local text = tostring(progress_text or "")
    if text == "" then return nil end
    -- 进度文件由 Python json.dumps 整体覆写；读取时可能恰好碰到写入中途（空串/截断），
    -- 完整解析失败即返回 nil，等下一次轮询，不再用正则（旧正则会被消息里的引号/反斜杠/
    -- 转义字符干扰，indent=2 与非 ASCII 场景也脆弱）。
    local ok, payload = pcall(decode_json_text, text)
    if not ok or type(payload) ~= "table" then return nil end
    local function number_field(name)
        return tonumber(payload[name])
    end
    return {
        stage = tostring(payload.stage or ""),
        message = tostring(payload.message or ""),
        batch_index = number_field("batch_index"),
        total_batches = number_field("total_batches"),
        progress_index = number_field("progress_index"),
        progress_total = number_field("progress_total"),
        eta_seconds = number_field("eta_seconds"),
        indeterminate = payload.indeterminate == true
    }
end

local function progress_bar_text(progress_state, payload)
    -- 定宽字符条：仅用 █/░ 两种等宽阴影块（Courier New 下 advance 稳定），
    -- 不再用 □/ᗧ/⚑ 这类依赖字体回退、跨平台会错位的字形。百分比由 MetaLabel 承载。
    local width = GENERATE_PROGRESS_BAR_WIDTH
    if payload and payload.indeterminate then
        local elapsed_seconds = math.max(0, os.time() - (tonumber(progress_state.started_at) or os.time()))
        local phase = elapsed_seconds % width + 1
        local cells = {}
        for cell_index = 1, width do
            cells[#cells + 1] = cell_index == phase and "█" or "░"
        end
        -- 后两个返回值供分段条使用：fraction 不推进（扫块动画），is_indeterminate=true。
        return table.concat(cells), 0, 0, true
    end
    local total = tonumber(payload and payload.progress_total)
    local index = tonumber(payload and payload.progress_index)
    if not total or not index or total <= 0 then
        total = tonumber(payload and payload.total_batches)
        index = tonumber(payload and payload.batch_index)
    end
    local fraction = 0
    if total and index and total > 0 then
        fraction = math.max(0, math.min(1, index / total))
    end
    local stage = tostring(payload and payload.stage or "")
    if stage ~= "完成" and stage ~= "已取消" and stage ~= "失败" then
        fraction = math.min(fraction, 0.98)
    end
    progress_state.progress_total = 100
    progress_state.progress_fraction = math.max(tonumber(progress_state.progress_fraction) or 0, fraction)
    local percent = math.floor(progress_state.progress_fraction * 100 + 0.5)
    local filled = math.floor(progress_state.progress_fraction * width + 0.5)
    filled = math.max(0, math.min(width, filled))
    -- 额外返回 fraction（供 embedded 分段条按比例点亮）与 is_indeterminate=false。
    return string.rep("█", filled) .. string.rep("░", width - filled), percent,
        progress_state.progress_fraction, false
end

local function progress_eta_text(seconds)
    local remaining = math.max(0, math.floor(tonumber(seconds) or 0))
    if remaining >= 60 then
        return string.format("%dm%02ds", math.floor(remaining / 60), remaining % 60)
    end
    return string.format("%ds", remaining)
end

-- embedded 进度状态：不另开窗口，把进度文案映射到右侧面板的 GenerateEmbedded* 控件，
-- 取消按钮复用面板自身的 GenerateSelectionCancelBtn（bind 时已接到 request_cancel）。
-- 用 do...end 包住，避免增加主 chunk local（Lua 5.1 上限 200）。
do
local function create_embedded_progress_state(controller)
    if not controller or not controller.window then
        return nil, "无法初始化内嵌进度面板"
    end
    local ok_items, host_items = pcall(function() return controller.window:GetItems() end)
    if not ok_items or not host_items then
        return nil, "无法初始化内嵌进度面板"
    end
    local block = host_items.GenerateEmbeddedProgressBlock
    local status_label = host_items.GenerateEmbeddedStatusLabel
    local meta_label = host_items.GenerateEmbeddedProgressMetaLabel
    if not block or not status_label or not meta_label then
        return nil, "内嵌进度面板缺少必要控件"
    end
    local started_at = os.time()
    -- 内嵌进度已移除条形（Fusion 运行时改 Weight/像素宽均不可靠），只显示两行文字：
    -- 状态行 + 橙色加粗的百分比/用时行（颜色在构建期静态 QSS 赋予）。
    pcall(function() block.Hidden = false end)
    pcall(function() status_label.Text = "准备中" end)
    pcall(function() meta_label.Text = "进度 0%  ·  用时 0s" end)

    local state = {
        embedded = true,
        cancel_requested = false,
        cancel_allowed = true,
        started_at = started_at,
        progress_fraction = 0,
        finished = false
    }
    -- 假 window：只提供既有进度函数会用到的接口；Hide 不做任何事，
    -- 面板收尾（隐藏进度块/复位按钮）由 embedded runner 统一负责。
    -- 内嵌不再有条形：无 Painter / 字符 Label，update 时只更新两行文字。
    state.window = {
        GetItems = function()
            return {
                GenerateProgressStatusLabel = status_label,
                GenerateProgressMetaLabel = meta_label,
                GenerateProgressCancelBtn = host_items.GenerateSelectionCancelBtn,
            }
        end,
        Hide = function() end,
        Show = function() end,
        SetAttrs = function() end,
    }
    SubFixGenerateSelectionCore._active_embedded_progress_state = state
    return state
end
SubFixGenerateSelectionCore._create_embedded_progress_state = create_embedded_progress_state
end

local function show_generate_progress_window(embedded_ctx)
    if not dispatcher or not ui then
        return nil, "无法初始化 Resolve UI"
    end
    if embedded_ctx and embedded_ctx.controller then
        return SubFixGenerateSelectionCore._create_embedded_progress_state(embedded_ctx.controller)
    end
    local progress_window = dispatcher:AddWindow({
        ID = "GenerateProgressWindow",
        WindowTitle = "SubFix · 正在生成选区字幕",
        Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({520, 380, 430, 200}),
    },
    ui:VGroup{
        Font = DIALOG_FONT,
        StyleSheet = SUBFIX_ROOT_STYLESHEET,
        Spacing = 8,
        ContentsMargins = 20,
        ui:Label{ID = "GenerateProgressStatusLabel", Text = "准备中", Weight = 0, MinimumSize = {0, 22}},
        ui:HGroup{
            Weight = 0,
            ui:HGap(0, 1),
            ui:Label{ID = "GenerateProgressBarLabel", Text = string.rep("░", GENERATE_PROGRESS_BAR_WIDTH), Font = ui:Font{PixelSize = 14, Family = "Courier New"}, Weight = 0, MinimumSize = {0, 20}},
            ui:HGap(0, 1)
        },
        ui:Label{ID = "GenerateProgressMetaLabel", Text = "进度 0%  ·  用时 0s", Weight = 0, MinimumSize = {0, 18}},
        ui:HGroup{
            Weight = 0,
            ui:HGap(0, 1),
            ui:Label{ID = "GenerateProgressHintLabel", Text = "去摸个鱼吧🐟～\n::)", Weight = 0, MinimumSize = {0, 38}, Alignment = {AlignHCenter = true, AlignVCenter = true}},
            ui:HGap(0, 1)
        },
        ui:VGap(4),
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            ui:HGap(0, 1),
            ui:Button{ID = "GenerateProgressCancelBtn", Text = "取消", Weight = 0, MinimumSize = {88, 28}},
            ui:HGap(0, 1)
        }
    })
    local progress_state = {
        window = progress_window,
        cancel_requested = false,
        cancel_allowed = true,
        started_at = os.time(),
        progress_fraction = 0,
        finished = false
    }

    function progress_window.On.GenerateProgressCancelBtn.Clicked(ev)
        if progress_state.finished then
            progress_window:Hide()
            return
        end
        if progress_state.cancel_allowed == false then return end
        progress_state.cancel_requested = true
    end

    function progress_window.On.GenerateProgressWindow.Close(ev)
        if progress_state.finished then
            progress_window:Hide()
            return
        end
        if progress_state.cancel_allowed == false then return end
        progress_state.cancel_requested = true
    end

    progress_window:Show()
    return progress_state
end

local function update_generate_progress_window(progress_state, payload, extra_log)
    local progress_window = progress_state and progress_state.window
    if not progress_window then return end
    local ok_items, items = pcall(function() return progress_window:GetItems() end)
    if not ok_items or not items then return end
    local stage = tostring(payload and payload.stage or "处理中")
    local message = tostring(payload and payload.message or "")
    local elapsed = progress_elapsed_text(progress_state.started_at)
    local bar, percent, fraction, is_indeterminate = progress_bar_text(progress_state, payload)
    local status_text = message ~= "" and message or stage
    if extra_log and extra_log ~= "" then
        status_text = tostring(extra_log)
    end
    -- P10：embedded 高频进度上报（每个音频片段/百分比 tick）下，状态行/百分比行文本
    -- 没变就不 SetText，避免无意义的 UIManager 属性提交与重绘。standalone 字符进度条
    -- （GenerateProgressBarLabel）保持每次更新，终态由 payload.force_text 强制刷新。
    local force_text = payload and payload.force_text == true
    if items.GenerateProgressStatusLabel then
        if force_text or progress_state._p10_last_status_text ~= status_text then
            items.GenerateProgressStatusLabel.Text = status_text
            progress_state._p10_last_status_text = status_text
        end
    end
    -- embedded 无条形（条形已移除）；standalone 独立弹窗仍是字符 Label。
    if items.GenerateProgressBarPainter then
        pcall(items.GenerateProgressBarPainter, fraction, is_indeterminate)
    elseif items.GenerateProgressBarLabel then
        items.GenerateProgressBarLabel.Text = bar
    end
    if items.GenerateProgressMetaLabel then
        local meta_text
        if payload and payload.indeterminate then
            meta_text = "正在进行中  ·  用时 " .. elapsed
        else
            meta_text = "进度 " .. tostring(percent) .. "%  ·  用时 " .. elapsed
            if payload and tonumber(payload.eta_seconds) and tonumber(payload.eta_seconds) > 0 then
                meta_text = meta_text .. "  ·  预计剩余 " .. progress_eta_text(payload.eta_seconds)
            end
        end
        if force_text or progress_state._p10_last_meta_text ~= meta_text then
            items.GenerateProgressMetaLabel.Text = meta_text
            progress_state._p10_last_meta_text = meta_text
        end
    end
end

local function set_generate_progress_cancel_enabled(progress_state, enabled, text)
    if not progress_state then return end
    progress_state.cancel_allowed = enabled ~= false
    local progress_window = progress_state.window
    if not progress_window then return end
    local ok_items, items = pcall(function() return progress_window:GetItems() end)
    if not ok_items or not items or not items.GenerateProgressCancelBtn then return end
    items.GenerateProgressCancelBtn.Enabled = enabled ~= false
    if text and text ~= "" then
        items.GenerateProgressCancelBtn.Text = tostring(text)
    end
end

local function pump_generate_progress_events(progress_state)
    if not dispatcher or not ui then
        return not (progress_state and progress_state.cancel_requested)
    end
    -- embedded：宿主主 RunLoop 已在运行，这里绝不能再起「timer+ExitLoop」的假嵌套循环
    -- （会立刻退出宿主主循环）。后处理均为快速 Lua 操作，直接返回取消标记即可；
    -- ASR 后台轮询自身的 200ms timer + RunLoop 仍在嵌套消息循环里正常分发。
    if EMBEDDED then
        return not (progress_state and progress_state.cancel_requested)
    end
    local uid = tostring(os.time()) .. "_" .. tostring(math.random(100000, 999999))
    local timer_id = "GenerateProgressYieldTimer_" .. uid
    local yield_timer = ui:Timer({
        ID = timer_id,
        Interval = GENERATE_UI_YIELD_INTERVAL_MS,
        SingleShot = true
    })
    local timer_registered = register_ui_timer(yield_timer, function()
        pcall(function() yield_timer:Stop() end)
        unregister_ui_timer(timer_id)
        pcall(function() dispatcher:ExitLoop() end)
    end)
    if not timer_registered then
        return not (progress_state and progress_state.cancel_requested)
    end
    local started = pcall(function() yield_timer:Start() end)
    if not started then
        unregister_ui_timer(timer_id)
        return not (progress_state and progress_state.cancel_requested)
    end
    pcall(function() dispatcher:RunLoop() end)
    pcall(function() yield_timer:Stop() end)
    unregister_ui_timer(timer_id)
    return not (progress_state and progress_state.cancel_requested)
end

local function update_generate_postprocess_stage(progress_state, stage, message, progress_index)
    update_generate_progress_window(progress_state, {
        stage = stage,
        message = message,
        progress_index = progress_index,
        progress_total = 100
    }, message)
    return pump_generate_progress_events(progress_state)
end

local function show_writeback_progress_overlay(progress_state)
    local progress_window = progress_state and progress_state.window
    if not progress_window then return false end
    local ok = pcall(function()
        progress_window:SetAttrs({Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry(GENERATE_WRITEBACK_OVERLAY_GEOMETRY)})
        progress_window:Show()
    end)
    return ok
end

local function finish_generate_progress_window(progress_state, status, message)
    local progress_window = progress_state and progress_state.window
    if not progress_window then return end
    local payload = {stage = status, message = message, force_text = true}
    if status == "完成" then
        payload.progress_index = 100
        payload.progress_total = 100
    end
    update_generate_progress_window(progress_state, payload, message)
    progress_state.finished = true
    set_generate_progress_cancel_enabled(progress_state, true, "关闭")
end

local function summarize_doubao_asr_failure_reason(reason)
    local text = trim_text(tostring(reason or "豆包 ASR 请求失败"))
    text = text:gsub("[\r\n]+", " ")
    local marker_start, marker_end = text:find("豆包 ASR 失败", 1, true)
    if marker_start then
        text = trim_text(text:sub(marker_end + 1):gsub("^[（(][^）)]*[）)]%s*[:：]?%s*", ""))
    end
    if text == "" then text = "豆包 ASR 请求失败" end
    if #text > 180 then text = text:sub(1, 177) .. "..." end
    return text
end

local function is_doubao_asr_failure(reason)
    return tostring(reason or ""):find("豆包 ASR 失败", 1, true) ~= nil
end

-- 云端请求失败不再静默回退：先给出原始原因，再由用户决定是否重试或改用本地 Qwen。
-- 该窗口运行时没有后台轮询 RunLoop，因此独立 RunLoop 不会嵌套进进度计时器。
local function show_doubao_asr_failure_action_dialog(reason)
    if not dispatcher or not ui then return "close" end
    local action = "close"
    local dialog = dispatcher:AddWindow({
        ID = "GenerateDoubaoAsrFailureWindow",
        WindowTitle = "SubFix · 云端识别失败",
        Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({520, 320, 520, 300}),
    },
    ui:VGroup{
        Font = DIALOG_FONT,
        StyleSheet = SUBFIX_ROOT_STYLESHEET,
        Spacing = 8,
        ContentsMargins = 12,
        ui:Label{Text = "豆包（云端）未完成识别：", Weight = 0},
        ui:TextEdit{
            ID = "GenerateDoubaoAsrFailureReason",
            Text = summarize_doubao_asr_failure_reason(reason),
            ReadOnly = true,
            Weight = 1,
            MinimumSize = {0, 84}
        },
        ui:VGap(2),
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            MinimumSize = {0, 44},
            ui:Button{ID = "GenerateDoubaoAsrRetryBtn", Text = "重试", Weight = 1, MinimumSize = {0, 28}},
            ui:Button{ID = "GenerateDoubaoAsrUseQwenBtn", Text = "改用本地 Qwen", Weight = 1, MinimumSize = {0, 28}},
            ui:Button{ID = "GenerateDoubaoAsrCloseBtn", Text = "关闭", Weight = 1, MinimumSize = {0, 28}}
        }
    })

    local function finish(choice)
        action = choice or "close"
        pcall(function() dialog:Hide() end)
        pcall(function() dispatcher:ExitLoop() end)
    end
    function dialog.On.GenerateDoubaoAsrRetryBtn.Clicked(ev) finish("retry") end
    function dialog.On.GenerateDoubaoAsrUseQwenBtn.Clicked(ev) finish("local_qwen") end
    function dialog.On.GenerateDoubaoAsrCloseBtn.Clicked(ev) finish("close") end
    function dialog.On.GenerateDoubaoAsrFailureWindow.Close(ev) finish("close") end

    dialog:Show()
    dispatcher:RunLoop()
    pcall(function() dialog:Hide() end)
    return action
end

local GENERATE_ENGINE_PROFILE_FILES = {
    v5 = "segmentation_profile_v4.json",
}

local function resolve_generate_engine()
    return "v5"
end

local function resolve_segmentation_profile_path(paths, engine)
    local file_name = GENERATE_ENGINE_PROFILE_FILES[engine]
    if not file_name then return nil end
    -- 同时识别 "/" 与 "\"（Windows 路径可能含反斜杠）。
    local helper_dir = tostring(paths.helper or ""):match("^(.*)[/\\][^/\\]+$")
    local candidates = {}
    if helper_dir then candidates[#candidates + 1] = helper_dir .. "/" .. file_name end
    candidates[#candidates + 1] = configured_script_root() .. "/.subfix_support/" .. file_name
    for _, candidate in ipairs(candidates) do
        if candidate and file_exists(candidate) then return candidate end
    end
    print(string.format("[SubFix Generate] 未找到 %s 引擎的断句 profile (%s)，交由 Python 默认路径处理", engine, file_name))
    return nil
end

local function build_asr_helper_batch_command(batch_plan_path, srt_path, json_path, timeline_start_frame, fps, progress_path, subtitle_mode, max_chars, backend, hotwords_json)
    local paths = resolve_asr_paths()
    if not file_exists(paths.helper) then
        return nil, "缺少 ASR helper: " .. tostring(paths.helper)
    end
    subtitle_mode = subtitle_mode == "live" and "live" or "narration"
    -- backend 由生成对话框的"识别模型"选择决定；缺省回退到 DEFAULT_ASR_BACKEND(auto/Qwen 本地)。
    local asr_backend = (type(backend) == "string" and backend ~= "") and backend or DEFAULT_ASR_BACKEND
    local qwen_status = qwen_check.inspect(paths)
    local python = paths.python
    if asr_backend == "doubao_asr" or asr_backend == "doubao_asr_v2" then
        -- 豆包负责转写，但 v4/v5 仍依赖 Qwen 做强制时间对齐。
        if qwen_status and qwen_status.ready and file_exists(qwen_status.python or "") then
            python = qwen_status.python
        else
            python = paths.runtime_python
            if not file_exists(python) then
                return nil, "SubFix 内置 Python 缺失，请重新安装完整 SubFix 测试版"
            end
        end
    elseif not (qwen_status and qwen_status.ready and file_exists(qwen_status.python or "")) then
        return nil, "本地 Qwen 尚未安装，请先双击“Qwen（本地）”完成下载安装"
    else
        python = qwen_status.python
    end
    local generate_engine = resolve_generate_engine()
    local cmd_parts = {}
    local command_args = {}
    if qwen_status and qwen_status.ready and file_exists(qwen_status.python or "") then
        if SUBFIX_IS_WINDOWS then
            -- Windows 的 cmd 没有 env：用 set 清空，避免继承父进程的 PYTHONHOME/PYTHONPATH。
            command_args = {'set "PYTHONHOME=" &', 'set "PYTHONPATH=" &'}
        else
            command_args = {"env", "-u", "PYTHONHOME", "-u", "PYTHONPATH"}
        end
    end
    local helper_args = {
        shell_quote(python),
        shell_quote(paths.helper),
        "--mode", "generate_subtitles_batch",
        "--backend", shell_quote(asr_backend),
        "--generate-engine", shell_quote(generate_engine),
        "--batch-plan-json", shell_quote(batch_plan_path),
        "--output", shell_quote(json_path),
        "--srt-output", shell_quote(srt_path),
        "--srt-base-frame", shell_quote(tostring(timeline_start_frame)),
        "--timeline-start-frame", shell_quote(tostring(timeline_start_frame)),
        "--fps", shell_quote(tostring(fps)),
        "--model", shell_quote(DEFAULT_ASR_MODEL),
        "--language", shell_quote(DEFAULT_ASR_LANGUAGE),
        "--subtitle-mode", shell_quote(subtitle_mode),
        "--diagnostic-output", shell_quote(paths.diagnostic),
        "--progress-json", shell_quote(progress_path)
    }
    for _, value in ipairs(helper_args) do command_args[#command_args + 1] = value end
    for _, value in ipairs(command_args) do cmd_parts[#cmd_parts + 1] = value end
    max_chars = tonumber(max_chars) or 25
    cmd_parts[#cmd_parts + 1] = "--max-chars"
    cmd_parts[#cmd_parts + 1] = shell_quote(tostring(max_chars))
    local profile_path = resolve_segmentation_profile_path(paths, generate_engine)
    if profile_path then
        cmd_parts[#cmd_parts + 1] = "--segmentation-profile"
        cmd_parts[#cmd_parts + 1] = shell_quote(profile_path)
    end
    if hotwords_json and file_exists(hotwords_json) then
        cmd_parts[#cmd_parts + 1] = "--hotwords-json"
        cmd_parts[#cmd_parts + 1] = shell_quote(hotwords_json)
    end
    local command_text = table.concat(cmd_parts, " ")
    if asr_backend == "auto" then
        if SUBFIX_IS_WINDOWS then
            -- cmd 里用 set 设置模型路径（整段加引号以容纳空格），再用 & 接上真正的命令。
            command_text = 'set "SUBFIX_QWEN3_ASR_MODEL=' .. tostring(qwen_status.model):gsub('"', "") .. '" & ' .. command_text
        else
            command_text = "SUBFIX_QWEN3_ASR_MODEL=" .. shell_quote(qwen_status.model) .. " " .. command_text
        end
    end
    return command_text, nil
end

local function kill_background_process(pid_file)
    local pid_text = trim_text(read_text_file(pid_file) or "")
    local pid = tonumber(pid_text)
    if pid and pid > 0 then
        if SUBFIX_IS_WINDOWS then
            -- taskkill /T 连同子进程（python 等）一起结束。
            os.execute("taskkill /F /T /PID " .. tostring(pid) .. " >nul 2>nul")
        else
            os.execute("kill -TERM -- -" .. tostring(pid) .. " 2>/dev/null || kill -TERM " .. tostring(pid) .. " 2>/dev/null || true")
            os.execute("sleep 0.2; kill -KILL -- -" .. tostring(pid) .. " 2>/dev/null || true")
        end
        return true
    end
    return false
end

-- 当前在跑的后台任务 PID 文件（ASR/文稿对齐/Qwen 安装）。正常/取消/失败收尾都会清空；
-- 宿主在任务运行中关闭窗口时，shutdown_embedded 据此回收进程树，杜绝孤儿 python/ffmpeg。
local active_background_pid_file = nil

-- 跨平台删除临时文件（失败静默忽略）。Windows 下 ANSI os.remove 删不掉含非当前
-- 代码页字符的路径时，用 DeleteFileW 宽字符兜底。
local function remove_temp_files(...)
    for _, path in ipairs({ ... }) do
        local target = tostring(path or "")
        if target ~= "" then
            pcall(function() os.remove(target) end)
            if SUBFIX_IS_WINDOWS and subfix_winio.remove and file_exists(target) then
                subfix_winio.remove(target)
            end
        end
    end
end

-- Windows 专用后台启动：先落盘一个 .cmd（真正干活的批处理），再落盘一个 .ps1 文件，
-- 用 start "" /b 以无窗口方式异步启动 PowerShell 执行该 .ps1，由 .ps1 记录子进程 PID。
-- 关键：必须改用独立 .ps1 文件 + -File，绝不能把 PowerShell 命令内联进 -Command。
-- 原因：os.execute 最终走 cmd /c，而 cmd 对“以引号开头且含多个引号组”的命令行会剥离首尾引号，
-- 导致内联 -Command "...嵌套引号..." 被劈坏（报 “‘...powershell.exe” ...’ 不是内部或外部命令”，
-- 进而进度窗永不出现、报错信息乱码）。-File 形式只有两组引号（程序路径、脚本路径），无嵌套，稳。
local function launch_background_windows(inner_cmd, stdout_file, pid_file, done_file, exit_file)
    local runner_path = stdout_file .. ".cmd"
    local ps1_path = stdout_file .. ".ps1"
    -- 宽字符写出包装脚本（TEMP 含中文用户名也能落盘）；.cmd 不带 BOM（cmd 首行遇 BOM 报错）。
    local runner_content = table.concat({
        "@echo off\r\n",
        "chcp 65001 >nul\r\n",
        -- 清掉宿主（Resolve/Fusion）可能注入的 Python 环境；统一按 UTF-8 输出，便于读取报错。
        'set "PYTHONHOME="\r\n',
        'set "PYTHONPATH="\r\n',
        'set "PYTHONIOENCODING=utf-8"\r\n',
        tostring(inner_cmd) .. " > " .. subfix_dq(stdout_file) .. " 2>&1\r\n",
        "echo %ERRORLEVEL% > " .. subfix_dq(exit_file) .. "\r\n",
        "echo done > " .. subfix_dq(done_file) .. "\r\n",
    })
    -- 独立的 .ps1：启动 cmd /c runner 并以隐藏窗口运行，把子进程（cmd.exe）PID 写到 pid_file。
    -- 路径用单引号包裹；pid 取 Start-Process -PassThru 返回的 .Id（便于 taskkill /T 取消）。
    -- 显式写 UTF-8 BOM，避免 Windows PowerShell 5.1 对无 BOM 脚本按 ANSI 读坏中文路径。
    local ps1_content = "\239\187\191"
        .. "$p = Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c','" .. runner_path
        .. "') -PassThru -WindowStyle Hidden\r\n"
        .. "$p.Id | Out-File -FilePath '" .. pid_file .. "' -Encoding ascii\r\n"
    if not (write_binary_file(runner_path, runner_content) and write_binary_file(ps1_path, ps1_content)) then
        return false
    end

    local powershell = tostring(os.getenv("SystemRoot") or "C:\\Windows")
        .. "\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
    local ps1_arguments = "-NoProfile -ExecutionPolicy Bypass -File " .. subfix_dq(ps1_path)
    -- 以「PID 文件是否落盘」作为启动成功的唯一判据，不能只看 ShellExecuteW 的同步返回值。
    -- 实测在达芬奇内嵌 LuaJIT 环境下，ShellExecuteW 已成功拉起 powershell（约 0.5s 后 PID
    -- 文件正常落盘、python 正常运行），返回值却 <=32；旧逻辑据此误判失败并立刻用
    -- os.execute 再启动一次，同一个 runner 被并发执行两遍：后一个 cmd 抢不到 stdout 的
    -- 重定向句柄，内部命令根本没执行，瞬间以 errorlevel=0 写 done，前台 1 秒内误判“helper
    -- 未生成 JSON”，而真正的 python 还要跑一分钟。只有 3 秒内始终等不到 PID 才走回退。
    local function launch_evidence_seen()
        local pid_text = trim_text(read_text_file(pid_file) or "")
        return tonumber(pid_text) ~= nil
    end
    local function wait_launch_evidence(timeout_seconds)
        local deadline = (os.clock() or 0) + timeout_seconds
        repeat
            if launch_evidence_seen() then return true end
            if subfix_winio.sleep then
                subfix_winio.sleep(100)
            else
                local until_clock = (os.clock() or 0) + 0.1
                while (os.clock() or 0) < until_clock do end
            end
        until (os.clock() or 0) >= deadline
        return launch_evidence_seen()
    end
    pcall(function()
        local ok_jit, jit_mod = pcall(require, "jit")
        if ok_jit and jit_mod then pcall(function() jit_mod.off(wait_launch_evidence) end) end
    end)
    if subfix_winio.hidden_shell then
        -- 返回值不可信也立即发一次启动；是否真的起来由 PID 证据判定。
        pcall(function() subfix_winio.hidden_shell(powershell, ps1_arguments) end)
        if wait_launch_evidence(3) then
            return true
        end
        -- 宽字符链路 3 秒内没有任何进程迹象（如返回 2=找不到文件）：回退 ANSI start。
        print("[SubFix Generate] ShellExecuteW 未观察到启动器进程（3s 无 PID），回退 start 启动")
    end
    os.execute('start "" /b ' .. subfix_dq(powershell) .. " " .. ps1_arguments)
    -- 回退路径不再以 PID 证据卡返回值：冷启动 PowerShell 在慢盘机器上可能超过 3 秒，
    -- 真没起来时由等待循环的 30 秒看门狗兜底。
    return true
end

local function progress_payload_signature(payload)
    return table.concat({
        tostring(payload and payload.stage or ""),
        tostring(payload and payload.message or ""),
        tostring(payload and payload.batch_index or ""),
        tostring(payload and payload.total_batches or ""),
        tostring(payload and payload.progress_index or ""),
        tostring(payload and payload.progress_total or "")
    }, "\n")
end

local function run_background_command_with_progress(cmd, progress_path, progress_state)
    local uid = tostring(os.time()) .. "_" .. tostring(math.random(100000, 999999))
    local root = temp_dir()
    local stdout_file = root .. "/asr_stdout_" .. uid .. ".log"
    local pid_file = root .. "/asr_pid_" .. uid
    local done_file = root .. "/asr_done_" .. uid
    local exit_file = root .. "/asr_exit_" .. uid
    local output = ""
    local cancelled = false
    local paths = resolve_asr_paths()
    if not file_exists(paths.runtime_python) then
        return false, "未找到 SubFix 内置 Python", nil
    end
    if SUBFIX_IS_WINDOWS then
        -- Windows：直接运行原命令（不需要进程组包装），由批处理写入退出码与完成标记。
        -- launch_background_windows 失败（如 TEMP 不可写）时立即返回，避免无进程可轮询
        -- 却白等 30 秒启动兜底。
        local launched = launch_background_windows(cmd, stdout_file, pid_file, done_file, exit_file)
        if not launched then
            return false, "后台任务启动失败：无法写入启动脚本（请检查临时目录权限）", nil
        end
        active_background_pid_file = pid_file
    else
        local grouped_cmd
        if file_exists(paths.process_group) then
            grouped_cmd = table.concat({
                shell_quote(paths.runtime_python),
                shell_quote(paths.process_group),
                shell_quote(cmd),
            }, " ")
        else
            -- v3.2.0 cannot add new paths, so incremental upgrades need an inline equivalent.
            local inline_group_code = 'import os,sys; os.setsid(); os.execl("/bin/sh", "sh", "-c", sys.argv[1])'
            grouped_cmd = table.concat({
                shell_quote(paths.runtime_python),
                "-c",
                shell_quote(inline_group_code),
                shell_quote(cmd),
            }, " ")
        end

        -- Detach the waiting shell too: inherited host pipes can keep launch blocked
        -- until the worker exits, preventing the progress event loop from starting.
        local bg_cmd = string.format(
            "(%s > %s 2>&1 & worker_pid=$!; echo $worker_pid > %s; wait $worker_pid; echo $? > %s; touch %s) </dev/null >/dev/null 2>&1 &",
            grouped_cmd,
            shell_quote(stdout_file),
            shell_quote(pid_file),
            shell_quote(exit_file),
            shell_quote(done_file)
        )
        os.execute(bg_cmd)
        active_background_pid_file = pid_file
    end

    local timer_id = "GenerateProgressPollTimer_" .. uid
    local poll_timer = ui:Timer({ID = timer_id, Interval = 200, SingleShot = false})
    local last_signature = nil
    local last_progress_changed_at = os.time()
    local stall_warning_logged = false
    local launch_failed = false
    local launch_ticks = 0
    -- 用户在启动器尚未写入 PID 文件的瞬间点取消时，短轮询等待 PID 出现后补 kill，
    -- 最多约 3 秒（15 × 200ms）；超时仍无 PID 才放弃，避免留下孤儿 python/ffmpeg。
    local cancel_retry_ticks = 0
    local CANCEL_KILL_RETRY_MAX_TICKS = 15

    local function finish_progress_loop()
        pcall(function() poll_timer:Stop() end)
        unregister_ui_timer(timer_id)
        pcall(function() dispatcher:ExitLoop() end)
    end

    local timer_registered = register_ui_timer(poll_timer, function()
        local payload = parse_progress_payload(read_text_file(progress_path) or "")
        local signature = progress_payload_signature(payload)
        if signature ~= last_signature then
            last_signature = signature
            last_progress_changed_at = os.time()
            stall_warning_logged = false
        end

        local display_message = tostring(payload and payload.message or "正在识别音频...")
        local stalled_seconds = math.max(0, os.time() - last_progress_changed_at)
        if stalled_seconds >= GENERATE_PROGRESS_STALL_WARNING_SECONDS then
            display_message = display_message .. " · 长时间无新进度，可取消后重试"
            if not stall_warning_logged then
                print(string.format(
                    "[SubFix Generate] ASR 已 %ds 无新进度；任务仍在运行，可由用户安全取消",
                    stalled_seconds
                ))
                stall_warning_logged = true
            end
        end
        update_generate_progress_window(
            progress_state,
            payload or {stage = "处理中", message = "正在识别音频..."},
            display_message
        )

        if progress_state and progress_state.cancel_requested then
            cancelled = true
            -- 任务已自行结束（done 已落盘）则无需再 kill。
            if file_exists(done_file) then
                finish_progress_loop()
                return
            end
            if kill_background_process(pid_file) then
                finish_progress_loop()
                return
            end
            -- PID 文件还没出现：保持轮询，等它出现立即补 kill。
            cancel_retry_ticks = cancel_retry_ticks + 1
            if cancel_retry_ticks <= CANCEL_KILL_RETRY_MAX_TICKS then
                return
            end
            print("[SubFix Generate] 取消时等待 PID 文件超时，无法确认子进程已终止: " .. tostring(pid_file))
            finish_progress_loop()
            return
        end

        -- 兜底：30 秒内既没有 PID、也没有输出/进度文件，判定启动器失败，避免永久卡在进度窗。
        launch_ticks = launch_ticks + 1
        if launch_ticks == 150 and not launch_failed
            and not file_exists(pid_file) and not file_exists(done_file)
            and not file_exists(stdout_file) and not file_exists(progress_path) then
            launch_failed = true
            finish_progress_loop()
            return
        end

        if file_exists(done_file) then
            finish_progress_loop()
        end
    end)
    if not timer_registered then
        kill_background_process(pid_file)
        active_background_pid_file = nil
        return false, "无法启动进度轮询", nil
    end

    update_generate_progress_window(progress_state, {stage = "启动 ASR", message = "正在启动字幕识别..."}, "启动 ASR helper")
    local timer_ok = pcall(function() poll_timer:Start() end)
    if not timer_ok then
        unregister_ui_timer(timer_id)
        kill_background_process(pid_file)
        active_background_pid_file = nil
        return false, "无法启动进度窗口计时器", nil
    end
    dispatcher:RunLoop()
    pcall(function() poll_timer:Stop() end)
    unregister_ui_timer(timer_id)

    output = read_text_file(stdout_file) or ""
    local exit_code = tonumber(trim_text(read_text_file(exit_file) or "")) or 1

    if launch_failed then
        remove_temp_files(stdout_file, pid_file, done_file, exit_file, stdout_file .. ".cmd", stdout_file .. ".ps1")
        active_background_pid_file = nil
        return false, "后台任务未能启动（启动器失败）。\n命令: " .. tostring(cmd), nil
    end
    if exit_code ~= 0 then
        -- 保留一份原始输出便于排查（批处理已 chcp 65001，正常情况下可直接阅读）。
        -- 用二进制宽字符读写，stdout/日志路径含中文也能保留。
        local keep_path = root .. "/last_failure_stdout.log"
        local data = read_binary_file(stdout_file)
        if data then
            write_binary_file(keep_path, data)
        end
        print(string.format(
            "[SubFix Generate] 后台命令失败（退出码 %d）；原始输出已保留到 %s：\n%s",
            exit_code, keep_path, tostring(output)
        ))
    end
    -- 正常/失败收尾都要清掉 .cmd 与 .ps1（旧版漏删 .ps1，长期使用会在 TEMP 堆积）。
    remove_temp_files(stdout_file, pid_file, done_file, exit_file, stdout_file .. ".cmd", stdout_file .. ".ps1")
    active_background_pid_file = nil

    if cancelled then
        return false, "已取消", "cancelled"
    end
    return exit_code == 0, output, nil
end

local function selected_sources_effective_audio_seconds(sources, fps)
    local total_seconds = 0
    local effective_fps = math.max(1, tonumber(fps) or 24)
    for _, source in ipairs(sources or {}) do
        local source_start = tonumber(source and source.source_start_seconds)
        local source_end = tonumber(source and source.source_end_seconds)
        local duration = source_start and source_end and (source_end - source_start) or 0
        if duration <= 0 then
            duration = (
                (tonumber(source and source.end_frame) or 0) -
                (tonumber(source and source.start_frame) or 0)
            ) / effective_fps
        end
        total_seconds = total_seconds + math.max(0, duration)
    end
    return total_seconds
end

local function format_generate_audio_duration(seconds)
    local total = math.max(0, math.floor((tonumber(seconds) or 0) + 0.5))
    local hours = math.floor(total / 3600)
    local minutes = math.floor((total % 3600) / 60)
    local remaining_seconds = total % 60
    if hours > 0 then
        return string.format("%dh%02dm%02ds", hours, minutes, remaining_seconds)
    end
    if minutes > 0 then
        return string.format("%dm%02ds", minutes, remaining_seconds)
    end
    return string.format("%ds", remaining_seconds)
end

local function run_asr_helper_batch_with_progress(batch_plan_path, srt_path, json_path, timeline_start_frame, fps, progress_state, source_count, subtitle_mode, max_chars, backend, hotwords_json, effective_audio_seconds)
    local progress_path = json_path .. ".progress.json"
    local cmd, cmd_err = build_asr_helper_batch_command(batch_plan_path, srt_path, json_path, timeline_start_frame, fps, progress_path, subtitle_mode, max_chars, backend, hotwords_json)
    if not cmd then return false, cmd_err end
    local duration_text = format_generate_audio_duration(effective_audio_seconds)
    update_generate_progress_window(
        progress_state,
        {stage = "启动批量 ASR", message = string.format("准备识别 %d 段音频 · 有效音频 %s", tonumber(source_count) or 0, duration_text)},
        string.format("批量识别 %d 段音频 · 有效音频 %s", tonumber(source_count) or 0, duration_text)
    )
    local ok, output, status = run_background_command_with_progress(cmd, progress_path, progress_state)
    remove_temp_files(progress_path)
    if not ok then
        return false, tostring(output or "ASR helper 执行失败"), status
    end
    if not file_exists(json_path) then
        return false, "ASR helper 未生成 JSON"
    end
    return true
end

local function write_script_match_batch_plan(path, audio_sources, rows, fps, timeline_start_frame, language)
    local lines = {"{", '  "fps": ' .. tostring(fps) .. ','}
    lines[#lines + 1] = '  "timeline_start_frame": ' .. tostring(timeline_start_frame) .. ','
    lines[#lines + 1] = '  "language": "' .. json_escape(language or "zh") .. '",'
    lines[#lines + 1] = '  "audio_sources": ['
    for i, src in ipairs(audio_sources) do
        local ch = tonumber(src.audio_channel_index)
        lines[#lines + 1] = "    {"
        lines[#lines + 1] = string.format('      "audio": "%s",', json_escape(tostring(src.file_path or "")))
        lines[#lines + 1] = string.format('      "source_start": %.6f,', tonumber(src.source_start_seconds) or 0)
        lines[#lines + 1] = string.format('      "source_end": %.6f,', tonumber(src.source_end_seconds) or 0)
        lines[#lines + 1] = string.format('      "timeline_start_frame": %d,', tonumber(src.start_frame) or 0)
        lines[#lines + 1] = string.format('      "fps": %.6f,', tonumber(src.fps) or fps)
        lines[#lines + 1] = string.format('      "audio_channel_index": %s', (ch and ch > 0) and tostring(math.floor(ch)) or "null")
        lines[#lines + 1] = i < #audio_sources and "    }," or "    }"
    end
    lines[#lines + 1] = "  ],"
    lines[#lines + 1] = '  "rows": ['
    for i, row in ipairs(rows) do
        lines[#lines + 1] = '    {"text": "' .. json_escape(tostring(row.text or "")) .. '"}' .. (i < #rows and "," or "")
    end
    lines[#lines + 1] = "  ]"
    lines[#lines + 1] = "}"
    return write_text_file(path, table.concat(lines, "\n") .. "\n")
end

-- ============ 文稿匹配：语义/标点优先断句 ============
-- 通用：断句发生在语义边界（从句/短语结束），绝不在单词或词组中间断开；
-- 中文：顿号转空格、保留《》·、逗号分行、句末 。！？ 删除/中段视为分行；
-- 英文：标点之后或连词/介词之前断行，… 表示停顿未完、-- 表示被打断。

local function utf8_next(s, i)
    i = i or 1
    if i > #s then return nil end
    local b = s:byte(i)
    local len = 1
    if b >= 0xF0 then len = 4
    elseif b >= 0xE0 then len = 3
    elseif b >= 0xC0 then len = 2 end
    local e = i + len - 1
    return i, e, s:sub(i, e)
end

local function utf8_len(s)
    local n, i = 0, 1
    while i <= #s do
        local _, e = utf8_next(s, i)
        i = e + 1
        n = n + 1
    end
    return n
end

local function char_to_byte_pos(s, char_count)
    local n, i = 0, 1
    while i <= #s and n < char_count do
        local _, e = utf8_next(s, i)
        i = e + 1
        n = n + 1
    end
    return i - 1
end

local function detect_segment_language(block)
    block = tostring(block or "")
    local latin, cjk = 0, 0
    local i = 1
    while i <= #block do
        local _, eb, c = utf8_next(block, i)
        if c:match("[A-Za-z]") then
            latin = latin + 1
        else
            local b = c:byte(1)
            if b and b >= 0xE3 then cjk = cjk + 1 end
        end
        i = eb + 1
    end
    if cjk == 0 then return "en" end
    if latin == 0 then return "zh" end
    return (cjk >= latin) and "zh" or "en"
end

-- 注意：Lua 的字符类 [] 是“按字节集合”匹配，把多字节中文标点写进 [] 会误伤字节集相同的汉字
-- （如“冀”=e5 86 80 含 0x80、“聚”=e8 81 9a 含 0x9a、“国”=e5 9b bd 含 0x9b），导致汉字被当标点切掉。
-- 因此这里统一改为“整字符精确比对”。
local ZH_SPLIT_CHARS = {
    ["，"] = true, ["。"] = true, ["！"] = true, ["？"] = true, ["；"] = true, ["："] = true,
    [","] = true, [";"] = true, [":"] = true, ["!"] = true, ["?"] = true, ["."] = true,
}
local EN_SPLIT_CHARS = {
    ["，"] = true, ["。"] = true, ["！"] = true, ["？"] = true, ["；"] = true, ["："] = true,
    [","] = true, [";"] = true, [":"] = true, ["!"] = true, ["?"] = true,
}
local function is_space_char(c)
    return c ~= nil and #c == 1 and c:match("^%s$") ~= nil
end
-- 逐字符折叠连续破折号（ASCII/全角/中文破折号）为 "--"，避免字节类 gsub 误伤含相同字节的汉字。
local function collapse_dash_runs(text)
    local out, i, n = {}, 1, #text
    while i <= n do
        local _, eb, c = utf8_next(text, i)
        if c == "-" or c == "–" or c == "—" then
            local j, count = eb + 1, 1
            while j <= n do
                local _, eb2, c2 = utf8_next(text, j)
                if c2 == "-" or c2 == "–" or c2 == "—" then
                    count = count + 1
                    j = eb2 + 1
                else
                    break
                end
            end
            out[#out + 1] = (count >= 2) and "--" or c
            i = j
        else
            out[#out + 1] = c
            i = eb + 1
        end
    end
    return table.concat(out)
end

local function normalize_script_segment(text, lang)
    text = tostring(text or "")
    text = text:gsub("%.%.%.", "…")
    text = collapse_dash_runs(text)
    if lang ~= "en" then
        text = text:gsub("、", " ")
    end
    return text
end

local function segment_chinese(block)
    local text = normalize_script_segment(block, "zh")
    local parts, start_byte = {}, 1
    local i = 1
    while i <= #text do
        local sb, eb, c = utf8_next(text, i)
        if ZH_SPLIT_CHARS[c] then
            local piece = trim_text(text:sub(start_byte, sb - 1))
            if piece ~= "" then parts[#parts + 1] = piece end
            i = eb + 1
            while i <= #text do
                local _, eb2, c2 = utf8_next(text, i)
                if c2:match("[%p%s]") then i = eb2 + 1 else break end
            end
            start_byte = i
        else
            i = eb + 1
        end
    end
    local last = trim_text(text:sub(start_byte))
    if last ~= "" then parts[#parts + 1] = last end
    return parts
end

local function segment_english(block)
    local text = normalize_script_segment(block, "en")
    local parts, start_byte = {}, 1
    local i = 1
    while i <= #text do
        local sb, eb, c = utf8_next(text, i)
        if EN_SPLIT_CHARS[c] then
            local piece = trim_text(text:sub(start_byte, sb - 1))
            if piece ~= "" then parts[#parts + 1] = piece end
            i = eb + 1
            while i <= #text do
                local _, eb2, c2 = utf8_next(text, i)
                if c2:match("[%p%s]") then i = eb2 + 1 else break end
            end
            start_byte = i
        elseif c == "." then
            local _, _, nxt = utf8_next(text, eb + 1)
            if (not nxt) or nxt:match("%s") then
                local piece = trim_text(text:sub(start_byte, sb - 1))
                if piece ~= "" then parts[#parts + 1] = piece end
                i = eb + 1
                while i <= #text do
                    local _, eb3, c3 = utf8_next(text, i)
                    if c3:match("[%s]") then i = eb3 + 1 else break end
                end
                start_byte = i
            else
                i = eb + 1
            end
        else
            i = eb + 1
        end
    end
    local last = trim_text(text:sub(start_byte))
    if last ~= "" then parts[#parts + 1] = last end
    return parts
end

local function find_best_break_point(s, lang, max_chars)
    local max_pos = char_to_byte_pos(s, max_chars)
    local last_candidate = 0
    local i = 1
    while i <= max_pos and i <= #s do
        local sb, eb, c = utf8_next(s, i)
        local is_break
        if lang == "en" then
            is_break = (EN_SPLIT_CHARS[c] == true) or is_space_char(c)
        else
            is_break = (ZH_SPLIT_CHARS[c] == true) or is_space_char(c)
        end
        if is_break then last_candidate = eb end
        i = eb + 1
    end
    return last_candidate
end

local function force_break_point(s, lang, max_chars)
    local max_pos = char_to_byte_pos(s, max_chars)
    if lang == "en" then
        local i = max_pos + 1
        while i <= #s do
            local _, eb, c = utf8_next(s, i)
            if c:match("%s") then return eb end
            i = eb + 1
        end
    end
    return max_pos
end

local function soft_break_by_length(seg, lang, max_chars)
    max_chars = tonumber(max_chars) or 0
    if max_chars <= 0 then return {seg} end
    if utf8_len(seg) <= max_chars then return {seg} end
    local pieces = {}
    local rest = seg
    while utf8_len(rest) > max_chars do
        local prev_len = utf8_len(rest)
        local cut = find_best_break_point(rest, lang, max_chars)
        if cut <= 0 then cut = force_break_point(rest, lang, max_chars) end
        if cut <= 0 then cut = char_to_byte_pos(rest, max_chars) end
        local a = trim_text(rest:sub(1, cut))
        rest = trim_text(rest:sub(cut + 1))
        if a ~= "" then pieces[#pieces + 1] = a end
        if utf8_len(rest) >= prev_len then break end
    end
    local final_rest = trim_text(rest)
    if final_rest ~= "" then
        if #pieces > 0 and utf8_len(final_rest) <= 2 then
            pieces[#pieces] = pieces[#pieces] .. final_rest
        else
            pieces[#pieces + 1] = final_rest
        end
    end
    return pieces
end

local function split_script_into_subtitle_rows(script_text, max_chars)
    max_chars = tonumber(max_chars) or 28
    local rows = {}
    for raw in (tostring(script_text or "") .. "\n"):gmatch("(.-)\n") do
        local block = trim_text(raw)
        if block ~= "" then
            local lang = detect_segment_language(block)
            local segs = (lang == "en") and segment_english(block) or segment_chinese(block)
            for _, seg in ipairs(segs) do
                for _, piece in ipairs(soft_break_by_length(seg, lang, max_chars)) do
                    local t = trim_text(piece)
                    if t ~= "" then rows[#rows + 1] = {text = t} end
                end
            end
        end
    end
    return rows
end

-- 文稿匹配：用本地 Qwen 环境里的 transformers 版 Qwen3ForcedAligner 对齐（复用 Qwen python）。
local function build_script_match_helper_command(batch_plan_path, json_path, progress_path, timeline_start_frame, fps, language)
    local paths = resolve_asr_paths()
    if not file_exists(paths.helper) then return nil, "缺少 ASR helper: " .. tostring(paths.helper) end
    local qwen_status = qwen_check.inspect(paths)
    if not (qwen_status and qwen_status.ready and file_exists(qwen_status.python or "")) then
        return nil, "本地 Qwen 尚未安装，请先双击“Qwen（本地）”完成下载安装"
    end
    local python = qwen_status.python
    local command_args = {}
    if SUBFIX_IS_WINDOWS then
        command_args = {'set "PYTHONHOME=" &', 'set "PYTHONPATH=" &'}
    else
        command_args = {"env", "-u", "PYTHONHOME", "-u", "PYTHONPATH"}
    end
    local helper_args = {
        shell_quote(python),
        shell_quote(paths.helper),
        "--mode", "script_match_align",
        "--batch-plan-json", shell_quote(batch_plan_path),
        "--output", shell_quote(json_path),
        "--timeline-start-frame", shell_quote(tostring(timeline_start_frame)),
        "--fps", shell_quote(tostring(fps)),
        "--language", shell_quote(language or DEFAULT_ASR_LANGUAGE),
        "--progress-json", shell_quote(progress_path)
    }
    local cmd_parts = {}
    for _, value in ipairs(command_args) do cmd_parts[#cmd_parts + 1] = value end
    for _, value in ipairs(helper_args) do cmd_parts[#cmd_parts + 1] = value end
    return table.concat(cmd_parts, " "), nil
end

local function run_script_match_helper_with_progress(batch_plan_path, json_path, timeline_start_frame, fps, language, progress_state)
    local progress_path = json_path .. ".progress.json"
    local cmd, cmd_err = build_script_match_helper_command(batch_plan_path, json_path, progress_path, timeline_start_frame, fps, language)
    if not cmd then return false, cmd_err end
    update_generate_progress_window(progress_state, {stage = "启动文稿对齐", message = "正在用 Qwen3 强制对齐文稿与音频"}, "正在用 Qwen3 强制对齐文稿与音频")
    local ok, output, status = run_background_command_with_progress(cmd, progress_path, progress_state)
    remove_temp_files(progress_path)
    if not ok then return false, tostring(output or "文稿对齐 helper 执行失败"), status end
    if not file_exists(json_path) then return false, "文稿对齐 helper 未生成 JSON" end
    return true
end

local function show_qwen_install_complete_dialog()
    if not dispatcher or not ui then return end
    local complete_window = dispatcher:AddWindow({
        ID = "GenerateQwenInstallCompleteWindow",
        WindowTitle = "SubFix · 本地 Qwen 安装完成",
        Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({560, 340, 420, 110}),
    },
    ui:VGroup{
        Font = DIALOG_FONT,
        StyleSheet = SUBFIX_ROOT_STYLESHEET,
        Spacing = 8,
        ContentsMargins = 14,
        ui:Label{Text = "本地 Qwen 已安装完成。", Weight = 0},
        ui:Label{Text = "Qwen3-ASR-1.7B 已就绪，可直接开始生成字幕。", Weight = 0},
        ui:HGroup{
            Weight = 0,
            MinimumSize = {0, 34},
            ui:Button{ID = "GenerateQwenInstallCompleteBtn", Text = "继续", Weight = 1, MinimumSize = {0, 28}, StyleSheet = SUBFIX_BTN_ACCENT}
        }
    })
    local function close_complete_window()
        pcall(function() complete_window:Hide() end)
    end
    function complete_window.On.GenerateQwenInstallCompleteBtn.Clicked(ev) close_complete_window() end
    function complete_window.On.GenerateQwenInstallCompleteWindow.Close(ev) close_complete_window() end
    complete_window:Show()
end

local function install_local_qwen_with_progress()
    local paths = resolve_asr_paths()
    local existing_status = qwen_check.inspect(paths)
    if existing_status.ready then
        show_qwen_install_complete_dialog()
        return true
    end
    local uid = tostring(os.time()) .. "_" .. tostring(math.random(100000, 999999))
    local root = temp_dir()
    local output_path = root .. "/subfix_qwen_install_" .. uid .. ".json"
    local progress_path = output_path .. ".progress.json"
    local cmd, cmd_err = build_qwen_install_command(paths, output_path, progress_path)
    if not cmd then return false, cmd_err end
    local progress_state, progress_err = show_generate_progress_window()
    if not progress_state then return false, progress_err end
    update_generate_progress_window(progress_state, {stage = "准备下载", message = "正在准备本地 Qwen 安装", indeterminate = true})
    local ok, output, status = run_background_command_with_progress(cmd, progress_path, progress_state)
    remove_temp_files(progress_path)
    local payload = decode_json_text(read_text_file(output_path) or "")
    remove_temp_files(output_path)
    if not ok then
        local message = type(payload) == "table" and tostring(payload.error or "") or ""
        if message == "" then message = tostring(output or "本地 Qwen 安装失败") end
        finish_generate_progress_window(progress_state, status == "cancelled" and "已取消" or "失败", message)
        pcall(function() progress_state.window:Hide() end)
        -- 第三值透传底层状态（"cancelled" 等），调用方据此区分“用户取消”与真失败，
        -- 取消时不再弹失败对话框。
        return false, message, status
    end
    -- P5：安装刚结束，强制绕过缓存复检，并把最新 ready 状态回填缓存
    local qwen_status = qwen_check.inspect(paths, true)
    if not qwen_status.ready then
        local message = "本地 Qwen 安装后校验未通过"
        finish_generate_progress_window(progress_state, "失败", message)
        pcall(function() progress_state.window:Hide() end)
        return false, message
    end
    finish_generate_progress_window(progress_state, "完成", "本地 Qwen 已安装")
    pcall(function() progress_state.window:Hide() end)
    show_qwen_install_complete_dialog()
    return true
end

local function show_qwen_install_failed_dialog(message)
    if not dispatcher or not ui then return false end
    local retry = false
    local failed_window = dispatcher:AddWindow({
        ID = "GenerateQwenInstallFailedWindow",
        WindowTitle = "SubFix · 本地 Qwen 安装失败",
        Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({560, 340, 560, 240}),
    },
    ui:VGroup{
        Font = DIALOG_FONT,
        StyleSheet = SUBFIX_ROOT_STYLESHEET,
        Spacing = 8,
        ContentsMargins = 14,
        ui:Label{Text = "本地 Qwen 尚未安装完成：", Weight = 0},
        ui:TextEdit{ID = "GenerateQwenInstallErrorText", PlainText = tostring(message or "未知错误"), ReadOnly = true, Weight = 1, MinimumSize = {0, 80}},
        ui:HGroup{
            Weight = 0,
            Spacing = 8,
            MinimumSize = {0, 34},
            ui:Button{ID = "GenerateQwenInstallRetryBtn", Text = "重试", Weight = 1, MinimumSize = {0, 28}},
            ui:Button{ID = "GenerateQwenInstallFailedCancelBtn", Text = "取消", Weight = 1, MinimumSize = {0, 28}}
        }
    })
    local function close_failed_window(value)
        retry = value == true
        pcall(function() failed_window:Hide() end)
        pcall(function() dispatcher:ExitLoop() end)
    end
    function failed_window.On.GenerateQwenInstallRetryBtn.Clicked(ev) close_failed_window(true) end
    function failed_window.On.GenerateQwenInstallFailedCancelBtn.Clicked(ev) close_failed_window(false) end
    function failed_window.On.GenerateQwenInstallFailedWindow.Close(ev) close_failed_window(false) end
    failed_window:Show()
    dispatcher:RunLoop()
    pcall(function() failed_window:Hide() end)
    return retry
end

local function merge_generated_rows_for_writeback(row_groups, scope)
    local all_rows = {}
    local scope_start = tonumber(scope and scope.start_frame)
    local scope_end = tonumber(scope and scope.end_frame)
    for _, rows in ipairs(row_groups or {}) do
        for _, row in ipairs(rows or {}) do
            local start_frame = tonumber(row.start_frame)
            local end_frame = tonumber(row.end_frame)
            local text = trim_text(tostring(row.text or ""))
            if start_frame and end_frame and text ~= "" then
                if scope_start then start_frame = math.max(start_frame, scope_start) end
                if scope_end then end_frame = math.min(end_frame, scope_end) end
                if end_frame > start_frame then
                    all_rows[#all_rows + 1] = {
                        start_frame = start_frame,
                        end_frame = end_frame,
                        text = text
                    }
                end
            end
        end
    end
    table.sort(all_rows, function(a, b)
        local a_start = tonumber(a.start_frame) or 0
        local b_start = tonumber(b.start_frame) or 0
        if a_start ~= b_start then return a_start < b_start end
        return (tonumber(a.end_frame) or 0) < (tonumber(b.end_frame) or 0)
    end)

    local merged_rows = {}
    local last_end = nil
    for _, row in ipairs(all_rows) do
        local start_frame = tonumber(row.start_frame) or 0
        local end_frame = tonumber(row.end_frame) or 0
        if last_end and start_frame < last_end then
            start_frame = last_end
        end
        if end_frame > start_frame then
            merged_rows[#merged_rows + 1] = {
                index = #merged_rows + 1,
                start_frame = start_frame,
                end_frame = end_frame,
                text = row.text
            }
            last_end = end_frame
        end
    end
    if #merged_rows == 0 then
        return nil, "生成结果没有可写回字幕"
    end
    return merged_rows
end

local function build_composite_rows_for_writeback(existing_rows, scope, generated_rows)
    local rows = {}
    for _, existing_row in ipairs(existing_rows or {}) do
        local start_frame = tonumber(existing_row.start_frame) or 0
        local end_frame = tonumber(existing_row.end_frame) or 0
        if not range_intersects_selection(start_frame, end_frame, scope) then
            rows[#rows + 1] = {
                start_frame = start_frame,
                end_frame = end_frame,
                text = tostring(existing_row.text or "")
            }
        end
    end
    for _, row in ipairs(generated_rows or {}) do
        rows[#rows + 1] = {
            start_frame = tonumber(row.start_frame) or 0,
            end_frame = tonumber(row.end_frame) or 0,
            text = tostring(row.text or "")
        }
    end
    table.sort(rows, function(a, b)
        return (tonumber(a.start_frame) or 0) < (tonumber(b.start_frame) or 0)
    end)
    for row_index, row in ipairs(rows) do
        row.index = row_index
        if (tonumber(row.end_frame) or 0) <= (tonumber(row.start_frame) or 0) then
            row.end_frame = (tonumber(row.start_frame) or 0) + 1
        end
    end
    return rows
end

local function write_rows_to_rebuild_srt(srt_path, rows, fps, base_frame)
    local rebuild_parts = {}
    for index, row in ipairs(rows or {}) do
        rebuild_parts[#rebuild_parts + 1] = tostring(index) .. "\n"
        rebuild_parts[#rebuild_parts + 1] = frames_to_srt_time(row.start_frame, fps, base_frame) .. " --> " .. frames_to_srt_time(row.end_frame, fps, base_frame) .. "\n"
        rebuild_parts[#rebuild_parts + 1] = tostring(row.text or "") .. "\n\n"
    end
    if not write_text_file(tostring(srt_path or ""), table.concat(rebuild_parts)) then
        return false, "无法创建重建字幕 SRT"
    end
    return true
end

local function capture_timeline_playhead_timecode(timeline)
    if not timeline then return nil end
    local ok, timecode = pcall(function() return timeline:GetCurrentTimecode() end)
    if ok and trim_text(timecode) ~= "" then
        return tostring(timecode)
    end
    print("[SubFix Generate] 保存播放头失败: " .. tostring(timecode))
    return nil
end

local function restore_timeline_playhead_timecode(timeline, timecode)
    timecode = trim_text(timecode)
    if not timeline or timecode == "" then return false end
    local ok, ret = pcall(function() return timeline:SetCurrentTimecode(timecode) end)
    if ok and ret ~= false then
        print("[SubFix Generate] 已恢复播放头: " .. tostring(timecode))
        return true
    end
    print("[SubFix Generate] 恢复播放头失败: " .. tostring(ret))
    return false
end

local function cleanup_imported_subtitle_media_item(media_pool, media_pool_item, context)
    if not media_pool or not media_pool_item then
        return false
    end

    local ok, result = pcall(function() return media_pool:DeleteClips({media_pool_item}) end)
    if ok and result ~= false then
        print("[SubFix Generate] " .. tostring(context or "字幕写回") .. "后已清理媒体池临时字幕")
        return true
    end

    print(
        "[SubFix Generate] " .. tostring(context or "字幕写回") ..
        "后清理媒体池临时字幕失败: " .. tostring(result)
    )
    return false
end

local function append_rebuild_srt_to_timeline(media_pool, media_pool_item)
    if not media_pool or not media_pool_item then
        return false, "缺少媒体池或字幕媒体项"
    end

    print("[SubFix Generate] 使用稳定模式追加字幕到时间线")
    local ok_append, ret = pcall(function() return media_pool:AppendToTimeline({media_pool_item}) end)
    if ok_append and ret ~= false and ret ~= nil then
        return true, ret
    end
    return false, "追加重建字幕到时间线失败"
end

local function import_rebuild_srt_with_retry(media_pool, rebuild_srt_path)
    local last_error = ""
    for attempt = 1, 2 do
        local root = media_pool and media_pool:GetRootFolder()
        if root then
            local ok_folder, folder_ret = pcall(function() return media_pool:SetCurrentFolder(root) end)
            if not ok_folder or folder_ret == false then
                last_error = "无法切换媒体池根目录"
            end
        end
        local ok_import, items = pcall(function() return media_pool:ImportMedia({rebuild_srt_path}) end)
        if ok_import and items and #items > 0 then
            if attempt > 1 then
                print("[SubFix Generate] 重试导入重建字幕 SRT 成功")
            end
            return items, nil
        end
        last_error = ok_import and "Resolve 返回空媒体项" or tostring(items)
        if attempt < 2 then
            print("[SubFix Generate] 首次导入重建字幕 SRT 失败，准备重试: " .. tostring(last_error))
            if SUBFIX_IS_WINDOWS then
                os.execute("ping -n 2 127.0.0.1 >nul")
            else
                os.execute("sleep 0.25")
            end
        end
    end
    return nil, "导入重建字幕 SRT 失败（已重试）: " .. tostring(last_error)
end

local function rebuild_target_subtitle_track_from_rows(project, timeline, rows, fps, base_frame)
    if not rows or #rows == 0 then return false, "没有可写回字幕" end
    local media_pool = project:GetMediaPool()
    if not media_pool then return false, "无法获取媒体池" end
    local original_playhead_timecode = capture_timeline_playhead_timecode(timeline)
    -- isolate 前的轨道启用快照：所有退出路径（成功/失败）经 finish_rebuild 统一恢复，
    -- 避免其他字幕轨被临时禁用后永久关闭。isolate 成功时目标轨保持启用。
    local enabled_snapshot = nil
    local isolate_applied = false
    local function finish_rebuild(result, err)
        if enabled_snapshot then
            local ok_restore, restore_err = pcall(function()
                return restore_subtitle_track_enabled_states(
                    enabled_snapshot,
                    isolate_applied and TARGET_SUBTITLE_TRACK or nil,
                    timeline
                )
            end)
            if ok_restore then
                print("[SubFix Generate] 已恢复重建前字幕轨启用状态")
            else
                print("[SubFix Generate] 恢复字幕轨启用状态失败: " .. tostring(restore_err))
            end
        end
        restore_timeline_playhead_timecode(timeline, original_playhead_timecode)
        return result, err
    end

    local ensured_items, ensure_err = ensure_subtitle_track_exists(TARGET_SUBTITLE_TRACK, timeline)
    if not ensured_items then return finish_rebuild(false, ensure_err or "目标字幕轨准备失败") end
    unlock_all_subtitle_tracks(timeline)
    enabled_snapshot = snapshot_subtitle_track_enabled(timeline)
    local isolate_ok, isolate_err = isolate_subtitle_target_track(TARGET_SUBTITLE_TRACK, timeline)
    if not isolate_ok then return finish_rebuild(false, isolate_err or "无法切换到目标字幕轨") end
    isolate_applied = true

    local rebuild_srt_path = temp_dir() .. "/GeneratedSelection_Rebuild_" .. os.time() .. "_" .. tostring(math.random(100000, 999999)) .. ".srt"
    local write_ok, write_err = write_rows_to_rebuild_srt(rebuild_srt_path, rows, fps, base_frame)
    if not write_ok then return finish_rebuild(false, write_err) end

    local items, import_err = import_rebuild_srt_with_retry(media_pool, rebuild_srt_path)
    if not items then
        return finish_rebuild(false, import_err)
    end

    local clear_ok, clear_result = clear_subtitle_track_clips(timeline, TARGET_SUBTITLE_TRACK)
    if not clear_ok then return finish_rebuild(false, clear_result) end
    print("[SubFix Generate] 已重建目标字幕轨，清空旧字幕 " .. tostring(clear_result or 0) .. " 条，保留选区外字幕")
    print("[SubFix Generate] SRT 时间已按 timeline_start_frame 转相对时间")
    local media_pool_item = items[1]
    local append_ok, append_err = append_rebuild_srt_to_timeline(media_pool, media_pool_item)
    if not append_ok then
        return finish_rebuild(false, append_err)
    end
    cleanup_imported_subtitle_media_item(media_pool, media_pool_item, "生成选区字幕")
    print("[SubFix Generate] 已提交写回目标字幕轨 " .. tostring(TARGET_SUBTITLE_TRACK) .. "，SRT 行数 " .. tostring(#rows))
    -- 字幕已成功写回、媒体池临时 item 也已清理，删除临时重建 SRT 避免堆积。
    remove_temp_files(rebuild_srt_path)
    return finish_rebuild(true)
end

-- 统一当前时间线获取链；失败按本模块惯例 error()，由外层 pcall 捕获。
local function get_active_timeline_or_throw()
    local resolve_obj = get_resolve()
    if not resolve_obj then error("无法获取 Resolve") end
    local pm = resolve_obj:GetProjectManager()
    local project = pm and pm:GetCurrentProject()
    local timeline = project and project:GetCurrentTimeline()
    if not project or not timeline then error("没有打开的时间线") end
    return timeline
end

local function generate_selection_subtitles(embedded_ctx)
    print("[SubFix Generate] 开始生成选区字幕")
    local timeline = get_active_timeline_or_throw()

    local fps = parse_fps(timeline:GetSetting("timelineFrameRate"))
    local scope, scope_err = read_generation_scope(timeline, fps)
    if not scope or scope.mode ~= WORK_SCOPE_MODE_SELECTION then
        error(scope_err or "请先用 I/O 设置 In/Out 选区")
    end
    print(string.format(
        "[SubFix Generate] 使用范围来源=%s 帧范围 %d-%d，时长 %.2fs%s",
        tostring(scope.source_label or scope.source or "unknown"),
        tonumber(scope.start_frame) or 0,
        tonumber(scope.end_frame) or 0,
        ((tonumber(scope.end_frame) or 0) - (tonumber(scope.start_frame) or 0)) / math.max(1, fps),
        scope.item_name and scope.item_name ~= "" and (" item=" .. tostring(scope.item_name)) or ""
    ))
    local track_items, track_err = ensure_subtitle_track_exists(TARGET_SUBTITLE_TRACK, timeline)
    if not track_items then error(track_err) end

    local audio_sources, audio_err = collect_audio_sources_for_scope(timeline, scope, fps)
    if not audio_sources then error(audio_err) end
    local raw_audio_source_count = #audio_sources
    audio_sources = build_audio_track_options_for_dialog(audio_sources)
    if raw_audio_source_count ~= #audio_sources then
        print(string.format("[SubFix Generate] 音频候选已按轨道折叠: %d -> %d", raw_audio_source_count, #audio_sources))
    end
    local selected_audio_sources = nil
    -- 模式选择下拉已移除，固定使用现场模式
    local subtitle_mode = "live"
    -- 以下缺省值仅在对话框异常/未返回时兜底；正常路径由对话框返回覆盖。
    -- 字幕长度默认标准（≤25字）；识别模型 backend 默认 Qwen 本地（DEFAULT_ASR_BACKEND=="auto"）。
    local max_chars = 25
    local backend = DEFAULT_ASR_BACKEND
    local hotwords_json = nil
    local script_match_enabled = false
    local script_match_text = nil
    local selection_err = nil

    if embedded_ctx and embedded_ctx.controller then
        -- embedded：选项就在右侧面板上，直接从 controller 取；无弹窗、无 RunLoop 嵌套。
        local options, options_err = embedded_ctx.controller:collect_current_options()
        if not options then error(options_err or "生成选项无效") end
        selected_audio_sources = options.sources
        subtitle_mode = options.subtitle_mode
        max_chars = options.max_chars
        backend = options.backend
        hotwords_json = options.hotwords_json
        script_match_enabled = options.script_match_enabled
        script_match_text = options.script_match_text
    else
        -- 始终弹出对话框（含只有 1 个音频候选的情形）：即便只有一条音频轨，用户也需要能
        -- 选择识别模型（Qwen/豆包）与字幕长度，故不再对单候选自动跳过弹窗、直接生成。
        while true do
            ---@diagnostic disable-next-line: lowercase-global
            selected_audio_sources, subtitle_mode, max_chars, backend, hotwords_json, script_match_enabled, script_match_text, selection_err = SubFixGenerateSelectionCore._show_audio_track_selection_dialog(audio_sources, scope, fps)
            if selection_err ~= "__subfix_install_qwen__" then break end
            local install_ok, install_err, install_status = false, nil, nil
            repeat
                install_ok, install_err, install_status = install_local_qwen_with_progress()
            until install_ok or install_status == "cancelled" or not show_qwen_install_failed_dialog(install_err)
            if not install_ok then
                if install_status == "cancelled" then error("已取消") end
                error(install_err or "本地 Qwen 安装失败")
            end
            save_last_engine_backend("auto")
        end
        if not selected_audio_sources then error(selection_err or "已取消") end
    end
    local selected_track_sources = collect_selected_track_sources(selected_audio_sources)
    if type(selected_track_sources) ~= "table" or #selected_track_sources == 0 then
        error("所选轨道没有可识别音频片段")
    end
    local raw_selected_track_source_count = #selected_track_sources
    local optimized_track_sources, source_optimization = optimize_selected_track_sources_for_generation(selected_track_sources, fps)
    selected_track_sources = optimized_track_sources or selected_track_sources
    local effective_audio_seconds = selected_sources_effective_audio_seconds(selected_track_sources, fps)
    print(string.format(
        "[SubFix Generate] 使用 %d 条音频轨；音频 %d → 去重 %d；有效音频 %s",
        #selected_audio_sources,
        raw_selected_track_source_count,
        tonumber(source_optimization and source_optimization.deduped_source_count) or #selected_track_sources,
        format_generate_audio_duration(effective_audio_seconds)
    ))
    for source_index, source in ipairs(selected_track_sources) do
        print(string.format(
            "[SubFix Generate] 片段 %d/%d: A%d #%d %s timeline=%d-%d source=%.3f-%.3fs mapping=%s channel=%s muted=%s linked_offset=%s fallback=%s",
            source_index,
            #selected_track_sources,
            tonumber(source.track_index) or 0,
            tonumber(source.item_index) or 0,
            tostring(source.file_name or source.file_path or ""),
            tonumber(source.start_frame) or 0,
            tonumber(source.end_frame) or 0,
            tonumber(source.source_start_seconds) or 0,
            tonumber(source.source_end_seconds) or 0,
            tostring(source.audio_mapping_source or ""),
            tostring(source.audio_channel_index or ""),
            tostring(source.audio_mapping_muted == true),
            tostring(source.linked_offset_samples or ""),
            tostring(source.audio_mapping_fallback_reason or "")
        ))
    end

    local progress_state, progress_err = show_generate_progress_window(embedded_ctx)
    if not progress_state then error(progress_err or "无法打开进度窗口") end

    local root = temp_dir()
    local uid = tostring(os.time()) .. "_" .. tostring(math.random(100000, 999999))
    local json_path = root .. "/GeneratedSelection_" .. uid .. ".json"
    local srt_path = root .. "/GeneratedSelection_" .. uid .. ".srt"
    local batch_plan_path = root .. "/GeneratedSelection_" .. uid .. ".batch_plan.json"
    local sm_plan_path = nil
    local audio_diag_path = json_path .. ".audio_source.json"
    if not write_generate_batch_plan(batch_plan_path, selected_track_sources, fps) then
        finish_generate_progress_window(progress_state, "失败", "无法写入批量生成计划")
        error("无法写入批量生成计划")
    end
    if write_selected_audio_source_diagnostic(audio_diag_path, selected_audio_sources, scope, source_optimization) then
        print("[SubFix Generate] 已写入音频源诊断: " .. audio_diag_path)
    end
    local helper_ok, helper_err, helper_status = nil, nil, nil
    if script_match_enabled and type(script_match_text) == "string" and trim_text(script_match_text) ~= "" then
        -- 文稿匹配分支：用本地 Qwen3 强制对齐器把文稿对到选区音频时间轴。
        -- 先按用户粘贴的显式换行硬断（尊重手动分段），再按语义/标点软断，最后按“每行最大字数”做软长度约束。
        local script_rows = split_script_into_subtitle_rows(subfix_ansi_to_utf8(script_match_text), max_chars)
        print("[SubFix Generate] 文稿断句: " .. #script_rows .. " 行（软长度上限 " .. tostring(max_chars) .. "）")
        if #script_rows == 0 then
            finish_generate_progress_window(progress_state, "失败", "文稿内容为空，无法匹配")
            error("文稿内容为空")
        end
        sm_plan_path = root .. "/GeneratedSelection_" .. uid .. ".script_match_plan.json"
        if not write_script_match_batch_plan(sm_plan_path, selected_track_sources, script_rows, fps, scope.timeline_start_frame, DEFAULT_ASR_LANGUAGE) then
            finish_generate_progress_window(progress_state, "失败", "无法写入文稿匹配计划")
            error("无法写入文稿匹配计划")
        end
        helper_ok, helper_err, helper_status = run_script_match_helper_with_progress(
            sm_plan_path,
            json_path,
            scope.timeline_start_frame,
            fps,
            DEFAULT_ASR_LANGUAGE,
            progress_state
        )
    else
        helper_ok, helper_err, helper_status = run_asr_helper_batch_with_progress(
            batch_plan_path,
            srt_path,
            json_path,
            scope.timeline_start_frame,
            fps,
            progress_state,
            #selected_track_sources,
            subtitle_mode,
            max_chars,
            backend,
            hotwords_json,
            effective_audio_seconds
        )
        while not helper_ok and helper_status ~= "cancelled" and (backend == "doubao_asr" or backend == "doubao_asr_v2") and is_doubao_asr_failure(helper_err) do
            -- 进度窗口中的错误文本通常包含 Python 完整日志；隐藏它，改由明确操作的短弹窗呈现。
            pcall(function() progress_state.window:Hide() end)
            local action = show_doubao_asr_failure_action_dialog(helper_err)
            if action == "close" then break end

            if action == "local_qwen" then
                backend = DEFAULT_ASR_BACKEND
                print("[SubFix Generate] 用户选择云端失败后改用本地 Qwen")
            else
                print("[SubFix Generate] 用户选择重试豆包（云端）")
            end

            local replacement_progress, replacement_err = show_generate_progress_window(embedded_ctx)
            if not replacement_progress then
                helper_ok = false
                helper_err = replacement_err or "无法重新打开生成进度窗口"
                break
            end
            progress_state = replacement_progress
            helper_ok, helper_err, helper_status = run_asr_helper_batch_with_progress(
                batch_plan_path,
                srt_path,
                json_path,
                scope.timeline_start_frame,
                fps,
                progress_state,
                #selected_track_sources,
                subtitle_mode,
                max_chars,
                backend,
                hotwords_json,
                effective_audio_seconds
            )
        end
    end
    if not helper_ok then
        finish_generate_progress_window(progress_state, helper_status == "cancelled" and "已取消" or "失败", helper_err)
        error(helper_err)
    end

    local function cancel_postprocess_if_requested(should_continue)
        if should_continue then return end
        finish_generate_progress_window(progress_state, "已取消", "已取消，目标字幕轨未修改")
        error("已取消")
    end

    cancel_postprocess_if_requested(update_generate_postprocess_stage(
        progress_state, "解析结果", "正在解析生成结果", 96
    ))
    local raw_generated_rows, raw_generated_err = parse_generated_json_subtitle_rows(json_path)
    if not raw_generated_rows then
        finish_generate_progress_window(progress_state, "失败", raw_generated_err)
        error(raw_generated_err)
    end

    cancel_postprocess_if_requested(update_generate_postprocess_stage(
        progress_state, "合并字幕", "正在合并识别结果", 97
    ))
    local priority_rows, priority_err = nil, nil
    if subtitle_mode == "live" then
        priority_rows, priority_err = merge_generated_rows_for_live_writeback(raw_generated_rows, scope)
    else
        priority_rows, priority_err = merge_generated_rows_with_track_priority(raw_generated_rows, scope)
    end
    if not priority_rows then
        finish_generate_progress_window(progress_state, "失败", priority_err)
        error(priority_err)
    end
    local generated_rows, generated_err = merge_generated_rows_for_writeback({priority_rows}, scope)
    if not generated_rows then
        finish_generate_progress_window(progress_state, "失败", generated_err)
        error(generated_err)
    end

    cancel_postprocess_if_requested(update_generate_postprocess_stage(
        progress_state, "备份字幕", "正在备份目标字幕轨", 98
    ))
    local backup_path, backup_err, existing_target_rows, backup_status = backup_target_track(
        timeline,
        TARGET_SUBTITLE_TRACK,
        fps,
        scope.timeline_start_frame,
        function(completed, total)
            local message = string.format("正在备份目标字幕轨 %d/%d", completed, total)
            update_generate_progress_window(progress_state, {
                stage = "备份字幕",
                message = message,
                progress_index = 98,
                progress_total = 100
            }, message)
            return pump_generate_progress_events(progress_state)
        end
    )
    if backup_status == "cancelled" then
        cancel_postprocess_if_requested(false)
    end
    if not existing_target_rows then
        finish_generate_progress_window(progress_state, "失败", backup_err or "无法读取目标字幕轨")
        error(backup_err or "无法读取目标字幕轨")
    end
    if backup_err then
        finish_generate_progress_window(progress_state, "失败", backup_err)
        error(backup_err)
    end
    if backup_path then
        print("[SubFix Generate] 已备份目标字幕轨: " .. backup_path)
    end

    cancel_postprocess_if_requested(update_generate_postprocess_stage(
        progress_state, "写回时间线", "正在写回字幕", 99
    ))
    -- 清空目标字幕轨后无法保证可逆，因此只在真正写回前关闭取消入口。
    set_generate_progress_cancel_enabled(progress_state, false, "写回中")
    show_writeback_progress_overlay(progress_state)
    local composite_rows = build_composite_rows_for_writeback(existing_target_rows, scope, generated_rows)
    local import_ok, import_err = rebuild_target_subtitle_track_from_rows(project, timeline, composite_rows, fps, scope.timeline_start_frame)
    if not import_ok then
        finish_generate_progress_window(progress_state, "失败", import_err)
        error(import_err)
    end
    -- 成功后清理过程临时文件（JSON/SRT/ASR 计划/文稿匹配计划）。
    -- 刻意保留 audio_diag_path（.audio_source.json 音频源诊断，便于事后核对）；
    -- 失败/取消分支不清理，保留现场供排查。进度文件已在各 helper wrapper 内删除。
    remove_temp_files(batch_plan_path, sm_plan_path, json_path, srt_path)
    finish_generate_progress_window(progress_state, "完成", "选区字幕已生成并写回时间线")
    pcall(function() progress_state.window:Hide() end)
    print("[SubFix Generate] 生成选区字幕完成，诊断: " .. audio_diag_path)
    return true
end

-- ============================================================================
-- embedded API：供 SubFix.lua 主窗口右侧常驻面板使用（standalone 入口不调用）。
--
-- 生命周期：
--   host 设置 _G.SUBFIX_GENERATE_CORE_HOST 后 loadfile 本模块；
--   build_embedded_panel() 取描述符放进主窗口 HGroup；
--   主窗口创建完成后 attach_embedded_panel(win, callbacks) 绑定事件；
--   refresh_embedded_context() 填充选区与音频候选；
--   点击「开始生成」走 start_embedded_generation()（事件驱动，同步执行管线 +
--   嵌套消息循环等待后台 ASR），完成回调 callbacks.on_finish；
--   宿主关闭时 shutdown_embedded() 收起子弹窗。
--
-- 本块同样用 do...end 限制 local 数量（Lua 5.1 主 chunk 200 local 上限）。
-- ============================================================================
do
local embedded_controller = nil
local embedded_host_callbacks = nil
local embedded_generation_running = false

local function embedded_find_item(item_id)
    if not embedded_controller or not embedded_controller.window then return nil end
    local ok, items = pcall(function() return embedded_controller.window:GetItems() end)
    if ok and type(items) == "table" then return items[item_id] end
    return nil
end

local function embedded_set_panel_running(running)
    local confirm_btn = embedded_find_item("GenerateSelectionConfirmBtn")
    local cancel_btn = embedded_find_item("GenerateSelectionCancelBtn")
    if confirm_btn then
        pcall(function()
            confirm_btn.Enabled = not running
            confirm_btn.Text = running and "生成中…" or "开始生成"
        end)
    end
    if cancel_btn then
        pcall(function()
            cancel_btn.Enabled = running == true
            cancel_btn.Hidden = running ~= true
            cancel_btn.Text = "取消生成"
        end)
    end
end

local function embedded_show_progress_block(visible)
    local block = embedded_find_item("GenerateEmbeddedProgressBlock")
    if block then pcall(function() block.Hidden = not visible end) end
    -- wide 页：生成中折叠选项区（narrow 无此控件，Find 返回 nil 静默跳过）。
    local options = embedded_find_item("GenerateEmbeddedOptionsBlock")
    if options then pcall(function() options.Hidden = visible == true end) end
end

local function embedded_notify_busy(running)
    if type(embedded_host_callbacks) == "table" and type(embedded_host_callbacks.on_busy_changed) == "function" then
        pcall(embedded_host_callbacks.on_busy_changed, running)
    end
end

local function embedded_notify_finish(ok, err)
    if type(embedded_host_callbacks) == "table" and type(embedded_host_callbacks.on_finish) == "function" then
        pcall(embedded_host_callbacks.on_finish, ok, err)
    end
end

-- 让「完成/失败」状态在面板停留片刻再收起进度块；timer 通过宿主路由分发。
local function embedded_schedule_reset(delay_ms)
    local timer_id = "GenerateEmbeddedResetTimer_" .. tostring(os.time()) .. "_" .. tostring(math.random(100000, 999999))
    local reset_timer = ui:Timer({ID = timer_id, Interval = delay_ms or 1400, SingleShot = true})
    local registered = register_ui_timer(reset_timer, function()
        pcall(function() reset_timer:Stop() end)
        unregister_ui_timer(timer_id)
        embedded_show_progress_block(false)
    end)
    if registered then pcall(function() reset_timer:Start() end) end
end

local function start_embedded_generation()
    if not EMBEDDED or not embedded_controller or embedded_generation_running then return end
    local options, why = embedded_controller:collect_start_options()
    if why == "need_qwen" then
        embedded_controller:open_qwen_download_dialog()
        return
    end
    if not options then
        embedded_controller:set_info(why)
        return
    end

    embedded_generation_running = true
    SubFixGenerateSelectionCore._active_embedded_progress_state = nil
    embedded_show_progress_block(true)
    embedded_set_panel_running(true)
    embedded_notify_busy(true)

    local ok, err = pcall(function()
        return generate_selection_subtitles({controller = embedded_controller})
    end)

    embedded_generation_running = false
    SubFixGenerateSelectionCore._active_embedded_progress_state = nil
    embedded_set_panel_running(false)
    embedded_notify_busy(false)

    if not ok then
        print("[SubFix Generate] embedded 生成失败: " .. tostring(err))
        local status_label = embedded_find_item("GenerateEmbeddedStatusLabel")
        if status_label then pcall(function() status_label.Text = "失败：" .. tostring(err) end) end
        embedded_notify_finish(false, tostring(err))
        embedded_schedule_reset(4500)
        return
    end
    embedded_notify_finish(true)
    embedded_schedule_reset(1400)
end

-- Qwen 下载确认后的路径：安装成功直接开跑（standalone 的安装进度窗在此复用，
-- 它自带 RunLoop，属于嵌套模态，与宿主主循环兼容）。
local function install_qwen_and_generate_embedded()
    if not EMBEDDED or not embedded_controller or embedded_generation_running then return end
    embedded_generation_running = true
    embedded_show_progress_block(true)
    local status_label = embedded_find_item("GenerateEmbeddedStatusLabel")
    if status_label then pcall(function() status_label.Text = "正在下载并安装本地 Qwen…" end) end
    embedded_set_panel_running(true)
    embedded_notify_busy(true)

    local install_ok, install_err, install_status = false, nil, nil
    repeat
        install_ok, install_err, install_status = install_local_qwen_with_progress()
    until install_ok or install_status == "cancelled" or not show_qwen_install_failed_dialog(install_err)

    if not install_ok then
        print("[SubFix Generate] embedded Qwen 安装取消/失败: " .. tostring(install_err))
        embedded_generation_running = false
        embedded_set_panel_running(false)
        embedded_notify_busy(false)
        embedded_schedule_reset(1200)
        return
    end
    save_last_engine_backend("auto")
    embedded_controller:select_engine(1)
    embedded_generation_running = false
    start_embedded_generation()
end

-- 重新读取当前时间线 In/Out 选区与重叠音频，刷新右侧轨道树。只读，不创建字幕轨。
-- P11：embedded 选区上下文指纹。时间线对象/帧率/In-Out/折叠后音频候选完全相同时，
-- 跳过 controller:set_context 对轨道树的重建（避免清空用户勾选与无谓的 UI 抖动）。
-- 整块封进 do 块（不占主 chunk local 名额），直接挂到模块导出表上。
do
local embedded_context_last_fp = nil

local function compute_embedded_context_fingerprint(timeline, fps, scope, options)
    local parts = {
        "tl=" .. tostring(timeline),
        "fps=" .. tostring(fps),
        "scope=" .. tostring(scope and scope.mode or "")
            .. ":" .. tostring(scope and scope.start_frame or "")
            .. ":" .. tostring(scope and scope.end_frame or ""),
        "n=" .. tostring(type(options) == "table" and #options or 0),
    }
    for _, option in ipairs(options or {}) do
        local segments = {}
        local grouped = type(option.track_sources) == "table" and option.track_sources or { option }
        for _, source in ipairs(grouped) do
            segments[#segments + 1] = table.concat({
                tostring(source.file_path or source.file_name or ""),
                tostring(source.source_start_seconds or ""),
                tostring(source.source_end_seconds or ""),
                tostring(source.start_frame or ""),
                tostring(source.end_frame or ""),
                tostring(source.item_index or ""),
                tostring(source.audio_channel_index or ""),
                tostring(source.linked_offset_samples or ""),
            }, ",")
        end
        parts[#parts + 1] = tostring(option.track_index or "")
            .. "|" .. tostring(option.display_label or option.track_name or "")
            .. "|muted=" .. tostring(option.audio_mapping_muted == true)
            .. "|[" .. table.concat(segments, ";") .. "]"
    end
    return table.concat(parts, "\n")
end

-- force=true：「重新检测」按钮等显式用户动作，即便指纹相同也强制重建轨道树。
SubFixGenerateSelectionCore.refresh_embedded_context = function(force)
    if not EMBEDDED or not embedded_controller then
        return false, "生成面板尚未初始化"
    end
    local ok, err = pcall(function()
        local timeline = get_active_timeline_or_throw()
        local fps = parse_fps(timeline:GetSetting("timelineFrameRate"))
        local scope, scope_err = read_generation_scope(timeline, fps)
        if not scope or scope.mode ~= WORK_SCOPE_MODE_SELECTION then
            embedded_context_last_fp = nil
            embedded_controller:set_context({}, scope, fps)
            embedded_controller:set_info(scope_err or "请先用 I/O 设置 In/Out 选区")
            error(scope_err or "请先用 I/O 设置 In/Out 选区")
        end
        local audio_sources, audio_err = collect_audio_sources_for_scope(timeline, scope, fps)
        if not audio_sources then
            embedded_context_last_fp = nil
            embedded_controller:set_context({}, scope, fps)
            embedded_controller:set_info(audio_err)
            error(audio_err)
        end
        local raw_count = #audio_sources
        audio_sources = build_audio_track_options_for_dialog(audio_sources)
        if raw_count ~= #audio_sources then
            print(string.format("[SubFix Generate] embedded 音频候选折叠: %d -> %d", raw_count, #audio_sources))
        end
        local next_fp = compute_embedded_context_fingerprint(timeline, fps, scope, audio_sources)
        if force ~= true and embedded_context_last_fp ~= nil and embedded_context_last_fp == next_fp then
            -- 选区与音频候选逐字段一致：保留轨道树与用户当前勾选，不做任何 UI 变更。
            return
        end
        embedded_controller:set_context(audio_sources, scope, fps)
        embedded_controller:set_info("选择用于识别的音频轨道")
        embedded_context_last_fp = next_fp
    end)
    if not ok then
        return false, tostring(err)
    end
    return true
end
end

local function build_embedded_panel(opts)
    if not EMBEDDED then
        error("build_embedded_panel 仅在 embedded 宿主注入下可用")
    end
    if not embedded_controller then
        embedded_controller = SubFixGenerateSelectionCore._new_generation_panel_controller("embedded")
    end
    local wide = type(opts) == "table" and opts.wide == true
    return embedded_controller:build_descriptor(wide)
end

local function attach_embedded_panel(host_window, callbacks)
    if not EMBEDDED then
        return false, "当前不是 embedded 模式"
    end
    if type(host_window) ~= "table" then
        return false, "缺少宿主窗口"
    end
    if not embedded_controller then
        embedded_controller = SubFixGenerateSelectionCore._new_generation_panel_controller("embedded")
    end
    embedded_host_callbacks = callbacks or {}
    -- 宿主运行参数：脚本根目录与目标字幕轨（等价于 standalone run(options)）。
    runtime_options = {
        script_root = embedded_host_callbacks.script_root,
        target_subtitle_track = embedded_host_callbacks.target_subtitle_track,
    }
    TARGET_SUBTITLE_TRACK = tonumber(embedded_host_callbacks.target_subtitle_track) or 1
    embedded_controller:bind(host_window, {
        request_confirm = start_embedded_generation,
        request_cancel = function()
            local state = SubFixGenerateSelectionCore._active_embedded_progress_state
            if state and not state.finished and state.cancel_allowed ~= false then
                state.cancel_requested = true
            end
        end,
        request_qwen_install = install_qwen_and_generate_embedded,
        rescan = function()
            -- P11：用户显式点击「重新检测」，绕过指纹缓存强制重建轨道树
            SubFixGenerateSelectionCore.refresh_embedded_context(true)
        end,
    })
    -- P5：后台预热本地 Qwen 状态，后续引擎门控/状态弹窗直接命中缓存，不再同步起 python 卡 UI
    pcall(qwen_check.preheat)
    return true
end

local function shutdown_embedded()
    -- 宿主在任务运行中关闭：先置取消标志（轮询器下一 tick 走正常取消收尾、退出嵌套
    -- RunLoop），再按 PID 直接回收进程树兜底，防止达芬奇退出后残留孤儿 python/ffmpeg。
    local state = SubFixGenerateSelectionCore._active_embedded_progress_state
    if type(state) == "table" and not state.finished and state.cancel_allowed ~= false then
        state.cancel_requested = true
    end
    if active_background_pid_file then
        pcall(function() kill_background_process(active_background_pid_file) end)
    end
    -- P5：停止 Qwen 状态预热轮询（隐藏子进程自身很快结束，残留 json 由 temp 清理兜底）
    pcall(qwen_check.finish)
    if embedded_controller then
        pcall(function() embedded_controller:hide_all_subwindows() end)
    end
    SubFixGenerateSelectionCore._active_embedded_progress_state = nil
end

SubFixGenerateSelectionCore.is_embedded_mode = function() return EMBEDDED end
SubFixGenerateSelectionCore.build_embedded_panel = build_embedded_panel
SubFixGenerateSelectionCore.attach_embedded_panel = attach_embedded_panel
SubFixGenerateSelectionCore.start_embedded_generation = start_embedded_generation
SubFixGenerateSelectionCore.shutdown_embedded = shutdown_embedded
end

-- standalone（生成选区字幕.lua 直接运行）致命错误可视化：原本只 print，控制台对用户
-- 不可见，双击运行失败时表现为"什么都没发生"。这里用与其他对话框一致的原生小窗展示；
-- 无 ui/dispatcher（极端初始化失败）时回退 print。
local function show_standalone_fatal_error_dialog(message)
    if not dispatcher or not ui then
        print("[SubFix Generate] 致命错误（无法显示消息窗）: " .. tostring(message or "未知错误"))
        return
    end
    local error_window = dispatcher:AddWindow({
        ID = "GenerateStandaloneFatalWindow",
        WindowTitle = "SubFix · 生成失败",
        Geometry = SUBFIX_WINDOW_GEOMETRY.centered_geometry({560, 300, 560, 230}),
    },
    ui:VGroup{
        Font = DIALOG_FONT,
        StyleSheet = SUBFIX_ROOT_STYLESHEET,
        Spacing = 8,
        ContentsMargins = 14,
        ui:Label{Text = "生成选区字幕后遇到错误：", Weight = 0},
        ui:TextEdit{ID = "GenerateStandaloneFatalText", PlainText = tostring(message or "未知错误"), ReadOnly = true, Weight = 1, MinimumSize = {0, 100}},
        ui:HGroup{
            Weight = 0,
            MinimumSize = {0, 34},
            ui:Button{ID = "GenerateStandaloneFatalOkBtn", Text = "确定", MinimumSize = {96, 28}}
        }
    })
    local function close_error_window()
        pcall(function() error_window:Hide() end)
        pcall(function() dispatcher:ExitLoop() end)
    end
    function error_window.On.GenerateStandaloneFatalOkBtn.Clicked(ev) close_error_window() end
    function error_window.On.GenerateStandaloneFatalWindow.Close(ev) close_error_window() end
    error_window:Show()
    dispatcher:RunLoop()
    pcall(function() error_window:Hide() end)
end

function SubFixGenerateSelectionCore.run(options)
    if EMBEDDED then
        return false, "embedded 模式请使用 build_embedded_panel/attach_embedded_panel"
    end
    options = options or {}
    runtime_options = options
    TARGET_SUBTITLE_TRACK = tonumber(options.target_subtitle_track) or 1

    local ok, err = pcall(function()
        return generate_selection_subtitles()
    end)
    runtime_options = {}
    if not ok then
        print("[SubFix Generate] 失败: " .. tostring(err))
        -- 用户主动取消（选择对话框/安装流程）不是致命错误，不弹失败窗。
        if tostring(err) ~= "已取消" then
            show_standalone_fatal_error_dialog(err)
        end
        return false, tostring(err)
    end
    return true
end

return SubFixGenerateSelectionCore
