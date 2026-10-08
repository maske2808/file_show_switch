#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_show_switch.py - 文件显隐切换（Marvis 风格 UI，复刻网关切换.exe 界面框架）
功能：
  - 一键切换 Windows 11 文件夹选项两项设置（均写入 HKCU，无需管理员权限）：
      1) 隐藏受保护的操作系统文件 -> HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced\\ShowSuperHidden
      2) 隐藏文件和文件夹        -> HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer\\Advanced\\Hidden
    1 = 显示，0 = 隐藏；两个状态分别为「两个都设置为显示」和「两个都设置为隐藏」
  - 切换后按「全局切换版本号 + 每页刷新版本记录」方案刷新：每次写 HKCU
    成功后 global_toggle_seq += 1；每个资源管理器视图记录 last_refresh_seq，
    仅当 last_refresh_seq < global_toggle_seq 才补刷该视图。切换时只刷当前
    可见活动页（F5 实测有效），非活动标签页保持"待刷"状态，由标签切换 /
    最小化还原等可见性事件（FOCUS/NAMECHANGE/SHOW/MINIMIZEEND）通知后再
    按版本号决定是否补刷；切换回已刷新页不产生多余开销。不重启进程、
    失败静默、无任何提示文案）
  - 托盘左键单击直接切换；托盘菜单可手动选择状态、显示当前状态、退出程序
  - UI 仿 Marvis：左侧导航（程序 + 底部设置）+ 右侧内容框，浅色主题，
    自绘无边框标题栏 + 圆角窗口 + 右侧可收起日志栏 + 系统托盘

运行：普通权限运行 python file_show_switch.py（写入 HKCU 无需管理员权限，exe 无 UAC 提权清单）
依赖：仅 Python 标准库 + ctypes；托盘需 pystray / pillow（可选）
"""

import ctypes
import ctypes.wintypes as wt
import os
import queue
import sys
import threading
import time
import tkinter as tk
import winreg

# 系统托盘（可选依赖：pip install pystray pillow）
try:
    import pystray
    from PIL import Image
    try:
        from PIL import ImageTk
    except Exception:
        ImageTk = None
    _HAS_TRAY = True
except Exception:
    _HAS_TRAY = False
    ImageTk = None
    Image = None

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
SINGLE_INSTANCE_MUTEX = r'Global\FileShowSwitch_SingleInstance'
WINDOW_TITLE = '文件显隐切换 - 隐藏文件显示管理'

# 注册表路径与值名（ShowSuperHidden / Hidden 均写入 HKCU，普通权限即可）
REG_ADVANCED_HKCU = r'Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced'
VAL_SHOW_SUPER_HIDDEN = 'ShowSuperHidden'
VAL_HIDDEN = 'Hidden'

# 静默启动参数：带该参数启动时主窗口不显示、仅托盘静默运行
SILENT_FLAGS = ('--silent', '--hidden', '--autostart')
IS_SILENT = any(a.lower() in SILENT_FLAGS for a in sys.argv[1:])

# 日志侧边栏宽度
LOG_PANEL_W = 300


def _round_rect_pts(x0, y0, x1, y1, r):
    """圆角矩形点列（配合 Canvas create_polygon smooth=True 绘制大圆角矩形）。"""
    r = max(1, min(float(r), (x1 - x0) / 2.0, (y1 - y0) / 2.0))
    return [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]


# ---------------------------------------------------------------------------
# 单实例互斥（命名互斥体 Global\\ 前缀 + 激活已有窗口）
# ---------------------------------------------------------------------------
def _acquire_single_instance():
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateMutexW.restype = wt.HANDLE
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
    h = k.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
    if not h:
        return True
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        try:
            k.CloseHandle(h)
        except Exception:
            pass
        return False
    return True


def _activate_existing_window():
    """查找已运行实例的主窗口并激活（还原 + 置前 + 前台）。"""
    try:
        u = ctypes.windll.user32
        u.FindWindowW.restype = wt.HWND
        u.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
        u.EnumWindows.restype = wt.BOOL
        u.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
        u.GetWindowTextLengthW.restype = ctypes.c_int
        u.GetWindowTextLengthW.argtypes = [wt.HWND]
        u.GetWindowTextW.restype = ctypes.c_int
        u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        u.IsWindow.restype = wt.BOOL
        u.IsWindow.argtypes = [wt.HWND]
        u.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        u.SetForegroundWindow.argtypes = [wt.HWND]
        u.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_uint]
        u.FlashWindow.argtypes = [wt.HWND, wt.BOOL]
        u.keybd_event.argtypes = [wt.BYTE, wt.BYTE, wt.DWORD, ctypes.c_size_t]

        def _title_ok(hwnd):
            n = u.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            return buf.value != WINDOW_TITLE

        _found = [0]

        @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
        def _cb(hwnd, lparam):
            if not _title_ok(hwnd):
                _found[0] = hwnd
                return False
            return True

        hwnd = u.FindWindowW(None, WINDOW_TITLE)
        if not (hwnd and u.IsWindow(hwnd)):
            u.EnumWindows(_cb, 0)
            hwnd = _found[0]
        if hwnd and u.IsWindow(hwnd):
            u.ShowWindow(hwnd, 9)
            u.SetWindowPos(hwnd, -1, 0, 0, 0, 0,
                           0x0001 | 0x0002 | 0x0040)   # NOMOVE|NOSIZE|SHOWWINDOW
            u.SetWindowPos(hwnd, -2, 0, 0, 0, 0,
                           0x0001 | 0x0002)            # NOMOVE|NOSIZE
            u.keybd_event(0x12, 0, 0, 0)     # ALT down
            u.keybd_event(0x12, 0, 2, 0)     # ALT up
            u.SetForegroundWindow(hwnd)
            u.FlashWindow(hwnd, True)
            return True
    except Exception:
        pass
    return False


def make_tray_icon(size=64, variant='show'):
    """托盘图标：按 variant 绘制眼睛图标（填充风格 PIL 绘制）。
    variant='show' -> 绿色睁眼（当前两个都显示）
    variant='hide' -> 红色闭眼带斜杠（当前两个都隐藏）
    variant='mixed' -> 橙色睁眼（当前为混合状态）
    优先加载 app_icon.ico 最大帧，失败回退内置绘制。"""
    if variant not in ('show', 'hide', 'mixed'):
        variant = 'show'
    img = None
    if variant == 'show':
        try:
            p = resource_path('app_icon.ico')
            if os.path.isfile(p):
                im = Image.open(p)
                frames = []
                try:
                    while True:
                        frames.append((im.size[0], im.copy()))
                        im.seek(im.tell() + 1)
                except EOFError:
                    pass
                if frames:
                    frames.sort(key=lambda t: t[0], reverse=True)
                    im = frames[0][1]
                img = im.convert('RGBA')
        except Exception:
            img = None
    if img is None:
        from PIL import Image, ImageDraw
        img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        s = size / 64.0
        if variant == 'show':
            ring = (80, 200, 120, 255)
        elif variant == 'hide':
            ring = (230, 70, 70, 255)
        else:
            ring = (245, 183, 61, 255)
        # 外圆环
        d.ellipse([4 * s, 4 * s, 60 * s, 60 * s], fill=ring,
                  outline=(255, 255, 255, 255), width=int(3 * s))
        # 白色眼白
        d.ellipse([18 * s, 22 * s, 46 * s, 42 * s], fill=(255, 255, 255, 255))
        # 虹膜
        d.ellipse([25 * s, 27 * s, 39 * s, 37 * s], fill=(30, 40, 60, 255))
        # 瞳孔高光
        d.ellipse([29 * s, 30 * s, 33 * s, 34 * s], fill=(255, 255, 255, 255))
        if variant == 'hide':
            # 斜杠（禁止/隐藏语义）
            d.line([10 * s, 10 * s, 54 * s, 54 * s],
                   fill=(255, 255, 255, 255), width=int(6 * s))
    img = img.resize((size, size), Image.LANCZOS)
    return img


# ---------------------------------------------------------------------------
# 开机自启 + 配置持久化
# ---------------------------------------------------------------------------
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_VALUE_NAME = 'FileShowSwitch'


def app_dir():
    """程序所在目录：exe 打包后为 exe 所在目录，py 运行时为脚本目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def autostart_exe_path():
    if getattr(sys, 'frozen', False):
        return sys.executable
    return '%s "%s"' % (sys.executable, os.path.abspath(__file__))


def is_autostart_set():
    """查询开机自启（HKCU Run 键 FileShowSwitch）是否已设置。"""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return True
        finally:
            winreg.CloseKey(key)
    except OSError:
        return False


def set_autostart(enabled):
    """创建/删除开机自启（HKCU Run 键，普通权限即可）。返回 (ok, msg)。"""
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                 winreg.KEY_SET_VALUE)
        try:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ,
                                  autostart_exe_path() + ' --silent')
                msg = '已设置开机自启（HKCU Run 键，登录时静默启动）'
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except OSError:
                    pass
                msg = '已取消开机自启'
        finally:
            winreg.CloseKey(key)
        return True, msg
    except Exception as e:
        return False, '设置开机自启失败：%s' % e


