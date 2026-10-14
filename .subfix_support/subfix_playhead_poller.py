# -*- coding: utf-8 -*-
"""SubFix 播放头外部轮询器（由 SubFix.lua 在“播放跟随”开启时以隐藏进程启动）。

真机已证实的关键事实：
1. timeline:GetCurrentTimecode() 无论进程内外，【播放期间都会冻结/阻塞】，暂停才刷新；
2. fusionscript 的 Python 绑定在阻塞期间持有 GIL，同进程任何线程（含键盘钩子）都会被
   饿死。所以读取必须用【一次性子进程】做进程级隔离，卡住就杀，绝不在本进程内直接调用。

播放状态双信号（任一命中即视为播放中）：
A. 键盘权威信号：WH_KEYBOARD_LL 监听 空格/J/K/L（仅当前台窗口属于 Resolve 进程）。
   能给出方向与倍速（J/L 的 1/2/4/8 档）。鼠标点播放按钮时没有按键信号。
B. 阻塞启发式兜底：播放期一次性读取子进程必然在 GetCurrentTimecode 上卡死、被 1.2s
   超时杀掉；暂停/跳转（暂停态）时读取会很快返回。因此“读取被超时杀掉”是播放的强
   证据，不依赖键盘与焦点——鼠标点播放、面板自带播放按钮都能检出。方向缺省按上次
   resume 方向（通常 +1）。暂停后首个快速返回的真实帧立即回贴校准。
   反证：若键盘声称播放中，却连续两次拿到【帧不动】的快速读取，说明空格被输入框吞掉
   （Resolve 实际没播），自动改判暂停。

架构：
- 主进程：看门线程用【进程快照找 Resolve.exe 的 PID】（标题枚举兜底）；键盘钩子线程
  安装 WH_KEYBOARD_LL 并泵消息，回调只做非阻塞调用；主循环每 50ms 原子写输出文件，
  并把按键/状态变迁追加到 <输出>.trace.txt 供真机诊断。
- 读取子进程：本文件 --oneshot 模式，独立连接 Resolve 读一次真实播放头后退出。
  暂停节奏 0.25s / 播放 1.0s；超时 1.2s 即杀（播放中必然超时）。
- 推算：播放时以上一真实帧为锚，按 帧率×墙上时钟 推进；暂停后首个真实帧自动校准。

用法：
    python subfix_playhead_poller.py <输出文件> [轮询毫秒]   # 轮询模式
    python subfix_playhead_poller.py --oneshot               # 单次读取模式
        stdout: rel=<帧> tc=<时间码> fps=<帧率> name=<时间线名> start=<起始帧>

输出单行：
    rel=<帧> playing=<0|1> speed=<帧每秒> tc=<时间码> real=<真实帧或-> seq=<序号> src=key|heur|real key=<按键轨迹>
停止：生成 <输出文件>.stop。诊断：<输出文件>.trace.txt（仅事件，无高频刷屏）。
"""
import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes

# ---------------- Resolve 连接 ----------------
def setup_scripting():
    candidates = []
    if sys.platform.startswith("win"):
        candidates.append(
            r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support"
            r"\Developer\Scripting\Modules")
        lib_default = r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"
    elif sys.platform == "darwin":
        candidates.append(
            "/Library/Application Support/Blackmagic Design/DaVinci Resolve/"
            "Developer/Scripting/Modules")
        lib_default = (
            "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/"
            "Libraries/Fusion/fusionscript.so")
    else:
        candidates.append("/opt/resolve/Developer/Scripting/Modules")
        lib_default = "/opt/resolve/libs/Fusion/fusionscript.so"
    for path in candidates:
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    if not os.environ.get("RESOLVE_SCRIPT_LIB") and os.path.exists(lib_default):
        os.environ["RESOLVE_SCRIPT_LIB"] = lib_default
    import DaVinciResolveScript as dvr  # noqa: E402
    return dvr


def tc_to_frame(tc, fps):
    if not tc:
        return None
    parts = str(tc).replace(";", ":").split(":")
    if len(parts) != 4:
        return None
    try:
        hh, mm, ss, ff = (int(float(p)) for p in parts)
    except (ValueError, TypeError):
        return None
    return (hh * 3600 + mm * 60 + ss) * int(round(fps)) + ff


def write_atomic(path, text):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except OSError:
        pass


# ---------------- 单次读取子进程入口 ----------------
def run_oneshot():
    dvr = setup_scripting()
    resolve = dvr.scriptapp("Resolve")
    if resolve is None:
        print("ERR=scriptapp 返回空")
        return 1
    project = resolve.GetProjectManager().GetCurrentProject()
    timeline = project.GetCurrentTimeline() if project else None
    if timeline is None:
        pname = project.GetName() if project else "<无项目>"
        print("ERR=当前项目没有打开的时间线（项目：%s）" % pname)
        return 1
    tc = timeline.GetCurrentTimecode()
    try:
        fps = float(timeline.GetSetting("timelineFrameRate") or 25.0)
    except (ValueError, TypeError):
        fps = 25.0
    try:
        start = int(timeline.GetStartFrame())
    except (ValueError, TypeError):
        start = tc_to_frame(timeline.GetStartTimecode(), fps) or 0
    rel = (tc_to_frame(tc, fps) or 0) - start
    print("rel=%d tc=%s fps=%.3f name=%s start=%d"
          % (rel, str(tc), fps, str(timeline.GetName()), start))
    return 0


# ---------------- 传输状态（JKL 档位） ----------------
LEVELS = (1, 2, 4, 8)
VK_SPACE = 0x20
VK_J = 0x4A
VK_K = 0x4B
VK_L = 0x4C


class Transport(object):
    def __init__(self):
        self.lock = threading.Lock()
        self.level = 0
        self.resume = 1
        self.last_key = ""

    def _next_level(self, positive):
        if positive:
            if self.level < 0:
                return 1
            nxt = LEVELS[-1]
            for lv in LEVELS:
                if lv > self.level:
                    nxt = lv
                    break
            return nxt
        else:
            if self.level > 0:
                return -1
            nxt = -LEVELS[-1]
            for lv in LEVELS:
                if lv > -self.level:
                    nxt = -lv
                    break
            return nxt

    def press(self, vk):
        with self.lock:
            if vk == VK_K:
                self.level = 0
                self.last_key = "K"
            elif vk == VK_L:
                self.level = self._next_level(True)
                self.resume = 1
                self.last_key = "L%d" % self.level
            elif vk == VK_J:
                self.level = self._next_level(False)
                self.resume = -1
                self.last_key = "J%d" % self.level
            elif vk == VK_SPACE:
                if self.level == 0:
                    self.level = self.resume if self.resume in (1, -1) else 1
                    if abs(self.level) not in LEVELS:
                        self.level = 1 if self.level > 0 else -1
                    self.last_key = "SP>%d" % self.level
                else:
                    self.resume = 1 if self.level > 0 else -1
                    self.level = 0
                    self.last_key = "SP||"

    def reset_level(self, tag="RST"):
        """阻塞反证 / 启发式下按空格暂停时，把档位拉回 0（保留 resume 方向）。"""
        with self.lock:
            if self.level != 0:
                self.resume = 1 if self.level > 0 else -1
            self.level = 0
            self.last_key = tag

    def snapshot(self):
        with self.lock:
            return self.level, self.resume, self.last_key


# ---------------- Resolve 进程发现 ----------------
def find_resolve_pids_by_process(kernel32):
    """用进程快照列出所有 Resolve.exe 的 PID（不依赖窗口标题，最稳）。"""
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                         ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    invalid = ctypes.c_void_p(-1).value
    if not snap or snap == invalid:
        return []
    pids = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            name = (entry.szExeFile or "").lower()
            if name == "resolve.exe":
                pids.append(int(entry.th32ProcessID))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return pids