def resource_path(name):
    """打包后从 PyInstaller 解包目录 sys._MEIPASS 取资源；源码运行时取脚本同目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, name)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), name)


# ---------------------------------------------------------------------------
# 注册表读写（HKCU）
# ---------------------------------------------------------------------------
def read_reg_dword(hive, path, name):
    """读取 REG_DWORD 值，失败返回 None。"""
    try:
        key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ)
        try:
            v, t = winreg.QueryValueEx(key, name)
            if t == winreg.REG_DWORD:
                return int(v)
        finally:
            winreg.CloseKey(key)
    except OSError:
        pass
    return None


def write_reg_dword(hive, path, name, value):
    """写入 REG_DWORD 值（HKCU，普通权限即可）。"""
    key = winreg.CreateKeyEx(hive, path, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))
    finally:
        winreg.CloseKey(key)


def get_show_super_hidden():
    """HKCU ShowSuperHidden：1=显示、0=隐藏；读取失败默认 0。"""
    v = read_reg_dword(winreg.HKEY_CURRENT_USER, REG_ADVANCED_HKCU,
                       VAL_SHOW_SUPER_HIDDEN)
    return 1 if v == 1 else 0


def get_hidden():
    """HKCU Hidden：1=显示、0=隐藏；读取失败默认 0。"""
    v = read_reg_dword(winreg.HKEY_CURRENT_USER, REG_ADVANCED_HKCU, VAL_HIDDEN)
    return 1 if v == 1 else 0


def get_states():
    """返回 (show_super_hidden, hidden) 两个当前状态（0/1）。"""
    return get_show_super_hidden(), get_hidden()


def overall_state(sup, hid):
    """总体状态：'show' 两个都显示 / 'hide' 两个都隐藏 / 'mixed' 混合。"""
    if sup == 1 and hid == 1:
        return 'show'
    if sup == 0 and hid == 0:
        return 'hide'
    return 'mixed'


def overall_text(state):
    return {'show': '全部显示', 'hide': '全部隐藏', 'mixed': '混合状态'}.get(state, '未知')


# ---------------------------------------------------------------------------
# 版本号驱动刷新（v13）：全局切换版本号 + 每视图刷新版本记录 + 事件通知补刷。
#
# v13 方案（替代 v12 伪激活方案，伪激活实测不可靠：0x22008a 曾成功 /
# 0xd0c04 失败，非对称，无法保证）：
#   - 每次托盘/按钮切换写 HKCU 成功后，global_toggle_seq += 1（全局单调
#     递增版本号，进程内维护，重启归零；重启后未发生切换则无需刷新任何
#     视图——last_refresh_seq 默认 0，global_toggle_seq 为 0 时恒不触发）。
#   - 每个资源管理器视图（标签页 F5 目标 DirectUIHWND）维护 last_refresh_seq：
#     该视图最后一次成功发送刷新命令时对应的 global_toggle_seq。
#   - 判断规则：切换后只对「当前可见活动页」立即补刷（活动页 F5 实测有效）
#     并更新 last_refresh_seq；非活动标签页不主动刷（伪激活不可靠），保持
#     last_refresh_seq < global_toggle_seq 的"待刷"状态。
#   - 事件只负责「通知可见了」：标签切换（FOCUS/NAMECHANGE/SHOW）、窗口从
#     最小化还原（MINIMIZEEND）触发检查；检查时若该视图
#     last_refresh_seq < global_toggle_seq 则补刷并更新，否则跳过（切换回
#     已刷新页不产生任何多余开销）。
#   - 最小化窗口不刷（最小化时刷新命令/布局不可用，实测需恢复后补刷），
#     保持待刷状态，等 MINIMIZEEND 还原事件补刷。
# ---------------------------------------------------------------------------
REFRESH_WINDOW_MAX = 80      # 单轮最多刷新窗口数保护
_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_VK_F5 = 0x74
_GW_HWNDPREV = 3
_F5_INTERVAL = 0.15         # 各窗口/标签页刷新间隔（避免 explorer 瞬时过载）
_RESTORE_DELAY = 0.9        # 最小化还原后布局就绪延迟（实测约 0.4s 未就绪）
_TABSWITCH_DELAY = 0.12     # 标签切换后重绘延迟（实测事件到达后 0.1s 级即可 F5）
# v16 按需挂摘（10s 硬超时）：切换写 HKCU 成功后挂载完整四事件钩子，
# 钩子最长存活 10 秒；无论窗口是否全部补刷对齐，10 秒后一律摘除全部
# 钩子并清掉挂载期定时器，回到无钩子 0% CPU 状态（无低频降级兜底）。
_HOOK_MAX_LIFETIME = 10.0   # 秒；钩子挂载后的最大存活期，到期一律摘除

# 全局切换版本号（进程内单调递增，每次写 HKCU 成功后 +1）
_global_seq = 0
# 视图刷新版本记录：{f5_target_hwnd: last_refresh_seq}
_view_seq = {}
# 事件补刷防抖：{cabinet_hwnd: True} 表示已有 pending 检查
_pending_checks = {}


def _get_class_name(hwnd):
    """获取窗口类名，失败返回空串。"""
    try:
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
        return buf.value
    except Exception:
        return ''


def _post_message(hwnd, msg, wparam=0, lparam=0):
    """PostMessageW 异步投递消息；返回是否投递成功。"""
    try:
        return bool(ctypes.windll.user32.PostMessageW(
            hwnd, msg, wparam, lparam))
    except Exception:
        return False


def _send_f5(hwnd):
    """向窗口投递 F5（WM_KEYDOWN + WM_KEYUP）。实测仅对当前激活标签页的
    DirectUIHWND 有效。"""
    if not hwnd:
        return
    _post_message(hwnd, _WM_KEYDOWN, _VK_F5, 0)
    _post_message(hwnd, _WM_KEYUP, _VK_F5, 0)


def _find_f5_target(top):
    """递归定位 F5 投递目标：优先第二个 DirectUIHWND（文件列表视图，实测
    刷新以第二个为准；第一个为导航树区域，历史实测也有效但以第二个为准），
    回退第一个 DirectUIHWND / SHELLDLL_DefView；都没有时返回顶层窗口本身
    兜底。"""
    try:
        user32 = ctypes.windll.user32
        wndproc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
        directui = []
        defview = [0]

        @wndproc
        def _cb(hwnd, lparam):
            cls = _get_class_name(hwnd)
            if cls == 'DirectUIHWND':
                directui.append(hwnd)
            elif cls == 'SHELLDLL_DefView' and not defview[0]:
                defview[0] = hwnd
            return True

        user32.EnumChildWindows(top, _cb, 0)
        if len(directui) >= 2:
            return directui[1]
        if directui:
            return directui[0]
        return defview[0] or top
    except Exception:
        return top


def _collect_tabs(cabinet):
    """递归枚举 CabinetWClass 下所有 ShellTabWindowClass（每个标签页一个）。"""
    tabs = []
    try:
        user32 = ctypes.windll.user32
        wndproc = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

        @wndproc
        def _cb(hwnd, lparam):
            if _get_class_name(hwnd) == 'ShellTabWindowClass':
                tabs.append(hwnd)
            return True

        user32.EnumChildWindows(cabinet, _cb, 0)
    except Exception:
        pass
    return tabs


def _find_active_tab(tabs):
    """判定活动标签页：实测活动页的 z-order 前驱（GW_HWNDPREV）不是
    ShellTabWindowClass（各标签页互为前驱，活动页前驱为地址栏等其他子
    窗口）；无前驱（z-order 最顶）同样视为活动页。
    返回 (active_tab, confident)：confident 表示前驱特征唯一可判（恰好一个
    标签页前驱非 ShellTabWindowClass）。实测部分窗口布局（如 此电脑/盘符
    混合标签）下多个标签页前驱均非 ShellTabWindowClass，无法仅凭 z-order
    区分，此时返回 confident=False，调用方应跳过该路径（交由 FOCUS 事件
    携带的精确 DirectUIHWND 目标补刷），避免刷错页并污染刷新记录。"""
    user32 = ctypes.windll.user32
    cands = []
    for t in tabs:
        prev = user32.GetWindow(t, _GW_HWNDPREV)
        if not prev or _get_class_name(prev) != 'ShellTabWindowClass':
            cands.append(t)
    if len(cands) == 1:
        return cands[0], True
    if len(cands) > 1:
        return cands[0], False
    return (tabs[0], False) if tabs else (None, False)


def _is_stale(target):
    """该视图是否需要补刷：最后一次刷新版本 < 全局切换版本号。"""
    return _view_seq.get(target, 0) < _global_seq


def _mark_refreshed(target):
    """视图成功发送刷新命令后，记录当前全局版本号。"""
    _view_seq[target] = _global_seq


def _cab_of(hwnd):
    """沿父链向上定位所属 CabinetWClass 顶层窗口；找不到返回 None。"""
    try:
        user32 = ctypes.windll.user32
        for _ in range(8):
            if not hwnd:
                return None
            if _get_class_name(hwnd) == 'CabinetWClass':
                return hwnd
            hwnd = user32.GetAncestor(hwnd, 1)  # GA_PARENT
    except Exception:
        pass
    return None


def _tab_of(hwnd):
    """沿父链向上定位所属 ShellTabWindowClass（标签页窗口）；找不到返回 None。"""
    try:
        user32 = ctypes.windll.user32
        for _ in range(8):
            if not hwnd:
                return None
            if _get_class_name(hwnd) == 'ShellTabWindowClass':
                return hwnd
            hwnd = user32.GetAncestor(hwnd, 1)  # GA_PARENT
    except Exception:
        pass
    return None


def _is_iconic(hwnd):
    try:
        return bool(ctypes.windll.user32.IsIconic(hwnd))
    except Exception:
        return False


def _refresh_if_stale(cabinet):
    """版本号驱动补刷单个资源管理器窗口（v13）：
    仅对当前活动标签页（唯一 F5 实测有效且当前可见）检查
    last_refresh_seq < global_toggle_seq，是则 F5 并更新记录；非活动标签页
    不主动刷（伪激活实测不可靠），保持待刷状态等待切换事件触发；最小化
    窗口跳过（还原事件再补）。已刷过的页面（last_refresh_seq ==
    global_toggle_seq）不产生任何刷新动作。
    注意：活动页判定若不可信（confident=False，见 _find_active_tab）则跳过
    本路径，避免刷错页并污染 last_refresh_seq 记录；此类窗口由 FOCUS 事件
    携带的精确 DirectUIHWND 目标补刷（_schedule_target）。"""
    try:
        if not cabinet or not ctypes.windll.user32.IsWindow(cabinet):
            return
        if _is_iconic(cabinet):
            return
        tabs = _collect_tabs(cabinet)
        if not tabs:
            # 无标签页（旧版单视图）：直接对视图目标检查补刷
            target = _find_f5_target(cabinet)
            if _is_stale(target):
                _send_f5(target)
                _mark_refreshed(target)
            return
        active, confident = _find_active_tab(tabs)
        if not confident:
            return
        target = _find_f5_target(active)
        if _is_stale(target):
            _send_f5(target)
            _mark_refreshed(target)
    except Exception:
        pass


def _cabinet_hwnds():
    """枚举 CabinetWClass 顶层窗口（所有资源管理器窗口，含最小化）。"""
    user32 = ctypes.windll.user32
    user32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
    user32.FindWindowExW.restype = wt.HWND
    hwnds = []
    hwnd = user32.FindWindowExW(None, None, 'CabinetWClass', None)
    count = 0
    while hwnd and count < REFRESH_WINDOW_MAX:
        hwnds.append(hwnd)
        count += 1
        hwnd = user32.FindWindowExW(None, hwnd, 'CabinetWClass', None)
    return hwnds


def _light_refresh_worker():
    """后台刷新主流程：切换写 HKCU 成功后，遍历全部资源管理器窗口
    （CabinetWClass，含最小化），对每个窗口按版本号驱动补刷活动页
    （_refresh_if_stale 内部再判 IsIconic 与 last_refresh_seq）；
    窗口间串行，失败静默。"""
    try:
        time.sleep(0.3)   # 等 explorer 应用注册表新值后再发 F5
        cabinets = _cabinet_hwnds()
        for cab in cabinets:
            _refresh_if_stale(cab)
            time.sleep(_F5_INTERVAL)
        # v15：活动页批量补刷完成后立即做一次对齐检查（全部对齐则摘钩）
        _maybe_detach_check()
    except Exception:
        pass


def light_refresh_windows():
    """切换后触发后台刷新（不阻塞界面、不重启进程、失败静默）。
    注意：调用前必须已 _global_seq += 1，是否刷新由版本号比较决定。"""
    threading.Thread(target=_light_refresh_worker, daemon=True).start()





# ---------------------------------------------------------------------------
# 可见性变化事件钩子（标签切换 / 最小化还原 / 标签重新显示）
# ---------------------------------------------------------------------------
_EXPLORER_EVENT_HOOKS = []  # 钩子句柄（完整/低频共用），保全局引用防止被 GC
# v16 按需挂摘全局状态（仅在切换写 HKCU 成功后进入"挂载期"）：
_hook_active = False       # 当前是否挂载了任何 WinEventHook（无钩子 = 绝对 0% CPU）
_hook_thread = None        # 钩子线程对象（平时不存在）
_hook_thread_id = None     # 钩子线程 id（摘钩时 PostThreadMessage WM_QUIT 用）
_hook_attached_at = 0.0    # 钩子挂载时刻（10s 硬超时强制摘钩的时间基准）
_hook_lock = threading.Lock()
_lifetime_timer = None     # 挂载期 10s 硬超时定时器（到期一律强制摘钩）
_hook_status_cb = None     # 钩子状态日志回调（main 中绑定到 UI 日志）


def _schedule_check(cabinet, delay=_TABSWITCH_DELAY):
    """事件驱动补刷入口：事件只负责「通知可见了」，刷不刷由版本号决定。
    对同一窗口做防抖（已有 pending 检查则跳过，避免事件风暴重复刷新），
    延迟后执行版本检查补刷。"""
    if not cabinet:
        return
    try:
        if _pending_checks.get(cabinet):
            return
        _pending_checks[cabinet] = True

        def _run():
            try:
                _refresh_if_stale(cabinet)
            finally:
                _pending_checks.pop(cabinet, None)
            # v15：补刷完成后立即做对齐检查（全部对齐则摘钩）
            _maybe_detach_check()

        threading.Timer(delay, _run).start()
    except Exception:
        _pending_checks.pop(cabinet, None)


def _schedule_target(target, delay=_TABSWITCH_DELAY):
    """事件携带精确刷新目标（新活动页 DirectUIHWND）时的补刷入口：
    只检查该目标是否过期（last_refresh_seq < global_toggle_seq），是则 F5
    并更新记录；已刷过则静默跳过。对同一目标防抖。"""
    if not target:
        return
    try:
        key = ('t', target)
        if _pending_checks.get(key):
            return
        _pending_checks[key] = True

        def _run():
            try:
                cab = _cab_of(target)
                if cab and _is_iconic(cab):
                    return
                if _is_stale(target):
                    _send_f5(target)
                    _mark_refreshed(target)
            finally:
                _pending_checks.pop(key, None)
            # v15：补刷完成后立即做对齐检查（全部对齐则摘钩）
            _maybe_detach_check()

        threading.Timer(delay, _run).start()
    except Exception:
        _pending_checks.pop(('t', target), None)


def _hook_thread_main():
    """v16 按需挂摘（10s 硬超时）：钩子线程主循环（平时不存在该线程，
    绝对 0% CPU）。仅由 _ensure_hook_attached 在「切换写 HKCU 成功后」
    按需启动，挂载完整四事件（MINIMIZEEND/SHOW/FOCUS/NAMECHANGE）全局
    钩子，监听窗口活动并补刷未对齐视图；全部对齐由 _maybe_detach_check
    提前摘钩，最迟由 10s 硬超时定时器强制摘钩（PostThreadMessage
    WM_QUIT 退出本循环）。
    回调仅做「通知」：把窗口加入延迟检查队列，由 _refresh_if_stale 依据
    last_refresh_seq < global_toggle_seq 决定是否真正补刷；已刷过的页面
    切换回来不产生任何多余开销。hook 线程阻塞在 GetMessageW 消息循环，
    事件驱动零 CPU。"""
    global _EXPLORER_EVENT_HOOKS, _hook_thread_id, _hook_active
    global _hook_attached_at
    _hook_thread_id = threading.get_ident()
    _my_hooks = []  # 本线程挂载的钩子句柄（收尾只卸自己的，避免误清新线程句柄）
    try:
        user32 = ctypes.windll.user32
        WINEVENTPROC = ctypes.WINFUNCTYPE(
            None, ctypes.c_void_p, wt.DWORD, wt.HWND, ctypes.c_long,
            ctypes.c_long, wt.DWORD, wt.DWORD)
        EVENT_SYSTEM_MINIMIZEEND = 0x0017
        EVENT_OBJECT_SHOW = 0x8002
        EVENT_OBJECT_FOCUS = 0x8005
        EVENT_OBJECT_NAMECHANGE = 0x800C
        OBJID_WINDOW = -4
        WINEVENT_OUTOFCONTEXT = 0x0000
        WINEVENT_SKIPOWNPROCESS = 0x0002

        @WINEVENTPROC
        def _hook_proc(hook, event, hwnd, id_obj, id_child, dw_thread, dw_time):
            if not hwnd:
                return
            try:
                if event == EVENT_SYSTEM_MINIMIZEEND:
                    cls = _get_class_name(hwnd)
                    if cls not in ('CabinetWClass', 'ShellTabWindowClass'):
                        return
                    cab = hwnd if cls == 'CabinetWClass' else _cab_of(hwnd)
                    # 还原后布局就绪慢（实测约 0.4s 未就绪），延迟稍长再补刷
                    _schedule_check(cab, _RESTORE_DELAY)
                    return
                if event == EVENT_OBJECT_NAMECHANGE:
                    # 切换标签时 CabinetWClass 触发（实测 idObj=0，不限
                    # OBJID_WINDOW；部分布局 idObj=-4）。事件只通知可见，
                    # 由版本号决定是否补刷；活动页判定不可信时跳过。
                    if _get_class_name(hwnd) != 'CabinetWClass':
                        return
                    _schedule_check(hwnd, _TABSWITCH_DELAY)
                    return
                if event == EVENT_OBJECT_FOCUS:
                    # 切换标签时新活动页视图 DirectUIHWND 触发（实测有效）。
                    # 事件携带的 hwnd 即新活动页视图，直接以其所属标签页的
                    # F5 目标为准补刷，绕开 z-order 活动页判定（部分布局
                    # 判定不可靠）。
                    cls = _get_class_name(hwnd)
                    if cls == 'DirectUIHWND':
                        tab = _tab_of(hwnd)
                        target = _find_f5_target(tab) if tab else hwnd
                        _schedule_target(target, _TABSWITCH_DELAY)
                        return
                    if cls == 'ShellTabWindowClass':
                        cab = _cab_of(hwnd)
                        if cab:
                            _schedule_check(cab, _TABSWITCH_DELAY)
                        return
                    return
                if event == EVENT_OBJECT_SHOW:
                    cls = _get_class_name(hwnd)
                    if cls == 'ShellTabWindowClass':
                        cab = _cab_of(hwnd)
                        _schedule_check(cab, _TABSWITCH_DELAY)
                    elif cls == 'CabinetWClass':
                        _schedule_check(hwnd, _TABSWITCH_DELAY)
                    return
            except Exception:
                return

        user32.SetWinEventHook.argtypes = [wt.DWORD, wt.DWORD,
                                           ctypes.c_void_p,
                                           WINEVENTPROC, wt.DWORD, wt.DWORD,
                                           wt.DWORD]
        user32.SetWinEventHook.restype = ctypes.c_void_p
        # v14：拆大区间(0x0017~0x800C)为四次单事件注册（eventMin==eventMax），
        # 避免 WINEVENT_OUTOFCONTEXT 把区间内无关高频事件（CREATE/DESTROY/
        # LOCATIONCHANGE 等）全部跨进程投递到钩子线程，消除静置 CPU 回升。
        # 回调逻辑与 v13 完全一致，仅钩子注册粒度收窄。
        _EXPLORER_EVENT_HOOKS[:] = []
        # 完整四事件：全局监听（切换后活动窗口的标签切换/还原/显示）
        for _ev in (EVENT_SYSTEM_MINIMIZEEND, EVENT_OBJECT_SHOW,
                    EVENT_OBJECT_FOCUS, EVENT_OBJECT_NAMECHANGE):
            _h = user32.SetWinEventHook(_ev, _ev, None, _hook_proc, 0, 0,
                                        WINEVENT_OUTOFCONTEXT
                                        | WINEVENT_SKIPOWNPROCESS)
            if _h:
                _EXPLORER_EVENT_HOOKS.append(_h)  # 保持引用防止被垃圾回收
                _my_hooks.append(_h)
        if not _my_hooks:
            _hook_active = False
            _hook_thread_id = None
            return
        _hook_active = True
        _hook_attached_at = time.time()
        _notify_hook_status('钩子已挂载：完整四事件（全局）')
        # v16：挂载完成后立即做一次对齐检查——若切换时所有窗口已补刷对齐
        # （例如仅单窗口单标签且活动页已刷完），无需等 10s 超时即可提前摘钩
        _maybe_detach_check()
        user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG),
                                       wt.HWND, wt.UINT, wt.UINT]
        user32.GetMessageW.restype = ctypes.c_long
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        # GetMessageW 返回 0（收到 WM_QUIT）：摘钩退出。
        # 只卸载本线程的钩子句柄；全局列表清空由摘钩/降级/升级路径负责，
        # 避免与并存的新线程（升级/降级场景）发生句柄竞态。
        for _h in _my_hooks:
            user32.UnhookWinEvent(_h)
        _notify_hook_status('钩子线程已退出')
    except Exception:
        try:
            for _h in _my_hooks:
                user32.UnhookWinEvent(_h)
        except Exception:
            pass




def _notify_hook_status(msg):
    """钩子挂载/摘除/降级状态通知（绑定到 UI 日志，供验证钩子生命周期）。"""
    cb = _hook_status_cb
    if cb:
        try:
            cb(msg)
        except Exception:
            pass


def _ensure_hook_attached():
    """切换写 HKCU 成功后调用：按需启动完整四事件钩子线程（幂等）。
    平时无钩子、无钩子线程；仅当本次切换写 HKCU 成功后进入挂载期，
    钩子最长存活 _HOOK_MAX_LIFETIME（10s），到期一律摘除回到无钩子态。
    """
    global _hook_thread
    with _hook_lock:
        if _hook_thread is not None and _hook_thread.is_alive():
            if not _hook_active:
                # 摘钩后线程尚未完全退出：等待自然退出，避免并发双线程
                _hook_thread.join(timeout=2.0)
                if _hook_thread.is_alive():
                    return  # 极端情况放弃本次挂载（不影响功能，下轮事件驱动兜底）
        _hook_thread = threading.Thread(
            target=_hook_thread_main, daemon=True)
        _hook_thread.start()
        # 启动 10s 硬超时定时器：到期一律强制摘除全部钩子
        _ensure_lifetime_timer_locked()


def _all_views_aligned():
    """返回 (aligned, unaligned)：所有已知资源管理器窗口的每页 F5 目标是否
    均已对齐（_view_seq[target] >= _global_seq）。无任何资源管理器窗口时
    视为全部对齐（无需补刷）。unaligned 为 [(cabinet_hwnd, target), ...]。"""
    cabs = _cabinet_hwnds()
    if not cabs:
        return True, []
    unaligned = []
    for cab in cabs:
        tabs = _collect_tabs(cab)
        targets = []
        if tabs:
            for t in tabs:
                if t:
                    targets.append(_find_f5_target(t))
        else:
            targets.append(_find_f5_target(cab))
        for t in targets:
            if t and _is_stale(t):
                unaligned.append((cab, t))
    return (not unaligned), unaligned


def _maybe_detach_check():
    """v16 摘钩检查（每次事件补刷完成后 / 批量补刷完成后 / 10s 硬超时定时
    触发时调用，幂等；任意线程可调）：
      - 全部视图对齐 -> 提前摘除全部钩子（PostThreadMessage WM_QUIT 结束
        钩子线程，取消 10s 定时器，回到无钩子态）
      - 未对齐但挂载已超 _HOOK_MAX_LIFETIME（10s）-> 一律强制摘除（不再
        做低频降级兜底，窗口后续由用户操作自然刷新）
    无钩子时直接返回（不创建任何定时器）。"""
    global _lifetime_timer
    with _hook_lock:
        if not _hook_active:
            _cancel_lifetime_timer_locked()
            return
        try:
            aligned, _unaligned = _all_views_aligned()
        except Exception:
            return
        if aligned:
            # 全部对齐：提前摘钩退出
            user32 = ctypes.windll.user32
            tid = _hook_thread_id
            for _h in _EXPLORER_EVENT_HOOKS:
                user32.UnhookWinEvent(_h)
            _EXPLORER_EVENT_HOOKS[:] = []
            _hook_active = False
            _cancel_lifetime_timer_locked()
            if tid:
                try:
                    user32.PostThreadMessageW(tid, 0x0012, 0, 0)  # WM_QUIT
                except Exception:
                    pass
            _notify_hook_status('全部窗口视图已对齐，钩子已摘除')
            return
        # 未对齐：10s 硬超时 -> 一律强制摘除（无低频降级兜底）
        if (time.time() - _hook_attached_at) > _HOOK_MAX_LIFETIME:
            user32 = ctypes.windll.user32
            tid = _hook_thread_id
            for _h in _EXPLORER_EVENT_HOOKS:
                user32.UnhookWinEvent(_h)
            _EXPLORER_EVENT_HOOKS[:] = []
            _hook_active = False
            _cancel_lifetime_timer_locked()
            if tid:
                try:
                    user32.PostThreadMessageW(tid, 0x0012, 0, 0)  # WM_QUIT
                except Exception:
                    pass
            _notify_hook_status('钩子存活期已到（10s），强制摘除全部钩子')
            return
        # 未超时且未对齐：保持挂载，等 10s 定时器到期强制摘除


def _ensure_lifetime_timer():
    """启动 10s 硬超时定时器（仅钩子挂载期间允许存活）。"""
    with _hook_lock:
        if _hook_active:
            _ensure_lifetime_timer_locked()


def _ensure_lifetime_timer_locked():
    """前提：已持有 _hook_lock。无定时器时创建；10s 后执行摘钩检查，
    未对齐一律强制摘除（摘钩路径会取消）。"""
    global _lifetime_timer
    if _lifetime_timer is not None:
        return

    def _tick():
        global _lifetime_timer
        _lifetime_timer = None
        _maybe_detach_check()

    _lifetime_timer = threading.Timer(_HOOK_MAX_LIFETIME, _tick)
    _lifetime_timer.daemon = True
    _lifetime_timer.start()


def _cancel_lifetime_timer_locked():
    """前提：已持有 _hook_lock。取消 10s 定时器（无钩子时不允许定时器存活）。"""
    global _lifetime_timer
    if _lifetime_timer is not None:
        try:
            _lifetime_timer.cancel()
        except Exception:
            pass
        _lifetime_timer = None


def _cancel_lifetime_timer():
    with _hook_lock:
        _cancel_lifetime_timer_locked()


def set_states(sup, hid):
    """写入两项设置（均写 HKCU）。sup/hid 为 0/1。返回 (ok, msg)。
    写成功后 global_toggle_seq += 1（全局切换版本号单调递增），再触发
    版本号驱动刷新：只有 last_refresh_seq < global_toggle_seq 的当前可见
    视图会被补刷；非活动标签页保持待刷状态，由切换事件通知后补刷。"""
    global _global_seq
    try:
        if sup is not None:
            write_reg_dword(winreg.HKEY_CURRENT_USER, REG_ADVANCED_HKCU,
                            VAL_SHOW_SUPER_HIDDEN, sup)
        if hid is not None:
            write_reg_dword(winreg.HKEY_CURRENT_USER, REG_ADVANCED_HKCU,
                            VAL_HIDDEN, hid)
        _global_seq += 1
        light_refresh_windows()
        # v15：写 HKCU 成功后按需挂载完整钩子（平时无钩子，绝对 0% CPU）
        _ensure_hook_attached()
        return True, '设置已写入 HKCU'
    except Exception as e:
        return False, '写入注册表失败：%s' % e


class MarvisUI:
    BG_TOP = (0xfe, 0xfe, 0xfe)     # 极浅灰白渐变顶部
    BG_BOT = (0xf7, 0xf7, 0xf7)     # 极浅灰白渐变底部
    CARD_BG = '#fbfbfb'
    CARD_BORDER = '#efefef'
    NAV_ACTIVE = '#f0f0f0'
    NAV_INACTIVE = '#fbfbfb'
    NAV_TEXT_ACTIVE = '#333333'
    NAV_TEXT = '#333333'
    TEXT_MAIN = '#333333'
    TEXT_DIM = '#888888'
    ACCENT = '#35d07f'
    WARN = '#f5b73d'
    DANGER = '#ff5d5d'
    FONT = 'Microsoft YaHei UI'
    # 左侧导航图标（Font Awesome 6 Free 字体：fa-eye U+F06E / fa-cog U+F013）
    NAV_ICONS = {'程序': '\uf06e', '设置': '\uf013'}

    def _apply_window_icon(self):
        """统一窗口/任务栏图标：与 exe 嵌入图标一致。"""
        try:
            u32 = ctypes.windll.user32
            u32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                       ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
            u32.LoadImageW.restype = ctypes.c_void_p
            u32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            u32.GetAncestor.restype = ctypes.c_void_p
            u32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                         ctypes.c_void_p, ctypes.c_void_p]
            u32.SendMessageW.restype = ctypes.c_void_p
            icon_path = resource_path('app_icon.ico')
            hbig = u32.LoadImageW(None, icon_path, 1, 32, 32, 0x10)   # IMAGE_ICON + LR_LOADFROMFILE
            hsmall = u32.LoadImageW(None, icon_path, 1, 16, 16, 0x10)
            hwnd = self.root.winfo_id()
            if hwnd:
                hwnd = u32.GetAncestor(hwnd, 2)  # GA_ROOT=2 取顶层窗口
            if hwnd:
                if hbig:
                    u32.SendMessageW(hwnd, 0x0080, 1, hbig)     # WM_SETICON ICON_BIG
                if hsmall:
                    u32.SendMessageW(hwnd, 0x0080, 0, hsmall)   # WM_SETICON ICON_SMALL
        except Exception:
            pass

    def __init__(self, root):
        self.root = root
        self._apply_window_icon()
        self.q = queue.Queue()

        self._dragging = False
        self._bg_dirty = False
        self._drag_sync_pending = False
        self._nav_anim_running = False
        self._nav_anim_items = {}
        # 最新状态缓存
        self._sup = 0
        self._hid = 0
        self._bg_cache = {}
        self._bg_photo = None

        self.current_page = '程序'
        self.tray = None
        self._tray_click_ts = 0
        self._tray_click_after = None
        self._tray_icon_variant = None
        self._switch_running = False
        self._build()
        self._apply_window_icon()
        # 事件驱动队列：绑定自定义事件回调；启动时主动消费一次兜底，
        # 避免启动早期 put 在绑定完成前丢失。
        self.root.bind('<<QueuePoll>>',
                       lambda e: self._poll_queue(), add='+')
        self.root.after_idle(self._poll_queue)
        self._apply_startup_settings()

    # ---------------- 基础绘制 ----------------
    def _rounded(self, c, x1, y1, x2, y2, r, fill='', outline='', width=1, tags=None):
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
               x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
               x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return c.create_polygon(pts, smooth=True, fill=fill, outline=outline,
                                width=width, tags=tags)

    def _draw_gradient(self, canvas, w, h):
        if self._dragging or self._drag_sync_pending:
            self._bg_dirty = True
            return
        if w < 2 or h < 2:
            return
        if ImageTk is not None:
            key = (w, h)
            photo = self._bg_cache.get(key)
            if photo is None:
                try:
                    top = self.BG_TOP
                    bot = self.BG_BOT
                    n = max(h - 1, 1)
                    grad = Image.new('RGB', (1, h))
                    for y in range(h):
                        t = y / n
                        r = int(top[0] + (bot[0] - top[0]) * t)
                        g = int(top[1] + (bot[1] - top[1]) * t)
                        b = int(top[2] + (bot[2] - top[2]) * t)
                        grad.putpixel((0, y), (r, g, b))
                    img = grad.resize((w, h), Image.NEAREST)
                    photo = ImageTk.PhotoImage(img)
                    if len(self._bg_cache) > 3:
                        self._bg_cache.clear()
                    self._bg_cache[key] = photo
                    self._bg_photo = photo
                except Exception:
                    photo = None
            if photo is not None:
                canvas.delete('bg')
                canvas.create_image(0, 0, anchor='nw', image=photo, tags='bg')
                canvas.tag_lower('bg')
                self._bg_dirty = False
                return
        canvas.delete('bg')
        steps = max(h, 2)
        for i in range(steps):
            t = i / (steps - 1)
            r = int(self.BG_TOP[0] + (self.BG_BOT[0] - self.BG_TOP[0]) * t)
            g = int(self.BG_TOP[1] + (self.BG_BOT[1] - self.BG_TOP[1]) * t)
            b = int(self.BG_TOP[2] + (self.BG_BOT[2] - self.BG_TOP[2]) * t)
            col = '#%02x%02x%02x' % (r, g, b)
            canvas.create_line(0, i, w, i, fill=col, tags='bg')
        self._bg_dirty = False

    # ---------------- 构建 ----------------
    def _build(self):
        self.root.title(WINDOW_TITLE)
        self.root.overrideredirect(False)
        self.root.geometry('1000x500')
        self.root.minsize(940, 480)
        self.root.configure(bg='#f7f7f7')
        self._strip_chrome()
        self._last_rgn_size = (0, 0)
        self.root.bind('<Configure>', self._on_root_configure)

        self.bg = tk.Canvas(self.root, highlightthickness=0, bd=0)
        self.bg.place(x=0, y=0, relwidth=1, relheight=1)
        self.bg.bind('<Configure>', lambda e: self._draw_gradient(self.bg, e.width, e.height))

        self._build_titlebar()

        self.nav = tk.Canvas(self.root, width=196, highlightthickness=0, bd=0,
                             bg='#fbfbfb')
        self.nav.place(x=0, y=0, relheight=1, height=0)
        self._build_nav()

        self.content = tk.Frame(self.root, bg='#f7f7f7')
        self.content.place(x=196, y=0, relwidth=1, relheight=1,
                           width=-196, height=0)

        self.pages = {}
        self.pages['程序'] = self._build_page_program(self.content)
        self.pages['设置'] = self._build_page_settings(self.content)
        self.pages['关于'] = self._build_page_about(self.content)
        for p in self.pages.values():
            p.place(relwidth=1, relheight=1)

        self.titlebar_left.lift()
        self.titlebar_mid.lift()
        self.titlebar_right.lift()

        self._switch('程序')
        self.root.after(200, self._ensure_taskbar)

    # ---------------- 自绘标题栏 ----------------
    TITLEBAR_H = 36
    ROUND_RADIUS = 16

    def _hwnd(self):
        try:
            return ctypes.windll.user32.GetParent(self.root.winfo_id())
        except Exception:
            return None

    def _build_titlebar(self):
        tb_left = tk.Frame(self.root, bg=self.NAV_INACTIVE,
                           height=self.TITLEBAR_H, highlightthickness=0, bd=0)
        tb_left.place(x=0, y=0, width=196, height=self.TITLEBAR_H)
        self.titlebar_left = tb_left
        tb_right = tk.Frame(self.root, bg='#f7f7f7',
                            height=self.TITLEBAR_H, highlightthickness=0, bd=0)
        tb_right.place(relx=1.0, x=-10, y=0, width=118,
                       height=self.TITLEBAR_H, anchor='ne')
        self.titlebar_right = tb_right
        tb_mid = tk.Frame(self.root, bg='#f7f7f7',
                          height=self.TITLEBAR_H, highlightthickness=0, bd=0)
        tb_mid.place(x=196, y=0, relwidth=1, width=-324,
                     height=self.TITLEBAR_H)
        self.titlebar_mid = tb_mid
        self._is_maximized = False
        self._prev_rect = None
        self._drag_off = None
        self._last_press_at = None
        self._dbl_pending = False
        self._press_x = 0
        self._press_y = 0
        self._press_hwnd = None
        self._drag_hold = False
        self._poll_active = False

        self._tb_btn_close = self._make_tb_btn(tb_right, '\u2715', self._tb_close,
                                               hover_close=True)
        self._tb_btn_close.pack(side='right', fill='y')
        self._tb_btn_max = self._make_tb_btn(tb_right, '\u25a1', self._tb_max)
        self._tb_btn_max.pack(side='right', fill='y')
        self._tb_btn_min = self._make_tb_btn(tb_right, '\u2014', self._tb_min)
        self._tb_btn_min.pack(side='right', fill='y')

        for tb in (tb_left, tb_mid, tb_right):
            tb.bind('<Button-1>', self._tb_drag_start)
            tb.bind('<B1-Motion>', self._tb_drag_move)
            tb.bind('<ButtonRelease-1>', self._tb_drag_end)
            tb.bind('<Double-Button-1>', lambda e: self._tb_toggle_max())
            for w in tb.winfo_children():
                if isinstance(w, tk.Label) and w not in (self._tb_btn_close,
                                                         self._tb_btn_max,
                                                         self._tb_btn_min):
                    w.bind('<Button-1>', self._tb_drag_start)
                    w.bind('<B1-Motion>', self._tb_drag_move)
                    w.bind('<ButtonRelease-1>', self._tb_drag_end)
                    w.bind('<Double-Button-1>', lambda e: self._tb_toggle_max())

        hwnd = self._hwnd()
        if hwnd:
            try:
                ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
                ctypes.windll.user32.SetWindowLongW(hwnd, -20,
                                                    ex | 0x40000)
            except Exception:
                pass

    def _strip_chrome(self):
        try:
            self.root.update_idletasks()
            hwnd = self._hwnd()
            if not hwnd:
                return
            st = ctypes.windll.user32.GetWindowLongW(hwnd, -16)
            st &= ~(0x00C00000 | 0x00080000 | 0x00010000 | 0x00040000)
            st |= 0x02000000 | 0x04000000
            ctypes.windll.user32.SetWindowLongW(hwnd, -16, st)
            ex = ctypes.windll.user32.GetWindowLongW(hwnd, -20)
            ex |= 0x40000
            ex &= ~0x80
            ctypes.windll.user32.SetWindowLongW(hwnd, -20, ex)
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, 0, 0, 0, 0,
                0x0001 | 0x0002 | 0x0004 | 0x0020)
            self._apply_round_region(self.ROUND_RADIUS)
        except Exception:
            pass

    def _apply_round_region(self, radius):
        try:
            hwnd = self._hwnd()
            if not hwnd:
                return
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            if w <= 0 or h <= 0:
                return
            self._last_rgn_size = (w, h)
            pref = 1 if radius <= 0 else 2
            try:
                ret = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    ctypes.c_void_p(hwnd), 33,
                    ctypes.byref(ctypes.c_int(pref)), 4)
                if ret == 0:
                    ctypes.windll.user32.SetWindowRgn(hwnd, None, True)
                    return
            except Exception:
                pass
            if radius <= 0:
                ctypes.windll.user32.SetWindowRgn(hwnd, None, True)
                return
            r = int(radius)
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(
                0, 0, w + 1, h + 1, r * 2, r * 2)
            if not rgn:
                return
            if not ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True):
                ctypes.windll.gdi32.DeleteObject(rgn)
        except Exception:
            pass

    def _on_root_configure(self, e):
        try:
            if e.widget is not self.root:
                return
            if self._dragging:
                return
            if (e.width, e.height) == self._last_rgn_size:
                return
            self._apply_round_region(0 if self._is_maximized else self.ROUND_RADIUS)
        except Exception:
            pass

    def _ensure_taskbar(self):
        self._strip_chrome()

    def _make_tb_btn(self, tb, text, cmd, hover_close=False):
        btn = tk.Label(tb, text=text, bg=tb['bg'], fg='#888888',
                       font=('Segoe UI Symbol', 11), width=3, cursor='hand2',
                       relief='flat', bd=0, highlightthickness=0)
        btn.bind('<Button-1>', lambda e: cmd())
        if hover_close:
            btn.bind('<Enter>', lambda e: btn.configure(bg='#f9e6e6'))
            btn.bind('<Leave>', lambda e: btn.configure(bg=tb['bg']))
        else:
            btn.bind('<Enter>', lambda e: btn.configure(bg='#f8f8f8'))
            btn.bind('<Leave>', lambda e: btn.configure(bg=tb['bg']))
        return btn

    def _tb_min(self):
        hwnd = self._hwnd()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE

    def _tb_max(self):
        self._tb_toggle_max()

    def _tb_toggle_max(self):
        hwnd = self._hwnd()
        if not hwnd:
            return
        if self._is_maximized:
            if self._prev_rect:
                ctypes.windll.user32.SetWindowPos(
                    hwnd, 0,
                    self._prev_rect[0], self._prev_rect[1],
                    self._prev_rect[2] - self._prev_rect[0],
                    self._prev_rect[3] - self._prev_rect[1],
                    0x0004)
            self._is_maximized = False
            self._tb_btn_max.config(text='\u25a1')
            self._apply_round_region(self.ROUND_RADIUS)
        else:
            r = wt.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
            self._prev_rect = (r.left, r.top, r.right, r.bottom)
            class _MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ('cbSize', ctypes.c_ulong),
                    ('rcMonitor', ctypes.wintypes.RECT),
                    ('rcWork', ctypes.wintypes.RECT),
                    ('dwFlags', ctypes.c_ulong),
                ]
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            mon = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)
            if mon and ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
                rc = mi.rcWork
            else:
                sp = wt.RECT()
                ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(sp), 0)
                rc = sp
            ctypes.windll.user32.SetWindowPos(
                hwnd, 0, rc.left, rc.top,
                rc.right - rc.left, rc.bottom - rc.top,
                0x0004)
            self._is_maximized = True
            self._tb_btn_max.config(text='\u2750')
            self._apply_round_region(0)

    def _tb_close(self):
        self.on_close()

    def _tb_drag_start(self, e):
        now = time.time()
        dbl_ms = ctypes.windll.user32.GetDoubleClickTime()
        if self._last_press_at and (now - self._last_press_at) * 1000.0 < dbl_ms:
            self._last_press_at = None
            self._dbl_pending = False
            self._tb_toggle_max()
            return
        self._last_press_at = now
        self._dbl_pending = True
        self._press_x = e.x_root
        self._press_y = e.y_root
        self._press_hwnd = self._hwnd()
        self.root.after(60, self._check_start_drag)

    def _check_start_drag(self):
        if not getattr(self, '_dbl_pending', False):
            return
        try:
            if not (ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000):
                self._dbl_pending = False
                return
            pt = wt.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            if (abs(pt.x - self._press_x) < 4 and
                    abs(pt.y - self._press_y) < 4):
                self.root.after(50, self._check_start_drag)
                return
        except Exception:
            self._dbl_pending = False
            return
        self._dbl_pending = False
        if self._is_maximized:
            self._tb_toggle_max()
        hwnd = self._press_hwnd or self._hwnd()
        if not hwnd:
            return
        try:
            pt = wt.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            r = wt.RECT()
            ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(r))
            self._drag_off = (pt.x - r.left, pt.y - r.top)
            self._drag_hwnd = hwnd
            self._dragging = True
            self._drag_hold = True
            self._drag_poll()
        except Exception:
            self._drag_hwnd = None
            self._drag_off = None
            self._dragging = False
            self._drag_hold = False
            self._after_drag_sync()

    def _drag_poll(self):
        if not self._dragging:
            return
        try:
            if not (ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000):
                self._dragging = False
                self._drag_hold = False
                self._drag_hwnd = None
                self._drag_off = None
                self._after_drag_sync()
                return
            pt = wt.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            hwnd = getattr(self, '_drag_hwnd', None)
            if hwnd and self._drag_off:
                x = pt.x - self._drag_off[0]
                y = pt.y - self._drag_off[1]
                ctypes.windll.user32.SetWindowPos(
                    hwnd, 0, x, y, 0, 0,
                    0x0001 | 0x0004 | 0x0010)
            self.root.after(5, self._drag_poll)
        except Exception:
            self._dragging = False
            self._drag_hold = False
            self._drag_hwnd = None
            self._drag_off = None
            self._after_drag_sync()

    def _tb_drag_move(self, e):
        return

    def _after_drag_sync(self):
        self._drag_sync_pending = True
        self._drag_hold = False
        # 拖拽结束：若消费未在进行则唤醒一轮（事件驱动，无常驻轮询）
        if not self._poll_active:
            self.root.after_idle(self._poll_queue)
        try:
            self.root.update_idletasks()
        except Exception:
            pass
        self.root.after_idle(self._flush_bg_redraw)

    def _flush_bg_redraw(self):
        if self._dragging:
            self._bg_dirty = True
            return
        try:
            w = self.bg.winfo_width()
            h = self.bg.winfo_height()
            if w < 2 or h < 2:
                self.root.after_idle(self._flush_bg_redraw)
                return
            self._drag_sync_pending = False
            self._draw_gradient(self.bg, w, h)
            self._bg_dirty = False
            if not self._is_maximized:
                self._apply_round_region(self.ROUND_RADIUS)
        except Exception:
            self.root.after_idle(self._flush_bg_redraw)

    def _tb_drag_end(self, e):
        if getattr(self, '_dbl_pending', False):
            self._dbl_pending = False
        if self._dragging:
            self._dragging = False
            self._after_drag_sync()

    def _build_nav(self):
        n = self.nav
        _TITLE_AREA_H = 90
        self._nav_title = self.nav.create_text(
            44, _TITLE_AREA_H // 2, text='Masker', anchor='w',
            fill=self.NAV_TEXT, font=('Hartwell Alt Bold', 24),
            tags='nav_title')
        for ev, fn in (('<Button-1>', self._tb_drag_start),
                       ('<B1-Motion>', self._tb_drag_move),
                       ('<ButtonRelease-1>', self._tb_drag_end),
                       ('<Double-Button-1>', self._tb_toggle_max)):
            self.nav.tag_bind('nav_title', ev, fn)
        items = ['程序']
        self._nav_tags = {}
        self._nav_icon_w = 13
        self._nav_text_x = 44 + self._nav_icon_w + 6
        self._nav_text_cx = (14 + 182) / 2.0
        y0 = 96
        for i, name in enumerate(items):
            y = y0 + i * 40
            tag = 'nav_%d' % i
            ttag = tag + '_t'
            itag = tag + '_i'
            self._nav_tags[name] = (tag, y)
            self._rounded(n, 14, y, 182, y + 34, 10, fill=self.NAV_INACTIVE,
                          outline='', tags=tag)
            n.create_text(44, y + 17, anchor='w', text=self.NAV_ICONS.get(name, ''),
                          fill=self.NAV_TEXT, font=('Font Awesome 6 Free', 10),
                          tags=itag)
            n.create_text(self._nav_text_x, y + 17, anchor='w', text=name,
                          fill=self.NAV_TEXT, font=(self.FONT, 10, 'bold'),
                          tags=ttag)
            n.tag_bind(tag, '<Button-1>', lambda e, nm=name: self._switch(nm))
            n.tag_bind(tag, '<Enter>', lambda e, nm=name: self._nav_hover(nm, True))
            n.tag_bind(tag, '<Leave>', lambda e, nm=name: self._nav_hover(nm, False))
            n.tag_bind(ttag, '<Button-1>', lambda e, nm=name: self._switch(nm))
            n.tag_bind(itag, '<Button-1>', lambda e, nm=name: self._switch(nm))

        name = '设置'
        tag = 'nav_settings'
        ttag = tag + '_t'
        itag = tag + '_i'
        y = 540
        self._nav_tags[name] = (tag, y)
        self._nav_settings_poly = self._rounded(
            n, 14, y, 182, y + 34, 10, fill=self.NAV_INACTIVE, outline='',
            tags=tag)
        self._nav_settings_icon = n.create_text(
            44, y + 17, anchor='w', text=self.NAV_ICONS.get(name, ''),
            fill=self.NAV_TEXT, font=('Font Awesome 6 Free', 10), tags=itag)
        self._nav_settings_label = n.create_text(
            self._nav_text_x, y + 17, anchor='w', text=name, fill=self.NAV_TEXT,
            font=(self.FONT, 10, 'bold'), tags=ttag)
        n.tag_bind(tag, '<Button-1>', lambda e, nm=name: self._switch(nm))
        n.tag_bind(tag, '<Enter>', lambda e, nm=name: self._nav_hover(nm, True))
        n.tag_bind(tag, '<Leave>', lambda e, nm=name: self._nav_hover(nm, False))
        n.tag_bind(ttag, '<Button-1>', lambda e, nm=name: self._switch(nm))
        n.tag_bind(itag, '<Button-1>', lambda e, nm=name: self._switch(nm))

        self._nav_divider = n.create_line(16, 524, 180, 524, fill='#e8e8e8')
        n.bind('<Configure>', self._relayout_nav)

    def _relayout_nav(self, event=None):
        try:
            h = self.nav.winfo_height()
            if h < 200:
                return
            y = h - 52
            x1, x2, r = 14, 182, 10
            pts = [x1 + r, y, x2 - r, y, x2, y, x2, y + r,
                   x2, y + 34 - r, x2, y + 34, x2 - r, y + 34,
                   x1 + r, y + 34, x1, y + 34, x1, y + 34 - r,
                   x1, y + r, x1, y]
            self.nav.coords(self._nav_settings_poly, *pts)
            self.nav.coords(self._nav_settings_icon, 44, y + 17)
            self.nav.coords(self._nav_settings_label, self._nav_text_x, y + 17)
            self.nav.coords(self._nav_divider, 16, y - 16, 180, y - 16)
            self._nav_tags['设置'] = (self._nav_tags['设置'][0], y)
        except Exception:
            pass

    def _nav_hover(self, name, enter):
        tag, y = self._nav_tags[name]
        if name == self.current_page:
            return
        self.nav.itemconfig(tag, fill='#f0f0f0' if enter else self.NAV_INACTIVE)

    def _nav_redraw(self):
        for name, (tag, y) in self._nav_tags.items():
            active = (name == self.current_page)
            self.nav.itemconfig(tag, fill=self.NAV_ACTIVE if active else self.NAV_INACTIVE)
            for i in self.nav.find_withtag(tag + '_t'):
                self.nav.itemconfig(i, fill=self.NAV_TEXT_ACTIVE if active else self.NAV_TEXT)
            for i in self.nav.find_withtag(tag + '_i'):
                self.nav.itemconfig(i, fill=self.NAV_TEXT_ACTIVE if active else self.NAV_TEXT)

    # ---------------- 导航文字动画 ----------------
    def _nav_anim_start(self):
        base = self._nav_text_base_x()
        cx = getattr(self, '_nav_text_cx', 98)
        if getattr(self, '_nav_anim_after_id', None) is not None:
            try:
                self.root.after_cancel(self._nav_anim_after_id)
            except Exception:
                pass
            self._nav_anim_after_id = None
        items = {}
        for nm, (tag, y) in self._nav_tags.items():
            ttag = tag + '_t'
            coords = self.nav.coords(ttag)
            cur = coords[0] if coords else base
            target = cx if nm == self.current_page else base
            if abs(cur - target) > 1:
                items[nm] = [ttag, cur, target]
        if not items:
            self._nav_anim_running = False
            self._nav_anim_items = {}
            return
        self._nav_anim_running = True
        self._nav_anim_items = items
        self._nav_anim_step()

    def _nav_anim_step(self):
        items = getattr(self, '_nav_anim_items', None)
        if not items:
            self._nav_anim_running = False
            return
        step = 4
        done = True
        for nm, (ttag, cur, target) in items.items():
            _co = self.nav.coords(ttag)
            y = _co[1] if len(_co) > 1 else 0
            if abs(cur - target) <= step:
                self.nav.coords(ttag, target, y)
            else:
                cur += step if target > cur else -step
                self.nav.coords(ttag, cur, y)
                items[nm][1] = cur
                done = False
        if done:
            self._nav_anim_running = False
            self._nav_anim_items = {}
            self._nav_anim_after_id = None
        else:
            self._nav_anim_after_id = self.root.after(16, self._nav_anim_step)

    def _nav_text_base_x(self):
        return getattr(self, '_nav_text_x', 60)

    # ---------------- 程序页 ----------------
    def _build_page_program(self, parent):
        page = tk.Frame(parent, bg='#f7f7f7')

        head = tk.Frame(page, bg='#f7f7f7')
        head.pack(fill='x', padx=28, pady=(40, 6))
        tk.Label(head, text='程序', bg='#f7f7f7', fg=self.TEXT_MAIN,
                 font=(self.FONT, 18, 'bold')).pack(side='left')
        # 切换开关（fa-eye-slash / fa-eye）：一键在「全部显示」与「全部隐藏」之间切换
        self.btn_toggle = tk.Button(head, text='\uf070', bg='#f7f7f7', fg='#35d07f',
                                    activebackground='#f0f0f0', activeforeground='#35d07f',
                                    relief='flat', bd=0, highlightthickness=0,
                                    font=('Font Awesome 6 Free', 16),
                                    cursor='hand2', command=self._toggle_state)
        self.btn_toggle.pack(side='left', padx=(16, 0))
        tk.Label(head, text='切换隐藏文件与受保护操作系统文件的显示状态',
                 bg='#f7f7f7', fg=self.TEXT_DIM,
                 font=(self.FONT, 11)).pack(side='left', padx=(10, 0))

        btn_kw = dict(bg='#f7f7f7', bd=0, relief='flat', highlightthickness=0,
                      cursor='hand2')
        self._log_icon_btn = tk.Label(head, text='\u269d', fg='#888888',
                                      font=('Segoe UI Symbol', 15), **btn_kw)
        self._log_icon_btn.pack(side='right')
        self._log_icon_btn.bind('<Button-1>', lambda e: self._toggle_log_panel())
        self._log_icon_btn.bind('<Enter>', lambda e: self._log_icon_btn.configure(
            bg='#f0f0f0'))
        self._log_icon_btn.bind('<Leave>', lambda e: self._set_log_btn_state(
            self._log_visible))

        # 总体状态横幅
        banner = tk.Frame(page, bg='#f7f7f7')
        banner.pack(fill='x', padx=28, pady=(10, 0))
        self.status_banner = tk.Canvas(banner, height=64, bg='#f7f7f7',
                                       highlightthickness=0, bd=0)
        self.status_banner.pack(fill='x')
        banner.bind('<Configure>',
                    lambda e: self._draw_status_banner(e.width))

        # 大数字卡片：两项设置
        cards = tk.Frame(page, bg='#f7f7f7')
        cards.pack(fill='x', padx=28, pady=(10, 6))
        cards.grid_columnconfigure(0, weight=1, uniform='c')
        cards.grid_columnconfigure(1, weight=1, uniform='c')

        self.sup_card = tk.Canvas(cards, height=172, bg='#f7f7f7',
                                  highlightthickness=0, bd=0)
        self.sup_card.grid(row=0, column=0, sticky='nsew', padx=(0, 8))
        self.hid_card = tk.Canvas(cards, height=172, bg='#f7f7f7',
                                  highlightthickness=0, bd=0)
        self.hid_card.grid(row=0, column=1, sticky='nsew', padx=(8, 0))
        cards.bind('<Configure>', lambda e: self._draw_cards(e.width))

        # 手动切换按钮
        btns = tk.Frame(page, bg='#f7f7f7')
        btns.pack(fill='x', padx=28, pady=(14, 6))
        tk.Label(btns, text='手动切换：', bg='#f7f7f7', fg=self.TEXT_DIM,
                 font=(self.FONT, 11)).pack(side='left')
        self.btn_show = tk.Button(btns, text='切换为全部显示', command=self._set_show,
                                  bg='#35d07f', fg='#ffffff', relief='flat', bd=0,
                                  activebackground='#2bb26c', activeforeground='#ffffff',
                                  font=(self.FONT, 11, 'bold'), cursor='hand2',
                                  padx=18, pady=4)
        self.btn_show.pack(side='left', padx=(8, 0))
        self.btn_hide = tk.Button(btns, text='切换为全部隐藏', command=self._set_hide,
                                  bg='#333333', fg='#ffffff', relief='flat', bd=0,
                                  activebackground='#555555', activeforeground='#ffffff',
                                  font=(self.FONT, 11, 'bold'), cursor='hand2',
                                  padx=18, pady=4)
        self.btn_hide.pack(side='left', padx=(10, 0))
        tk.Label(btns, text='也可左键单击系统托盘图标快速切换', bg='#f7f7f7',
                 fg='#888888', font=(self.FONT, 9)).pack(side='left', padx=(16, 0))

        # 右侧可收起日志侧边栏
        self.log_panel = tk.Frame(self.root, bg='#f7f7f7', highlightthickness=0)
        self._log_visible = False
        self._log_anim = False
        self._log_cur = 0
        self._log_target = 0
        self.log_panel.place_forget()
        lp_sep = tk.Frame(self.log_panel, bg='#e8e8e8', width=1)
        lp_sep.pack(side='left', fill='y')
        lp_head = tk.Frame(self.log_panel, bg='#f7f7f7')
        lp_head.pack(fill='x', pady=(40, 0))
        tk.Label(lp_head, text='运行日志', bg='#f7f7f7', fg=self.TEXT_MAIN,
                 font=(self.FONT, 14, 'bold')).pack(pady=(10, 8))
        self.log_text = tk.Text(self.log_panel, height=8, bg='#f7f7f7', fg='#333333',
                                insertbackground='#333333', relief='flat', bd=0,
                                font=('Consolas', 9), state='disabled', wrap='word',
                                padx=10, pady=4)
        sb = tk.Canvas(self.log_panel, width=10, bg='#f7f7f7',
                       highlightthickness=0, bd=0)
        self._log_sb = sb
        self._log_sb_hide_after = None
        self._log_sb_drag_off = 0
        self._log_sb_geo = sb.create_rectangle(0, 0, 10, 20, fill='', outline='')
        self._log_sb_item = sb.create_polygon(
            *_round_rect_pts(0, 0, 10, 20, 4),
            fill='#ffffff', outline='#d8d8d8', width=1, smooth=True)
        sb.tag_raise('all')
        self.log_text.bind('<MouseWheel>', self._log_sb_kick)
        sb.bind('<ButtonPress-1>', self._log_sb_press)
        sb.bind('<B1-Motion>', self._log_sb_drag)
        sb.bind('<ButtonRelease-1>', lambda e: self._log_sb_kick(e, release=True))
        self.log_text.configure(yscrollcommand=self._log_sb_update)
        self.log_text.pack(side='left', fill='both', expand=True)
        self.log('程序已就绪。点击顶部切换按钮或左键单击托盘图标，在「全部显示」与「全部隐藏」之间切换。')

        return page

    def _set_log_btn_state(self, active):
        try:
            self._log_icon_btn.configure(
                bg='#f7f7f7' if active else '#f7f7f7',
                text='\u2347' if active else '\u269d')
        except Exception:
            pass

    def _log_sb_kick(self, event=None, release=False):
        sb = getattr(self, '_log_sb', None)
        if sb is None:
            return
        try:
            if not sb.winfo_ismapped():
                head = getattr(self, 'log_panel', None).winfo_children()[1]
                y0 = head.winfo_y() + head.winfo_height() + 4
                h0 = self.log_panel.winfo_height() - y0 - 8
                if h0 < 40:
                    h0 = 40
                sb.place(relx=1.0, rely=0.0, anchor='ne',
                         y=y0, height=h0, width=10)
                self.root.after_idle(self._log_sb_refresh)
        except Exception:
            return
        delay = 1200 if release else 2000
        if self._log_sb_hide_after is not None:
            try:
                self.root.after_cancel(self._log_sb_hide_after)
            except Exception:
                pass
            self._log_sb_hide_after = None
        self._log_sb_hide_after = self.root.after(delay, self._log_sb_hide)

    def _log_sb_update(self, first, last):
        self._log_sb_refresh()

    def _log_sb_refresh(self):
        sb = getattr(self, '_log_sb', None)
        if sb is None or not sb.winfo_ismapped():
            return
        try:
            first, last = self.log_text.yview()
            h = sb.winfo_height()
            frac = max(0.0, min(1.0, last - first))
            thumb_h = max(20, int(h * frac))
            y0 = int(first * (h - thumb_h)) if h > thumb_h else 0
            w = sb.winfo_width()
            sb.coords(self._log_sb_geo, 0, y0, w, y0 + thumb_h)
            sb.coords(self._log_sb_item,
                      *_round_rect_pts(0, y0, w, y0 + thumb_h, 4))
        except Exception:
            pass

    def _log_sb_press(self, event):
        self._log_sb_kick(event)
        try:
            c = self._log_sb.coords(self._log_sb_geo)
            self._log_sb_drag_off = event.y - c[1]
        except Exception:
            self._log_sb_drag_off = 0

    def _log_sb_drag(self, event):
        self._log_sb_kick(event)
        try:
            h = self._log_sb.winfo_height()
            c = self._log_sb.coords(self._log_sb_geo)
            thumb_h = c[3] - c[1]
            track = h - thumb_h
            if track <= 0:
                return
            frac = min(1.0, max(0.0,
                                (event.y - self._log_sb_drag_off) / track))
            self.log_text.yview_moveto(frac)
        except Exception:
            pass

    def _log_sb_hide(self):
        self._log_sb_hide_after = None
        sb = getattr(self, '_log_sb', None)
        if sb is None:
            return
        try:
            if sb.winfo_ismapped():
                sb.place_forget()
        except Exception:
            pass

    def _toggle_log_panel(self):
        if self._log_anim:
            return
        self._log_anim = True
        if self._log_visible:
            self._log_target = 0
        else:
            self._log_visible = True
            self._log_cur = 0
            self.log_panel.place(relx=1.0, rely=0.0, anchor='ne',
                                 relheight=1.0, width=0)
            self.titlebar_left.lift()
            self.titlebar_mid.lift()
            self.titlebar_right.lift()
            self._set_log_btn_state(True)
            self._log_target = LOG_PANEL_W
        self._log_step()

    def _log_step(self):
        step = 28
        if self._log_target > self._log_cur:
            self._log_cur = min(self._log_cur + step, self._log_target)
        else:
            self._log_cur = max(self._log_cur - step, 0)
        self.log_panel.place(relx=1.0, rely=0.0, anchor='ne',
                             relheight=1.0, width=self._log_cur)
        self.content.place_configure(x=196, y=0, relheight=1, relwidth=1,
                                     width=-(196 + self._log_cur), height=0)
        if self._log_cur != self._log_target:
            self.root.after(12, self._log_step)
        else:
            self._log_anim = False
            if self._log_target == 0:
                self.log_panel.place_forget()
                self.content.place_configure(x=196, y=0, relwidth=1,
                                             relheight=1, width=-196, height=0)
                self._log_visible = False
                self._set_log_btn_state(False)

    def _draw_status_banner(self, w):
        """总体状态横幅：全部显示（绿）/ 全部隐藏（红）/ 混合（橙）。"""
        try:
            self.status_banner.delete('all')
        except Exception:
            return
        if w < 40:
            return
        state = overall_state(self._sup, self._hid)
        color = {'show': self.ACCENT, 'hide': self.DANGER,
                 'mixed': self.WARN}.get(state, '#888888')
        self._rounded(self.status_banner, 0, 0, w, 64, 14,
                      fill=color, outline='', width=0)
        self.status_banner.create_text(
            w // 2, 24, text='当前状态：%s' % overall_text(state),
            fill='#ffffff', font=(self.FONT, 15, 'bold'))
        self.status_banner.create_text(
            w // 2, 46,
            text='隐藏受保护的操作系统文件=%s  ·  隐藏文件和文件夹=%s'
                 % ('显示' if self._sup else '隐藏',
                    '显示' if self._hid else '隐藏'),
            fill='#ffffff', font=(self.FONT, 9))

    def _draw_cards(self, w):
        for c in (self.sup_card, self.hid_card):
            c.delete('all')
        if w < 40:
            return
        cw = (w - 16) // 2
        ch = 172

        # 卡1：隐藏受保护的操作系统文件（HKCU ShowSuperHidden）
        self._rounded(self.sup_card, 0, 0, cw, ch, 18, fill=self.CARD_BG,
                      outline='', width=0)
        self.sup_card.create_text(22, 34, anchor='w', text='隐藏受保护的操作系统文件',
                                  fill=self.TEXT_DIM, font=(self.FONT, 12))
        self.sup_card.create_text(22, 52, anchor='w',
                                  text='ShowSuperHidden  (HKCU)',
                                  fill='#888888', font=(self.FONT, 8))
        sup_fill = self.ACCENT if self._sup == 1 else self.DANGER
        self.sup_value = self.sup_card.create_text(cw // 2, 108,
                                                   text='显示' if self._sup == 1 else '隐藏',
                                                   fill=sup_fill,
                                                   font=(self.FONT, 26, 'bold'))
        self.sup_card.create_text(22, 152, anchor='w',
                                  text='1=显示  0=隐藏（含系统文件）',
                                  fill='#888888', font=(self.FONT, 8))

        # 卡2：隐藏文件和文件夹（HKCU Hidden）
        self._rounded(self.hid_card, 0, 0, cw, ch, 18, fill=self.CARD_BG,
                      outline='', width=0)
        self.hid_card.create_text(22, 34, anchor='w', text='隐藏文件和文件夹',
                                  fill=self.TEXT_DIM, font=(self.FONT, 12))
        self.hid_card.create_text(22, 52, anchor='w',
                                  text='Hidden  (HKCU)',
                                  fill='#888888', font=(self.FONT, 8))
        hid_fill = self.ACCENT if self._hid == 1 else self.DANGER
        self.hid_value = self.hid_card.create_text(cw // 2, 108,
                                                   text='显示' if self._hid == 1 else '隐藏',
                                                   fill=hid_fill,
                                                   font=(self.FONT, 26, 'bold'))
        self.hid_card.create_text(22, 152, anchor='w',
                                  text='1=显示  0=隐藏（普通隐藏文件）',
                                  fill='#888888', font=(self.FONT, 8))

    # ---------------- 设置页 ----------------
    def _build_page_settings(self, parent):
        page = tk.Frame(parent, bg='#f7f7f7')

        head = tk.Frame(page, bg='#f7f7f7')
        head.pack(fill='x', padx=28, pady=(40, 6))
        tk.Label(head, text='设置', bg='#f7f7f7', fg=self.TEXT_MAIN,
                 font=(self.FONT, 18, 'bold')).pack(side='left')
        tk.Button(head, text='关于', command=lambda: self._switch('关于'),
                  bg='#333333', fg='#ffffff', activebackground='#555555',
                  activeforeground='#ffffff', relief='flat', bd=0,
                  font=(self.FONT, 10, 'bold'), cursor='hand2',
                  padx=14, pady=2).pack(side='right')

        card = tk.Frame(page, bg='#fbfbfb', highlightbackground=self.CARD_BORDER,
                        highlightthickness=1)
        card.pack(fill='x', padx=28, pady=(18, 10))

        row4 = tk.Frame(card, bg='#fbfbfb')
        row4.pack(fill='x', padx=24, pady=(20, 20))
        tk.Label(row4, text='启动设置', bg='#fbfbfb', fg=self.TEXT_MAIN,
                 font=(self.FONT, 12)).pack(side='left')
        self.auto_start_var = tk.BooleanVar(value=False)
        tk.Button(row4, text='开机自启', command=self._apply_auto_start,
                  bg='#35d07f', fg='#ffffff', relief='flat', bd=0,
                  activebackground='#2bb26c', activeforeground='#ffffff',
                  font=(self.FONT, 10), cursor='hand2', padx=16, pady=2).pack(side='left', padx=(24, 0))
        tk.Label(row4, text='经 HKCU Run 键生效（登录时静默驻留托盘，无需管理员权限）',
                 bg='#fbfbfb', fg='#888888', font=(self.FONT, 9)).pack(side='left', padx=(16, 0))

        return page

    # ---------------- 关于页 ----------------
    def _build_page_about(self, parent):
        page = tk.Frame(parent, bg='#f7f7f7')

        head = tk.Frame(page, bg='#f7f7f7')
        head.pack(fill='x', padx=28, pady=(40, 6))
        tk.Label(head, text='关于', bg='#f7f7f7', fg=self.TEXT_MAIN,
                 font=(self.FONT, 18, 'bold')).pack(side='left')
        tk.Button(head, text='返回', command=lambda: self._switch('设置'),
                  bg='#333333', fg='#ffffff', activebackground='#555555',
                  activeforeground='#ffffff', relief='flat', bd=0,
                  font=(self.FONT, 10, 'bold'), cursor='hand2',
                  padx=14, pady=2).pack(side='right')

        card = tk.Frame(page, bg='#fbfbfb', highlightbackground=self.CARD_BORDER,
                        highlightthickness=1)
        card.pack(fill='x', padx=28, pady=(18, 10))
        lines = [
            '· 切换方式：直接改写注册表 HKCU\\...\\Explorer\\Advanced 下两项设置',
            '· 隐藏受保护的操作系统文件：HKCU\\...\\Explorer\\Advanced\\ShowSuperHidden（1=显示、0=隐藏）',
            '· 隐藏文件和文件夹：HKCU\\...\\Explorer\\Advanced\\Hidden（1=显示、0=隐藏）',
            '· 托盘操作：左键单击在「全部显示」与「全部隐藏」之间来回切换；右键菜单可手动选择状态',
            '· 权限要求：仅写入 HKCU，无需管理员权限（exe 未内置 UAC 提权清单）',
            '· 日志：右侧栏记录每次切换命令与结果，可随时展开查看',
        ]
        for i, ln in enumerate(lines):
            tk.Label(card, text=ln, bg='#fbfbfb', fg='#888888',
                     font=(self.FONT, 9), anchor='w', justify='left').pack(fill='x', pady=2)

        return page

    # ---------------- 行为 ----------------
    def _switch(self, name):
        self.current_page = name
        for p in self.pages.values():
            p.place_forget()
        self.pages[name].place(relwidth=1, relheight=1)
        self._nav_redraw()
        self._nav_anim_start()

    def _toggle_state(self):
        """切换开关 / 托盘左键：在「全部显示」与「全部隐藏」之间来回切换。
        当前为全部显示 -> 切到全部隐藏；其余（全部隐藏/混合）-> 切到全部显示。"""
        if self._switch_running:
            self.log('切换正在进行中，请稍候...')
            return
        sup, hid = get_states()
        if sup == 1 and hid == 1:
            self.log('正在切换到全部隐藏（ShowSuperHidden=0，Hidden=0）...')
            self._switch_running = True
            threading.Thread(target=self._apply_worker,
                             args=(0, 0, '全部隐藏'), daemon=True).start()
        else:
            self.log('正在切换到全部显示（ShowSuperHidden=1，Hidden=1）...')
            self._switch_running = True
            threading.Thread(target=self._apply_worker,
                             args=(1, 1, '全部显示'), daemon=True).start()

    def _set_show(self):
        """手动切换为全部显示。"""
        if self._switch_running:
            self.log('切换正在进行中，请稍候...')
            return
        self._switch_running = True
        threading.Thread(target=self._apply_worker,
                         args=(1, 1, '全部显示'), daemon=True).start()

    def _set_hide(self):
        """手动切换为全部隐藏。"""
        if self._switch_running:
            self.log('切换正在进行中，请稍候...')
            return
        self._switch_running = True
        threading.Thread(target=self._apply_worker,
                         args=(0, 0, '全部隐藏'), daemon=True).start()

    def _apply_worker(self, sup, hid, label):
        ok, msg = set_states(sup, hid)
        try:
            self.q.put(('log', ('切换成功：%s  %s' % (label, msg))
                        if ok else ('切换失败：%s' % msg)))
            self._wake_poll()
        except Exception:
            pass
        try:
            self.q.put(('state_updated', ok))
            self._wake_poll()
        except Exception:
            pass

    def _apply_auto_start(self):
        import tkinter.messagebox as _mb
        enabled = not bool(self.auto_start_var.get())
        if enabled:
            msg = ('将设置开机自启（写入 HKCU Run 键）：\n\n'
                   '值名称：%s\n'
                   '动作：登录时静默启动 %s --silent\n\n'
                   '确认后立即写入，下次登录自动进入托盘。'
                   % (RUN_VALUE_NAME, autostart_exe_path()))
        else:
            msg = ('将取消开机自启（删除 HKCU Run 键 %s）：\n\n'
                   '确认后立即删除，开机不再自动启动。' % RUN_VALUE_NAME)
        if not _mb.askyesno('开机自启确认', msg, parent=self.root):
            return
        ok, m = set_autostart(enabled)
        if ok:
            self.auto_start_var.set(enabled)
            self.log('开机自启已%s：%s' % ('开启' if enabled else '关闭', m))
        else:
            self.log('开机自启设置失败：%s' % m)

    def _apply_startup_settings(self):
        self.auto_start_var.set(is_autostart_set())
        # 启动后立即读取当前注册表状态并更新展示
        self.root.after(200, self._refresh_view)

    def _poll_queue(self):
        """事件驱动消费队列（替代原先每 100ms 常驻轮询）。

        仅在收到唤醒通知（<<QueuePoll>> 事件 / after_idle / 启动兜底）时
        被调用；消费期间有新 put 会继续循环直到排空，排空后不再自我重排，
        空闲时主循环不被定时唤醒，消除后台空转 CPU 消耗。
        """
        if self._drag_hold:
            self._poll_active = False
            return
        if self._poll_active:
            # 防重入：已有一次消费在进行，本次唤醒让位
            return
        self._poll_active = True
        try:
            while True:
                try:
                    item = self.q.get_nowait()
                except queue.Empty:
                    break
                if item[0] == 'log':
                    self.log(item[1])
                elif item[0] == 'state_updated':
                    _, ok = item
                    self._switch_running = False
                    if ok:
                        self.log('设置已写入 HKCU')
                    else:
                        self.log('切换未生效，请检查注册表写入是否被安全软件拦截')
                    self._refresh_view()
        finally:
            self._poll_active = False
        # 消费期间又来了新消息（消费中 put）：继续调度一轮直到排空
        if not self.q.empty():
            self.root.after_idle(self._poll_queue)

    def _wake_poll(self):
        """线程安全的队列唤醒：向主线程投递 <<QueuePoll>> 事件。

        从后台线程（_apply_worker）调用，event_generate 仅向 Tcl 事件
        队列投递事件，不直接操作解释器状态，跨线程安全；失败时降级为
        after_idle 兜底。
        """
        try:
            self.root.event_generate('<<QueuePoll>>', when='tail')
        except Exception:
            try:
                self.root.after_idle(self._poll_queue)
            except Exception:
                pass

    def _refresh_view(self):
        """读取当前注册表状态，更新界面卡片、状态横幅与托盘图标/菜单。"""
        try:
            self._sup, self._hid = get_states()
        except Exception:
            pass
        try:
            self._draw_status_banner(self.status_banner.winfo_width())
            self._draw_cards((self.sup_card.master.winfo_width()
                              if self.sup_card.master else 800))
        except Exception:
            pass
        try:
            self._update_tray()
        except Exception:
            pass

    def log(self, msg):
        try:
            self.log_text.config(state='normal')
            self.log_text.insert('end', msg + '\n')
            self.log_text.see('end')
            self.log_text.config(state='disabled')
        except Exception:
            pass

    def on_close(self):
        """点击窗口 X：先启动托盘，再隐藏主窗口（程序驻留托盘）。"""
        if not _HAS_TRAY:
            self.log('未安装 pystray/Pillow，无法托盘化，直接退出')
            self._quit()
            return
        self._start_tray()
        self.root.withdraw()
        self.log('已最小化到系统托盘，左键托盘图标可切换，右键菜单可退出')

    # ---------------- 托盘 ----------------
    def _start_tray(self):
        if self.tray is not None:
            return
        try:
            self.tray = pystray.Icon('file_show_switch', make_tray_icon(),
                                     WINDOW_TITLE, self._tray_menu())
            self._tray_icon_variant = None
            self.tray.run_detached()
            threading.Timer(0.3, self._tray_make_visible).start()
            threading.Timer(1.5, self._tray_check_visible).start()
            threading.Timer(0.8, lambda: self.root.after(0, self._update_tray)).start()
        except Exception as e:
            self.log('托盘启动失败：%s' % e)
            self.tray = None
            self.root.deiconify()

    def _tray_menu(self):
        state = overall_state(self._sup, self._hid)
        return pystray.Menu(
            pystray.MenuItem('切换显示/隐藏状态', self._tray_click_toggle,
                             default=True),
            pystray.MenuItem('当前状态：%s' % overall_text(state), None,
                             enabled=False),
            pystray.MenuItem('切换为全部显示', self._tray_set_show),
            pystray.MenuItem('切换为全部隐藏', self._tray_set_hide),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem('显示/隐藏窗口', self._tray_toggle_window),
            pystray.MenuItem('退出', self._tray_quit),
        )

    def _tray_make_visible(self):
        try:
            if self.tray is not None:
                self.tray.visible = True
        except Exception:
            pass

    def _tray_check_visible(self):
        try:
            t = self.tray
            if t is None:
                return
            if not t.visible:
                self.log('托盘图标未能显示，恢复主窗口')
                self.root.after(0, self._show_window)
        except Exception:
            pass

    def _tray_click_toggle(self, icon, item):
        """托盘左键单击（菜单默认项回调）：先挂起判定是否为双击。
        双击 -> 显示/隐藏主窗口；单击 -> 直接切换显示状态。"""
        try:
            self.root.after(0, self._tray_on_lclick)
        except Exception:
            pass

    def _tray_on_lclick(self):
        """左键单击事件（主线程）：双击判定 + 单击切换。"""
        try:
            dbl_ms = int(ctypes.windll.user32.GetDoubleClickTime())
        except Exception:
            dbl_ms = 500
        now = time.time()
        if (getattr(self, '_tray_click_ts', 0)
                and (now - self._tray_click_ts) * 1000.0 <= dbl_ms):
            # 判定为双击：取消挂起的单击切换，改为显示/隐藏主窗口
            self._tray_click_ts = 0
            if getattr(self, '_tray_click_after', None):
                try:
                    self.root.after_cancel(self._tray_click_after)
                except Exception:
                    pass
                self._tray_click_after = None
            self._tray_toggle_window()
            return
        self._tray_click_ts = now
        self._tray_click_after = self.root.after(
            dbl_ms + 80, self._tray_fire_switch)

    def _tray_fire_switch(self):
        """单击确认：执行状态切换。"""
        self._tray_click_after = None
        self._tray_click_ts = 0
        self._toggle_state()

    def _tray_set_show(self, icon=None, item=None):
        self.root.after(0, self._set_show)

    def _tray_set_hide(self, icon=None, item=None):
        self.root.after(0, self._set_hide)

    def _tray_toggle_window(self, icon=None, item=None):
        """显示/隐藏主窗口（托盘左键双击或右键菜单项触发）。"""
        try:
            if self.root.winfo_viewable():
                self.root.withdraw()
            else:
                self._show_window()
        except Exception:
            self._show_window()

    def _update_tray(self):
        """按当前状态同步托盘图标颜色与菜单（绿色=全部显示、红色=全部隐藏、橙色=混合）。"""
        try:
            if self.tray is None:
                return
            variant = overall_state(self._sup, self._hid)
            if getattr(self, '_tray_icon_variant', None) != variant:
                self._tray_icon_variant = variant
                self.tray.icon = make_tray_icon(64, variant=variant)
                # 强制刷新托盘缓存（设置 icon 后翻转 visible 触发 NIM_MODIFY）
                try:
                    self.tray.visible = False
                    self.tray.visible = True
                except Exception:
                    pass
            self.tray.menu = self._tray_menu()
        except Exception:
            pass

    def _show_window(self):
        self.root.deiconify()
        hwnd = self._hwnd()
        if hwnd:
            try:
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            except Exception:
                pass
        self.root.lift()
        self.root.focus_force()

    def _tray_quit(self, icon, item):
        try:
            icon.stop()
        except Exception:
            pass
        self.root.after(0, self._quit)

    def _quit(self):
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
            self.tray = None
        self.root.destroy()


_BUNDLED_FONTS = ('HartwellAlt-Bold.ttf', 'fa-solid-900.ttf')


def _register_bundled_font():
    """注册随包分发的字体（FR_PRIVATE 仅对当前进程生效，不污染系统字体库）。"""
    gdi32 = ctypes.windll.gdi32
    FR_PRIVATE = 0x10
    ok = True
    for name in _BUNDLED_FONTS:
        try:
            path = resource_path(name)
            if not os.path.isfile(path):
                ok = False
                continue
            n = gdi32.AddFontResourceExW(path, FR_PRIVATE, 0)
            if n == 0:
                n = gdi32.AddFontResourceW(path)
            if n <= 0:
                ok = False
        except Exception:
            ok = False
    return ok


def main():
    if sys.platform != 'win32':
        print('本程序仅支持 Windows。')
        sys.exit(1)
    _register_bundled_font()
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass
    if not _acquire_single_instance():
        if IS_SILENT:
            sys.exit(0)
        _ok = _activate_existing_window()
        if _ok:
            sys.exit(0)
        try:
            import tkinter.messagebox as _mb
            _mb.showwarning(WINDOW_TITLE, '程序已在运行，请查看已打开的窗口。')
        except Exception:
            pass
        sys.exit(0)
    root = tk.Tk()
    if IS_SILENT:
        root.withdraw()
    app = MarvisUI(root)
    root.protocol('WM_DELETE_WINDOW', app.on_close)
    # v15：启动即不挂任何 WinEventHook（后台静置绝对 0% CPU），
    # 仅绑定钩子生命周期状态回调到 UI 日志，便于观察挂载/降级/摘除。
    _hook_status_cb = lambda m: root.after(0, lambda: app.log(m))
    try:
        if _HAS_TRAY:
            app._start_tray()
    except Exception:
        pass
    root.mainloop()


if __name__ == '__main__':
    if sys.platform != 'win32':
        print('本程序仅支持 Windows。')
        sys.exit(1)
    main()