# ---------------- 键盘低级钩子（仅 Windows） ----------------
def start_keyboard_hook(transport, trace):
    if not sys.platform.startswith("win"):
        return

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYUP = 0x0105

    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                         wintypes.HINSTANCE, wintypes.DWORD]
    user32.CallNextHookEx.restype = ctypes.c_long
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.GetMessageW.restype = ctypes.c_long
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    down = set()
    hook_dead = {"v": False}
    # Resolve 进程号：优先进程快照（Resolve.exe），兜底枚举 “DaVinci Resolve” 主窗口。
    # SubFix / Hooper 面板也由同一个 Resolve 进程承载，故钩子判定条件是“前台窗口属于
    # Resolve 进程”。回调里只能用非阻塞 API（GetForegroundWindow +
    # GetWindowThreadProcessId），GetWindowText 跨进程 SendMessage 播放时会堵死钩子链。
    resolve_pid = {"v": None}
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def window_title(hwnd):
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def watcher_loop():
        first = True
        while True:
            pid = None
            src = ""
            try:
                pids = find_resolve_pids_by_process(kernel32)
                if pids:
                    pid = max(pids)
                    src = "proc"
            except Exception as exc:  # noqa: BLE001
                trace("watch 进程快照失败: %r" % exc)

            if pid is None:
                found_pids = []

                def _enum(hwnd, _lparam):
                    try:
                        if hwnd and user32.IsWindowVisible(hwnd):
                            if window_title(hwnd).startswith("DaVinci Resolve"):
                                wpid = wintypes.DWORD(0)
                                user32.GetWindowThreadProcessId(
                                    hwnd, ctypes.byref(wpid))
                                if wpid.value:
                                    found_pids.append(wpid.value)
                    except Exception:  # noqa: BLE001
                        pass
                    return True

                try:
                    user32.EnumWindows(EnumProc(_enum), 0)
                except Exception:  # noqa: BLE001
                    found_pids = []
                if found_pids:
                    pid = max(found_pids)
                    src = "title"

            if pid != resolve_pid["v"]:
                resolve_pid["v"] = pid
                trace("watch resolve_pid=%s via=%s" % (pid, src or "none"))
            time.sleep(0.1 if first else 1.5)
            first = False

    def unified(code, wparam, lparam):
        try:
            if code == 0:
                kb = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                vk = int(kb.vkCode)
                if wparam in (WM_KEYUP, WM_SYSKEYUP):
                    down.discard(vk)
                elif wparam == WM_KEYDOWN:
                    fg = user32.GetForegroundWindow()
                    fg_pid = wintypes.DWORD(0)
                    if fg:
                        user32.GetWindowThreadProcessId(fg, ctypes.byref(fg_pid))
                    is_repeat = bool(kb.flags & 0x4000) or vk in down
                    down.add(vk)
                    if vk in (VK_SPACE, VK_J, VK_K, VK_L):
                        accepted = bool(resolve_pid["v"]
                                        and fg_pid.value == resolve_pid["v"])
                        if accepted:
                            if not is_repeat:
                                transport.press(vk)
                                lvl, _res, key = transport.snapshot()
                                trace("key ACCEPT vk=0x%02X fgpid=%s rpid=%s "
                                      "repeat=%d -> level=%s key=%s"
                                      % (vk, fg_pid.value, resolve_pid["v"],
                                         1 if is_repeat else 0, lvl, key))
                        else:
                            trace("key IGNORE vk=0x%02X fgpid=%s rpid=%s"
                                  % (vk, fg_pid.value, resolve_pid["v"]))
        except Exception as exc:  # noqa: BLE001
            hook_dead["v"] = True
            trace("hook 回调异常: %r" % exc)
        return user32.CallNextHookEx(None, code, wparam, lparam)

    HOOKPROC = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM,
                                wintypes.LPARAM)
    ptr = HOOKPROC(unified)
    start_keyboard_hook._ref = ptr  # 保活防 GC
    start_keyboard_hook._enum_ref = EnumProc

    wt2 = threading.Thread(target=watcher_loop, name="subfix-winwatch", daemon=True)
    wt2.start()

    def loop():
        # WH_KEYBOARD_LL 只回调安装它的线程，故在本线程安装并泵消息；
        # hMod 必须传 NULL（传 EXE 句柄本机报 error 126）。
        hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, ptr, None, 0)
        if not hook:
            hook_dead["v"] = True
            trace("hook 安装失败 err=%d" % ctypes.get_last_error())
            return
        trace("hook 已安装 WH_KEYBOARD_LL")
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            pass

    t = threading.Thread(target=loop, name="subfix-kbd", daemon=True)
    t.start()
    return hook_dead


# ---------------- 一次性读取子进程管理 ----------------
class ReadCache(object):
    """后台串行拉起 --oneshot 子进程；播放中卡住就杀，主进程永不阻塞。

    每次读取落地一个事件（read_event_seq 递增）：
      kind="fast"：在超时前返回（暂停/跳转态），带真实帧；
      kind="slow"：被超时杀掉（播放期 GetCurrentTimecode 阻塞的铁证）。
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.proc = None
        self.started = 0.0
        self.real_rel = None
        self.real_tc = ""
        self.real_wall = None
        self.fps = 25.0
        self.timeline = None
        self.error = "启动中…"
        self.ok = False
        self.last_finish = 0.0
        self.read_event_seq = 0
        self.last_kind = ""
        self.last_dur = None
        self.last_finish_wall = None

    def spawn(self):
        with self.lock:
            if self.proc is not None:
                return
            creationflags = 0x08000000 if sys.platform.startswith("win") else 0
            try:
                self.proc = subprocess.Popen(
                    [sys.executable, os.path.abspath(__file__), "--oneshot"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    creationflags=creationflags)
                self.started = time.perf_counter()
            except OSError as exc:
                self.proc = None
                self.error = "拉起读取子进程失败: %s" % exc

    def poll(self, timeout=1.2):
        with self.lock:
            proc = self.proc
            if proc is None:
                return
            rc = proc.poll()
            if rc is None:
                if time.perf_counter() - self.started > timeout:
                    # 播放中 Resolve 不响应是常态；杀掉，记 slow 事件（播放强证据）
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    self.proc = None
                    self.error = "读取超时（播放中属正常）"
                    self.last_kind = "slow"
                    self.last_dur = time.perf_counter() - self.started
                    self.last_finish_wall = time.perf_counter()
                    self.read_event_seq += 1
                return
            try:
                out = proc.stdout.read().decode("utf-8", "replace").strip()
            except Exception:  # noqa: BLE001
                out = ""
            finish_wall = time.perf_counter()
            self.proc = None
            self.last_finish = finish_wall
            self.last_kind = "fast"
            self.last_dur = finish_wall - self.started
            self.last_finish_wall = finish_wall
            self.read_event_seq += 1
            self._consume(out, rc)

    def _consume(self, out, rc):
        fields = {}
        for token in out.replace("\n", " ").split():
            if "=" in token:
                k, v = token.split("=", 1)
                fields[k] = v
        if rc == 0 and "rel" in fields:
            try:
                self.real_rel = int(fields["rel"])
                self.real_tc = fields.get("tc", "")
                self.fps = float(fields.get("fps", self.fps))
                name = fields.get("name", "")
                start = fields.get("start", "0")
                self.timeline = "%s|%s" % (name, start)
                self.real_wall = time.perf_counter()
                self.ok = True
                self.error = ""
            except (ValueError, TypeError):
                self.ok = False
                self.error = "读取输出解析失败: %s" % out
        else:
            self.ok = False
            self.error = fields.get("ERR", out or ("退出码 %s" % rc))

    def kill_current(self):
        with self.lock:
            if self.proc is not None:
                try:
                    self.proc.kill()
                except OSError:
                    pass
                self.proc = None

    def snapshot(self):
        with self.lock:
            return (self.real_rel, self.real_tc, self.real_wall, self.fps,
                    self.timeline, self.ok, self.error,
                    self.read_event_seq, self.last_kind, self.last_dur,
                    self.last_finish_wall)


def _process_exe_path(pid):
    """返回指定 PID 的可执行文件全路径（小写）；进程不存在/无权限返回 None。"""
    if not sys.platform.startswith("win"):
        return None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(260)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return (buf.value or "").lower()
    finally:
        kernel32.CloseHandle(handle)
    return None


def takeover_existing_instance(pid_path, self_pid, trace):
    """单实例接管：若 PID 文件指向另一个存活的 python 轮询进程，强杀它。

    防止“强制退出/崩溃残留 + 新一轮启动”造成两个进程同时写同一个输出文件
    （实测会让 Lua 读到的播放/暂停状态随机回退）。
    """
    try:
        with open(pid_path, "r", encoding="utf-8") as handle:
            old_pid = int((handle.read() or "0").strip() or 0)
    except (OSError, ValueError):
        old_pid = 0
    if not old_pid or old_pid == self_pid:
        return
    exe = _process_exe_path(old_pid)
    if not exe or not exe.endswith("python.exe"):
        return
    trace("发现旧轮询实例 pid=%d (%s)，执行接管强杀" % (old_pid, exe))
    try:
        # /T 连同旧轮询拉起的 --oneshot 读取子进程一起结束，避免孤儿 python 继续
        # 占用 Resolve 并写状态文件（旧实现只杀父进程，在途读取子进程会残留）。
        subprocess.Popen(["taskkill", "/F", "/T", "/PID", str(old_pid)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         creationflags=0x08000000)
        time.sleep(0.4)
    except OSError as exc:
        trace("接管强杀失败: %r" % exc)


# ---------------- 播放状态机（纯逻辑，时钟由参数注入，便于单测）----------------
# 键盘按下空格后，Resolve 真正开始阻塞 GetCurrentTimecode 有几百 ms 延迟，且暂停期
# 已在途的那次读取会在按键后立刻“快速返回旧帧”。因此按键播放入射后给一个宽限期，
# 期内的 fast 读取一律不能作为暂停/反证证据。
KEY_PLAY_GRACE = 1.0
# 宽限期后，需要连续两次“帧几乎不动”的快速读取才判定空格被输入框吞掉（反证）。
CONTRADICT_NEED_STILL = 2


class FollowFSM(object):
    """输入：一次性读取事件（fast/slow）+ 键盘档位；输出：是否播放/档位/推算锚点。

    副作用通过返回动作告知调用方：
      None           无
      "kill_reread"  杀掉在途读取子进程并立即重读（暂停/反证瞬间尽快回贴真实帧）
    对键盘档位的 reset_level 由调用方在收到 note=contradiction / heur-space-pause
    时执行。
    """

    def __init__(self):
        self.eff_playing = False
        self.eff_level = 0
        self.anchor_rel = None
        self.anchor_wall = None
        self.last_fast_real = None
        self.last_fast_wall = None
        self.blocked = 0
        self.prev_key_level = None
        self.key_arm_wall = None     # 最近一次“键盘开始播放”的墙上时刻
        self.fast_still = 0          # 宽限期后连续“帧不动”的快速读取次数

    def _arm_heuristic(self, resume):
        if self.last_fast_real is None or self.last_fast_wall is None:
            return False
        level = resume if resume in (1, -1) else 1
        self.eff_playing = True
        self.eff_level = level
        self.anchor_rel = self.last_fast_real
        self.anchor_wall = self.last_fast_wall
        return True

    def _pause(self):
        self.eff_playing = False
        self.eff_level = 0
        self.anchor_rel = None
        self.anchor_wall = None

    def read_event(self, kind, real_rel, wall, key_level, resume):
        """处理一次读取落地。返回 (action, note)。"""
        if kind == "fast":
            self.blocked = 0
            if real_rel is None or wall is None:
                return None, "fast-nodata"

            # 键盘声称播放中：刚按键的宽限期内，在途读取是暂停期发出的、返回的是旧帧，
            # 既不能判暂停也不能反证；只记录，不动播放状态。
            in_grace = (self.eff_playing and key_level != 0
                        and self.key_arm_wall is not None
                        and wall - self.key_arm_wall < KEY_PLAY_GRACE)
            if in_grace:
                self.last_fast_real = real_rel
                self.last_fast_wall = wall
                return None, "fast-grace"

            contradiction = False
            if self.eff_playing and key_level != 0:
                moved = (self.last_fast_real is None
                         or self.last_fast_wall is None
                         or wall - self.last_fast_wall <= 0.15
                         or abs(real_rel - self.last_fast_real) >= 2)
                self.fast_still = 0 if moved else self.fast_still + 1
                if self.fast_still >= CONTRADICT_NEED_STILL:
                    contradiction = True

            if contradiction:
                # 宽限期后连续多次快速读取帧不动：空格被输入框/树吞掉，Resolve 没播
                self._pause()
                self.prev_key_level = 0
                self.fast_still = 0
                self.last_fast_real = real_rel
                self.last_fast_wall = wall
                self.anchor_rel = real_rel
                self.anchor_wall = wall
                return "kill_reread", "contradiction"

            if self.eff_playing and key_level != 0 and self.fast_still == 1:
                # 键盘声称播放、宽限期后第一次帧不动：可能只是读取竞态，先立刻再读一次
                # 确认（不翻状态、不动键盘档位）；仍不动下一轮才走 contradiction。
                return "kill_reread", "fast-watch"

            if self.eff_playing:
                # 播放中读取必然阻塞；宽限期外能快速返回 = 实际已暂停/跳转。
                # 同步把键盘档位归零（由调用方 reset_transport），避免与 Resolve 失步。
                self._pause()
                self.prev_key_level = 0
                self.fast_still = 0
                self.last_fast_real = real_rel
                self.last_fast_wall = wall
                self.anchor_rel = real_rel
                self.anchor_wall = wall
                return "kill_reread", "fast-pause"

            self.last_fast_real = real_rel
            self.last_fast_wall = wall
            self.anchor_rel = real_rel
            self.anchor_wall = wall
            return None, "fast"

        # kind == "slow"：GetCurrentTimecode 阻塞被超时杀掉 = 播放强证据
        self.blocked += 1
        self.fast_still = 0
        if not self.eff_playing and key_level == 0:
            if self._arm_heuristic(resume):
                return None, "heur-arm"
        return None, "slow"

    def key_event(self, level, resume, key, real_rel, now):
        """键盘档位/轨迹变化。返回 (action, note)。"""
        # 启发式已判定播放中，用户按空格（Transport 误以为“开始”）→ 实际是暂停
        if self.eff_playing and level != 0 and key.startswith("SP>"):
            self._pause()
            self.prev_key_level = 0
            return "kill_reread", "heur-space-pause"
        if level != self.prev_key_level:
            if level != 0:
                self.eff_playing = True
                self.eff_level = level
                if real_rel is not None:
                    self.anchor_rel = real_rel
                    self.anchor_wall = now
                elif self.last_fast_real is not None:
                    self.anchor_rel = self.last_fast_real
                    self.anchor_wall = self.last_fast_wall or now
                self.key_arm_wall = now
                self.fast_still = 0
                self.prev_key_level = level
                # 立即丢弃暂停期已在途的读取，否则它会在按键后立刻回一帧旧值
                return "kill_reread", "key-play"
            self.prev_key_level = level
            if self.eff_playing and key in ("SP||", "K"):
                self._pause()
                return "kill_reread", "key-pause"
        return None, "key-same"

    def output(self, now, real_rel, key_level, fps):
        """返回 (out_rel, playing, speed, src)。"""
        if self.eff_playing and self.anchor_rel is not None and self.anchor_wall is not None:
            speed = self.eff_level * fps
            return (self.anchor_rel + speed * (now - self.anchor_wall),
                    True, speed, "key" if key_level != 0 else "heur")
        if real_rel is not None:
            return float(real_rel), False, 0.0, "real"
        if self.last_fast_real is not None:
            return float(self.last_fast_real), False, 0.0, "real"
        return None, False, 0.0, "real"


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--oneshot":
        return run_oneshot()
    if len(sys.argv) < 2:
        return 2

    out_path = sys.argv[1]
    interval = 0.05
    if len(sys.argv) >= 3:
        try:
            interval = max(0.02, float(sys.argv[2]) / 1000.0)
        except ValueError:
            pass
    stop_path = out_path + ".stop"
    err_path = out_path + ".err"
    trace_path = out_path + ".trace.txt"
    pid_path = out_path + ".pid"
    alive_path = out_path + ".alive"
    alive_start_wall = time.time()
    alive_check_wall = 0.0
    ALIVE_GRACE = 10.0   # 启动宽限（等 Lua 创建 .alive）
    ALIVE_STALE = 15.0   # Lua 定时器 120ms 一跳；超过 15s 没心跳视为消费者已退出

    # trace 会被 主线程/看门线程/钩子线程 并发写：必须共用一个文件句柄并加锁，
    # 否则各自 open('a') 的 seek-to-end 会互相覆盖（曾观测到整行丢失/撕裂）。
    trace_lock = threading.Lock()
    try:
        trace_fh = open(trace_path, "w", encoding="utf-8", newline="\n")
    except OSError:
        trace_fh = None

    def trace(msg):
        if trace_fh is None:
            return
        line = "%.3f %s\n" % (time.perf_counter(), msg)
        with trace_lock:
            try:
                trace_fh.write(line)
                trace_fh.flush()
            except OSError:
                pass

    time.sleep(0.25)
    try:
        if os.path.exists(stop_path):
            os.remove(stop_path)
    except OSError:
        pass
    for stale in (out_path, err_path):
        try:
            if os.path.exists(stale):
                os.remove(stale)
        except OSError:
            pass

    # 接管可能残留的旧实例（同输出文件的双写者），再写下自己的 PID
    takeover_existing_instance(pid_path, os.getpid(), trace)
    try:
        with open(pid_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(str(os.getpid()))
    except OSError:
        pass

    transport = Transport()
    hook_dead = start_keyboard_hook(transport, trace)
    cache = ReadCache()

    seq = 0
    event_seen = 0
    err_reported = ""
    next_read_attempt = 0.0

    fsm = FollowFSM()
    last_timeline = None
    prev_key = None

    trace("poller 启动 interval=%.3fs python=%s" % (interval, sys.executable))

    try:
        while True:
            if os.path.exists(stop_path):
                try:
                    os.remove(stop_path)
                except OSError:
                    pass
                try:
                    os.remove(pid_path)
                except OSError:
                    pass
                os._exit(0)

            # 消费者看门狗：Lua 跟随定时器每 ~120ms touch 一次 .alive。
            # Lua 被强制退出/崩溃后心跳停止，本进程自杀，避免成为孤儿持续写 out。
            now_wall = time.time()
            if now_wall - alive_check_wall >= 1.0:
                alive_check_wall = now_wall
                try:
                    alive_age = now_wall - os.path.getmtime(alive_path)
                except OSError:
                    alive_age = None
                if alive_age is None:
                    if now_wall - alive_start_wall > ALIVE_GRACE:
                        trace("消费者看门狗: .alive 始终缺失，自杀退出")
                        os._exit(0)
                elif alive_age > ALIVE_STALE:
                    trace("消费者看门狗: .alive 已 %.1fs 未更新（Lua 已退出？），自杀退出"
                          % alive_age)
                    try:
                        os.remove(pid_path)
                    except OSError:
                        pass
                    os._exit(0)

            now = time.perf_counter()
            cache.poll(1.2)
            (real_rel, real_tc, real_wall, fps, timeline_fp,
             ok, error, evseq, kind, dur, finish_wall) = cache.snapshot()

            # 读取节奏：播放 1.0s（反正会超时），暂停 0.25s
            read_gap = 1.0 if fsm.eff_playing else 0.25
            if now >= next_read_attempt and cache.proc is None:
                cache.spawn()
                next_read_attempt = now + read_gap

            if timeline_fp != last_timeline:
                last_timeline = timeline_fp
                fsm = FollowFSM()

            # ---- 处理“上一轮没见过”的读取事件（每个事件只处理一次）----
            if evseq != event_seen:
                event_seen = evseq
                kl0, res0, _k0 = transport.snapshot()
                action, note = fsm.read_event(
                    kind, real_rel if kind == "fast" else None,
                    finish_wall, kl0, res0)
                if kind == "slow":
                    trace("读取超时#%d dur=%.2f key_level=%s"
                          % (fsm.blocked, dur or -1, kl0))
                if note == "heur-arm":
                    trace("-> 播放 (阻塞启发式 level=%d anchor=%s)"
                          % (fsm.eff_level, fsm.anchor_rel))
                elif note == "fast-grace":
                    trace("宽限忽略: 按键后旧在途读取 real=%s dur=%.2f（不反证）"
                          % (real_rel, dur or -1))
                elif note == "fast-watch":
                    trace("反证观察: 宽限期后首次帧不动 real=%s，立即复读确认" % real_rel)
                elif note == "fast-pause":
                    trace("-> 暂停 (读取快速返回 real=%s dur=%.2f)"
                          % (real_rel, dur or -1))
                elif note == "contradiction":
                    trace("反证暂停: 键盘声称播放但真实帧不动 (real=%s)" % real_rel)
                if action == "kill_reread":
                    if note in ("contradiction", "fast-pause"):
                        transport.reset_level("RST")
                    cache.kill_current()
                    next_read_attempt = 0.0

            # ---- 键盘权威信号 ----
            key_level, resume, key = transport.snapshot()
            if key != prev_key:
                trace("按键状态: level=%s resume=%s key=%s eff_playing=%s"
                      % (key_level, resume, key, fsm.eff_playing))
                action, note = fsm.key_event(
                    key_level, resume, key, real_rel, time.perf_counter())
                if note == "key-play":
                    trace("-> 播放 (键盘 level=%d)" % key_level)
                elif note == "key-pause":
                    trace("-> 暂停 (键盘 %s)" % key)
                elif note == "heur-space-pause":
                    trace("-> 暂停 (启发式播放中按空格)")
                if action == "kill_reread":
                    if note == "heur-space-pause":
                        transport.reset_level("SP||(heur)")
                    cache.kill_current()
                    next_read_attempt = 0.0
                prev_key = key

            # ---- 输出 ----
            now = time.perf_counter()
            out_rel, playing, speed, src = fsm.output(now, real_rel, key_level, fps)

            if not ok and real_rel is None:
                if error and error != err_reported:
                    err_reported = error
                    write_atomic(err_path, error)
            elif os.path.exists(err_path):
                try:
                    os.remove(err_path)
                except OSError:
                    pass

            seq += 1
            if out_rel is not None:
                write_atomic(out_path,
                             "rel=%d playing=%d speed=%.2f tc=%s real=%s seq=%d src=%s key=%s\n"
                             % (int(round(out_rel)), 1 if playing else 0, speed,
                                real_tc or "",
                                "-" if real_rel is None else str(real_rel),
                                seq, src, key))

            time.sleep(interval)
    except KeyboardInterrupt:
        os._exit(0)


if __name__ == "__main__":
    sys.exit(main())
