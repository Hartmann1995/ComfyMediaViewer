#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ComfyUI 媒体浏览器 V2.13.5
========================
本地媒体浏览器：扫描目录、网格缩略图、图片/视频预览、读取 ComfyUI PNG 元数据、
一键回传工作流到 ComfyUI (127.0.0.1:8188)。

用法:
    python server.py                          # 自动探测默认目录
    python server.py --root "D:\\我的图片"     # 指定根目录
    python server.py --port 8765 --no-browser # 固定端口、不开浏览器

零第三方依赖；若环境里有 Pillow 则自动生成磁盘级缓存的缩略图。
仅监听 127.0.0.1，文件访问被限制在当前根目录内。
环境变量：COMFYUI_URL  = ComfyUI 地址（默认 http://127.0.0.1:8188）；
          COMFYUI_ROOT = ComfyUI 安装目录（用于定位 UI 工作流模板）。
"""

import argparse
import concurrent.futures
import contextlib
import ctypes
import email.utils
import hashlib
import io
import ipaddress
import json
import math
import mimetypes
import os
import queue
import re
import secrets
import shutil
import socket
import stat as stat_mod
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# NSFW 检测引擎（V2.11.0，可选依赖）。这里**故意不套 try/except** ——
# onnxruntime / numpy / Pillow 缺哪个都由 nsfw.py 自己吞在里面，它永远导得进来；
# 真正的可用性看 nsfw.available()，缺库时只在界面上提示，绝不 500、不影响既有功能。
# 打包成 exe 时 nsfw 必须进 hiddenimports（见 ComfyMediaViewer.spec）。
import nsfw as NSFW_ENGINE
import lora_links

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS          # /api/file 只允许内联渲染这些类型
PLAYABLE_VIDEO = {".mp4", ".webm", ".m4v", ".mov"}
CHUNK = 1 << 16

APP_TITLE = "ComfyUI Media Viewer"
APP_VERSION = "2.13.5"          # 版本号唯一来源（/api/info 与 Server 头都取它）
# 关于页「项目」链接：**只认枚举 key，接口不接收任意 URL** —— 否则这个端点就成了
# 「本机任意网址打开器」，能被拿来钓鱼或触发本机协议。新增入口只改这一处，
# 前端从 /api/sysinfo 的 links 字段取用（不在前端硬编码，避免两处漂移）。
ABOUT_LINKS = {
    "repo":     "https://github.com/Hartmann1995/ComfyMediaViewer",
    "releases": "https://github.com/Hartmann1995/ComfyMediaViewer/releases",
    "issues":   "https://github.com/Hartmann1995/ComfyMediaViewer/issues",
    "license":  "https://github.com/Hartmann1995/ComfyMediaViewer/blob/main/LICENSE",
    "notice":   "https://github.com/Hartmann1995/ComfyMediaViewer/blob/main/models/nsfw/NOTICE.md",
}
# 第 22 级 P1-4：桌面壳 spawn 后端时经环境变量给的一次性令牌，由本进程**原样回显**
# 在每个响应头里，供壳逐字节核对。它不做访问控制（本机模式本来就没有鉴权），
# 作用是**身份证明**：壳靠它挡掉「端口被别的程序抢注 ⇒ 把陌生页面装进 iframe」。
# 独立运行 / 网页版没有这个环境变量 ⇒ 不发头，行为与从前完全一致；
# 「重启程序」走 _restart_env()（os.environ 拷贝）⇒ 新进程自动继承同一个令牌。
SHELL_TOKEN = os.environ.get("CMV_SHELL_TOKEN", "").strip()
SHELL_TOKEN_HEADER = "X-CMV-Shell-Token"
COMFY_BASE = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
# ---------------------------------------------------------------- 数据目录（F28 便携模式）
# 设置 / 收藏 / 标签 / 索引 / 缩略图缓存 / 回收站全在这里。默认在用户目录
# （%LOCALAPPDATA%\ComfyMediaViewer），便携模式改成程序目录下的 cmv-data/。
#
# 为什么用「exe 旁一个标记文件」而不是把选择写进 settings.json：后者自己就住在数据目录里，
# 等于「要先知道数据目录才能读到数据目录」，成了死循环。标记文件放在程序旁还有实际好处——
# 整个文件夹拷到 U 盘，便携状态跟着一起走（换机不用重新设一次）。
PORTABLE_DATA_NAME = "cmv-data"

# 程序目录覆盖（优先级见 _exe_dir）。三档来源：
#   1. `CMV_EXE_DIR` 环境变量 —— 验证脚本 / 自定义启动器用，最高优先级，随时能压过下面两个
#   2. --app-dir 命令行 —— **桌面壳专用**。桌面壳把后端 exe 释放到 %LOCALAPPDATA% 下去跑，
#      于是 sys.executable 指的是那个释放目录，不是用户放程序的地方；壳必须把自己 exe 的真实
#      位置从这儿告诉后端，否则便携目录会落在 C 盘（用户看到的就是「便携了个寂寞」）。
#   3. 都没有就按常规算（源码判断 __file__、打包判断 sys.executable）
_APP_DIR_OVERRIDE = None


def _preparse_app_dir(argv=None) -> str:
    """先于 argparse 把 --app-dir 抠出来。

    _exe_dir() 不只被 main() 用 —— 模块级就有 PORTABLE_FLAG / DATA_DIR 等一批常量要算，
    它们比 main() 跑得早。所以这个开关得在导入期就能生效，不能等 argparse。
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    for i, a in enumerate(argv):
        if a == "--app-dir" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--app-dir="):
            return a.split("=", 1)[1]
    return ""


def _exe_dir() -> Path:
    """程序所在目录：打包后 = exe 同目录；源码直启 = server.py 同目录。

    可被 _APP_DIR_OVERRIDE / CMV_EXE_DIR 覆盖 —— 便携模式的标记与数据目录都挂在这上面，
    验证脚本必须能把它指到临时目录，否则一次测试就会在工程里留下 portable.flag（下次真启动
    会被切到便携模式，看着像数据丢了）。桌面壳也靠它把「程序真正在哪」传进来。
    """
    env = os.environ.get("CMV_EXE_DIR")
    if env:
        return Path(env).resolve()
    if _APP_DIR_OVERRIDE:
        return Path(_APP_DIR_OVERRIDE).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


_APP_DIR_OVERRIDE = _preparse_app_dir()


def _user_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ComfyMediaViewer"


PORTABLE_FLAG = _exe_dir() / "portable.flag"


def _portable_flag_dir() -> Path:
    """读 portable.flag 里的 {"dir": ...} 覆盖，返回绝对落点；无有效覆盖返回 None。

    ε-13（§6.6）：提取成独立函数，`_resolve_data_dir()` 与 `datadir_for()` 共用 ——
    此前只有前者读标记 JSON、后者写死「程序目录/ComfyMediaViewer_Data」，
    口径分叉的后果：切到便携模式（自定义 dir）后，`/api/sysinfo` 报的 targets、
    前端弹窗显示的目标路径全是错的（显示默认落点，实际数据去了自定义目录），
    用户再点一次「切回去」时算出的 dst 也不是真正生效的那份。
    """
    try:
        cfg = json.loads(PORTABLE_FLAG.read_text("utf-8") or "{}")
        if isinstance(cfg, dict):
            raw = str(cfg.get("dir") or "").strip() or None
            if raw:
                target = Path(raw)
                if not target.is_absolute():
                    target = _exe_dir() / target
                return target
    except (OSError, ValueError):
        pass                      # 标记存在但内容不是 JSON：仍按便携处理，落点取默认
    return None


def _resolve_data_dir():
    """返回 (数据目录, 模式)。标记文件里可以写 {"dir": "..."} 覆盖落点（相对路径按程序目录算）。"""
    if PORTABLE_FLAG.is_file():
        target = _portable_flag_dir() or (_exe_dir() / PORTABLE_DATA_NAME)
        return target, "portable"
    return _user_data_dir(), "user"


DATA_DIR, DATA_MODE = _resolve_data_dir()      # 进程内唯一真相；改它必须重启

# ---- 跨进程数据锁（第 24 级 · B1-P2-1 / B7-P2-8）---------------------------
# 为什么需要：进程内 FAV_LOCK / TAGS_LOCK 只保证**同一进程的线程**不交错。
# 只要有两个进程（桌面壳内后端 + 用户又双击独立版）指向同一数据目录，
# 「读-改-写」就会整表覆盖：
#     A 读 {x} → B 读 {x} → A 写 {x,a} → B 写 {x,b}      ← a 静默丢失
# `_save_json` 的 tmp + fsync + replace 只保证**文件不写坏**，不保证**更新不丢**
# —— 这两件事必须分开看，本锁修的是后者。
#
# 设计（详见 _artifacts/reports/第24级-跨进程锁设计-20261019.md）：
#   · 粒度：单一 `DATA_DIR/.data.lock`。按文件分锁会让「删一张图同时改
#     favs+tags+marks+trash」变成多锁顺序问题，写反一次就是偶发死锁。
#   · 原语：`msvcrt.locking` 非阻塞 + 退避重试。内核在进程退出时自动释放 ⇒
#     不会像 O_EXCL 锁文件那样留下 stale 锁（也就没有「拿 pid 当身份」的坑）。
#   · 层次：`_data_lock()`（跨进程）→ FAV_LOCK/TAGS_LOCK/…（进程内）→ 读改写。
#     顺序固定，不可反向（反向会形成 ABBA）。
#   · 失败：等 3s，拿不到就**明确失败**，绝不静默丢更新。
DATA_LOCK_FILE = DATA_DIR / ".data.lock"
DATA_LOCK_TIMEOUT = 3.0
_DATA_DEPTH = threading.local()


@contextlib.contextmanager
def _data_lock(timeout=DATA_LOCK_TIMEOUT):
    """跨进程数据锁；`with _data_lock() as ok:` —— ok=False 表示超时未拿到。

    同线程可重入（`_DATA_DEPTH` 记深度）：`save_settings` 内部会再取一次锁，
    业务路径里也常有 delete → 清收藏 → 清标记的嵌套。
    """
    depth = getattr(_DATA_DEPTH, "n", 0)
    if depth:
        _DATA_DEPTH.n = depth + 1
        try:
            yield True
        finally:
            _DATA_DEPTH.n = depth
        return

    fh = None
    got = False
    try:
        DATA_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + max(0.1, timeout)
        wait = 0.01
        while True:
            # ⚠️ 整个「打开 + 补 1 字节 + 加锁」必须一起重试：
            #    Windows 上另一个句柄持有 0..1 字节的区域锁时，**open/写入这一步**
            #    也可能直接抛 PermissionError（不只是 msvcrt.locking）。
            #    第一版只把 msvcrt.locking 包进重试 ⇒ 同进程第二个线程当场炸
            #    （实测：并发 tags_add_many 丢一批，报 Errno 13 .data.lock）。
            try:
                fh = open(DATA_LOCK_FILE, "a+b")
                fh.seek(0, os.SEEK_END)
                if fh.tell() == 0:
                    fh.write(b"x")
                    fh.flush()
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if fh is not None:
                    try:
                        fh.close()
                    except OSError:
                        pass
                    fh = None
                left = deadline - time.time()
                if left <= 0:
                    break
                time.sleep(min(wait, left))
                wait = min(wait * 1.5, 0.1)
        if got:
            _DATA_DEPTH.n = 1
        yield got
    finally:
        if got:
            try:
                if os.name == "nt":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            _DATA_DEPTH.n = 0
        if fh is not None:
            try:
                fh.close()
            except OSError:
                pass


class DataLockTimeout(RuntimeError):
    """跨进程数据锁超时（另一实例长时间持锁）。

    单独一个异常类型，是为了让 HTTP 层能把它翻译成 **409 + 可读文案**，
    而不是落进 500 兜底（对外只剩 internal error）。**绝不静默丢更新。**
    """


@contextlib.contextmanager
def _store_lock(lock, what):
    """跨进程锁 + 进程内锁的组合（顺序固定：先跨进程，后进程内）。

    用法：把原来的 `with FAV_LOCK:` 换成 `with _store_lock(FAV_LOCK, "收藏"):`
    —— 块内代码与缩进都不用动，这样「读 → 改 → 写」整段自然被跨进程锁包住。

    ⚠️ 只在 `save_xxx()` 里加跨进程锁**等于没修**：
        A 读 → B 读 → A 写 → B 写，仍会整表覆盖。
    """
    with _data_lock() as ok:
        if not ok:
            log("[warn] 数据锁超时 %.1fs：%s（可能有另一个实例正在写入）"
                % (DATA_LOCK_TIMEOUT, what))
            raise DataLockTimeout("另一个 ComfyMediaViewer 实例正在写入%s，请稍后重试" % what)
        with lock:
            yield


THUMB_DIR = DATA_DIR / "thumbs"
FAV_FILE = DATA_DIR / "favorites.json"
TAGS_FILE = DATA_DIR / "tags.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
TRASH_DIR = DATA_DIR / "trash"
TRASH_KEEP_DAYS = 30          # 回收站条目保留天数，超龄启动时自动清理
# ---- 单图查看（V2.10.7）--------------------------------------------------
# 「打开一张图片」把外部图片（别人发来的原图）复制进这个目录，再走既有的
# /api/file、/api/thumb、/api/meta 端点。之所以落盘而非留在内存：预览/缩略图/
# 下载全部按「路径」取文件，纯内存态得另开一套并行数据通道；落盘后只要让
# inside_root() 放行该目录，整条链路零改动复用。
SOLO_DIR = DATA_DIR / "solo"
SOLO_MAX_BYTES = 64 << 20     # 单张上限（ComfyUI 4K 放大图常见 20~40MB）
SOLO_KEEP_MAX = 20            # 暂存上限张数；超出按 mtime 从旧删。同图同内容按
                              # 哈希复用同名文件，反复打开同一张不会累积。
# ε-12（§6.3）：POST body 全局上限。此前各端点 `rfile.read(Content-Length)` 无上限，
# 恶意/异常客户端可声明 Content-Length: 10G 让读循环把内存吃爆。最重的合法 body
# 是 meta 批量写入 / 批量导出的路径列表 JSON，几十 MB 绰绰有余 ⇒ 64MB 封顶。
MAX_BODY_BYTES = 64 << 20
REMEMBER_ROOTS = True         # 是否把根目录写进用户「常用目录」(settings.json)
                              # —— 自动化脚本 / 回归测试必须以 --no-remember 启动：
                              # 否则每次用临时目录起服务都会占一个常用目录名额（上限 8 个），
                              # 几次之后就把用户真实的 output 挤没了（V2.9.2 踩过）。
TRASH_LOCK = threading.Lock()
SETTINGS_LOCK = threading.RLock()   # settings.json 读-改-写串行化（可重入：save_settings 内部再取一次）
# 删除去向（设置项，存 settings.json）：app=应用内回收站 / system=Windows 回收站
DELETE_MODES = ("app", "system")
# 单次 /api/delete 的路径上限。真实图库上万张、列表视图「全选当前视图」很容易越过，
# 所以这个值刻意给得很宽（它只是防呆，不是分页）；真被截断时被丢弃的路径会并入
# failed 回报给前端 —— 详见 do_POST 里 /api/delete 那段（P1-1）。
DELETE_MAX_PATHS = 20000
# 仅测试用：允许用环境变量把上限压小，好复现「超限截断」这条分支（见 _delete_max_paths）。
try:
    DELETE_MAX_PATHS = int(os.environ.get("CMV_TEST_DELETE_MAX") or DELETE_MAX_PATHS)
except ValueError:
    pass

# O3（2026-10-28）：批量删除时回收站索引的**分块落盘**粒度。
# 逐条 trash_move 是「一文件 = 一次整份 index.json 的 load + save」（O(N²)），删 500 个
# 在回收站已有 1 万条时要 7.5s（实测）。改成攒一批文件、锁一次、每 200 条落一次盘。
# 为什么按块而不是「全部成功再落一次」：某块写失败（磁盘满）只回滚**这一块**的文件，
# 前面的块照常算已删 —— 否则「磁盘满」会把几百个已经搬走的文件全退回去，用户白点。
TRASH_BATCH_CHUNK = 200
# 仅测试用：把块调小，好逼出「第 2 块落盘失败 ⇒ 只回滚这一块」这条分支。
try:
    TRASH_BATCH_CHUNK = max(1, int(os.environ.get("CMV_TEST_TRASH_CHUNK") or TRASH_BATCH_CHUNK))
except ValueError:
    pass

# P1-3：回收站保留天数的环境变量覆盖（0=关闭自动清理）。解析在 load_trash_keep_days()，
# 那里是权威；这里只做一次「提前暴露」——启动早期就把非法值挡掉并记一条日志。
TRASH_KEEP_DAYS_ENV = os.environ.get("CMV_TRASH_KEEP_DAYS")

STATE = {"root": None}
STATE_LOCK = threading.Lock()
FAV_LOCK = threading.Lock()
TAGS_LOCK = threading.Lock()


# ---------------------------------------------------------------- utilities

MAX_TEXT_BYTES = 64 << 20   # 单个文本元数据块解压上限（防 zTXt/iTXt 解压炸弹）
META_BATCH_MAX = 3000       # 筛选面板现场解析上限（索引命中时不受此限）


def _tmp_tag() -> str:
    """临时文件名标签：PID.线程号。

    ε-13（§6.6）：此前只带线程号不带 PID。线程号在 Windows 上是递增分配的、
    且不同进程经常撞号 —— 两个 CMV 进程（如重启竞态期间新旧并存）同时写
    favorites.json 时，旧进程的 `.123.tmp` 和新进程的 `.123.tmp` 是**同一个文件名**，
    后写者会截断先写者，再各自 replace 成正式文件 ⇒ 数据悄悄损坏。
    加上 PID 后每个进程的临时名必然不同，跨进程互斥成立。
    """
    return "%d.%d" % (os.getpid(), threading.get_ident())


# ε-13（§6.6）：回环请求**必须**绕过系统代理。本机若设了 HTTP_PROXY（用户环境
# 实测 http://127.0.0.1:4455），urllib 默认会把 127.0.0.1 的请求也交给代理 ——
# 代理不转发或对死端口回 502，单实例唤起 / CDP / ComfyUI 通信全部失灵，
# 症状还是「时好时坏」，极难排查。no_proxy 环境变量不归我们管（用户可能没设），
# 所以对 localhost 一律用这个显式无代理 opener。
_OPENER_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def load_favs() -> dict:
    """收藏表：{绝对路径: {"t": 时间戳, "cat": 分类名}}；兼容旧格式 {路径: 时间戳}。

    P1-6：单条记录的转换**必须**自己兜住异常。此前 `int(v["t"])` 在 try 之外——
    文件里只要有一条 `{"t": "??"}` / `{"t": null}` / `{"t": {}}`（手改、别的工具写坏、
    旧版本留下的脏值），整个 load_favs 就在 `int()` 上抛 TypeError/ValueError，
    而调用点全在请求线程里 ⇒ **整个服务起不来**（连首页都打不开），且报错只有一行
    "int() argument must be..."，看不出是哪个文件坏了。现在坏的那条跳过、其余照读。
    """
    # B1-P2-9（2026-10-19）：走 `_cached_json`（按 mtime_ns+size 失效）。
    # 此前是每次 `read_text + json.loads`，而 `load_favs` 在逐条循环里被调 ——
    # 上万次系统调用，/api/list 端到端 2.6s 里绝大部分是这个开销。
    # 注意：`_cached_json` 返回浅拷贝，但下面会重新构造 `out`（新 dict + 新值），
    # 不会把缓存对象就地改脏。
    data = _cached_json(FAV_FILE, {})
    if not isinstance(data, dict):
        return {}
    out = {}
    dropped = 0
    if isinstance(data, dict):
        for k, v in data.items():
            try:
                if isinstance(v, dict) and v.get("t"):
                    out[str(k)] = {"t": int(v["t"]), "cat": str(v.get("cat") or "默认")}
                elif isinstance(v, (int, float)) and not isinstance(v, bool):
                    out[str(k)] = {"t": int(v), "cat": "默认"}
                else:
                    # ε-13（§6.6）：t=0 / 缺失也是「坏条目」，丢弃但不许静默 ——
                    # 用户看到收藏少了一条，得能在日志里找到原因。
                    dropped += 1
                    log(f"[warn] 收藏记录无效（t 缺失或为 0），已跳过：{k!r}")
            except (TypeError, ValueError):
                dropped += 1
                log(f"[warn] 收藏记录损坏，已跳过：{k!r}")
    if dropped:
        log(f"[warn] favorites.json 共丢弃 {dropped} 条无效记录")
    return out


class WriteError(Exception):
    """写盘失败。

    收藏 / 标签的保存此前返回 False 却没人检查，界面照常弹「已收藏」，
    刷新后数据就没了（磁盘只读 / 文件被占用 / 路径失效）。
    改成异常后由 do_POST 的兜底转成 500 + error，前端才会如实提示失败。
    """


def _need(ok: bool, what: str) -> bool:
    """写盘返回值必须检查：失败就抛，绝不假装成功。"""
    if not ok:
        raise WriteError(f"{what}写入失败（磁盘不可写或文件被占用）")
    return True


# P1-2 测试注入用；正常运行为 None，见 save_favs / _restore_meta。
# ε-4：允许**启动时**由环境变量置位（沿用 CMV_TEST_* 惯例）—— 否则进程外无法让
# 「/api/delete 的元数据回写」失败，那条静默路径（文件删了、收藏没清干净）就永远验不到。
_FAIL_SAVE_AT = os.environ.get("CMV_TEST_FAIL_SAVE_AT") or None


# ---------------------------------------------------------------------------
# B1-3（2026-10-09 全代码审计）：「数据目录写不进去」必须能被界面看见。
#
# 病灶：`save_favs` / `save_tags` / `_save_settings_locked` 全都把落盘失败
#   `return False` + 打一行日志就完了；而 `add_root()`（换根与启动都调用）与
#   `prune_favs()` 干脆**不看返回值**。于是数据目录不可写（便携模式指向只读 U 盘 /
#   写保护 SD 卡 / 外接盘掉线 / JSON 被杀软或编辑器占用）时，常用目录、上次目录、
#   删除偏好全都「看着改了、重启回原样」，用户界面上**没有任何提示** —— 日志只写在
#   数据目录里的 log 文件里，而那个目录恰好写不进去。
#
# 处置：① 把「最近一次写盘失败」做成进程级信号，由 `/api/info` 带出去，设置页红字告警；
#      ② 启动时对数据目录做一次可写自检（写一个探针文件再删掉）；
#      ③ 调用方（`/api/root`、`/api/prefs`）拿到失败必须如实回传，不再把
#         「写盘失败」和「值非法」混成同一句 400。
_DATA_WRITE = {"ok": True, "at": 0.0, "what": "", "error": ""}
_DATA_WRITE_LOCK = threading.Lock()
# 启动自检结果：None=还没测 / True=可写 / False=不可写
_DATA_DIR_PROBE = [None]


def _note_write_fail(what: str, ex) -> None:
    """记下「最近一次数据写盘失败」（供 /api/info 上报）。"""
    with _DATA_WRITE_LOCK:
        _DATA_WRITE.update({"ok": False, "at": time.time(), "what": what,
                            "error": f"{type(ex).__name__}: {ex}"})


def _note_write_ok() -> None:
    """写盘成功时复位信号（只在曾经失败过时记时间，避免每次都动锁）。"""
    with _DATA_WRITE_LOCK:
        if not _DATA_WRITE["ok"]:
            _DATA_WRITE.update({"ok": True, "at": time.time(), "what": "", "error": ""})


def probe_data_dir() -> bool:
    """启动自检：数据目录是否真的可写（**建目录 + 写探针 + 删探针**）。

    只做 `os.access(DATA_DIR, os.W_OK)` 是不够的 —— Windows 上写保护介质与
    被占用的目录都可能让 access 报真而实际写入失败。真写一次才算数。
    """
    ok = False
    probe = None
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / f".write-probe.{_tmp_tag()}"
        probe.write_text("ok", "utf-8")
        ok = True
    except OSError as ex:
        log(f"[warn] 数据目录不可写（{DATA_DIR}）：{type(ex).__name__}: {ex} —— "
            f"设置 / 收藏 / 标签 / 索引都会「看着改了、重启回原样」，"
            f"界面会在设置页显式告警（请检查该目录是否只读 / 所在盘是否掉线）")
        _note_write_fail("数据目录自检", ex)
    finally:
        if probe is not None:
            try:
                probe.unlink()
            except OSError:
                pass
    _DATA_DIR_PROBE[0] = ok
    return ok


def data_write_state() -> dict:
    """数据目录写入状态（启动自检 + 最近一次实际写盘结果）。"""
    with _DATA_WRITE_LOCK:
        st = dict(_DATA_WRITE)
    st["dir_writable"] = _DATA_DIR_PROBE[0]
    return st


def save_favs(favs: dict):
    try:
        # ⚠️ 注入开关只对「恢复回写」生效（_FAIL_SAVE_AT 由 _restore_meta 置位、返回前复位），
        # 不用环境变量全局生效 —— 全局生效会把「设置收藏」这一步本身也打挂，
        # 于是回收站条目里根本没有收藏快照，还原时无事可做，永远复现不出 partial。
        if _FAIL_SAVE_AT == "favs":
            raise OSError("测试注入：save_favs 指定失败")
        FAV_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = FAV_FILE.with_suffix(f".{_tmp_tag()}.tmp")
        # 4.1-2（2026-11-05 审计）：write_text + replace 只保证「要么旧要么新」，
        # 不保证**新内容真的落盘** —— 掉电后可能只剩目录项、数据块没写，读回半截
        # JSON 会被当成空表。收藏是用户手点出来的，必须 flush + fsync 再换名。
        with io.open(tmp, "w", encoding="utf-8") as _fh:
            _fh.write(json.dumps(favs, ensure_ascii=False, indent=1))
            _fh.flush()
            os.fsync(_fh.fileno())
        tmp.replace(FAV_FILE)
        _invalidate_json(FAV_FILE)           # B1-P2-9：load_favs 走缓存 ⇒ 写后必须失效
        _note_write_ok()                     # B1-3：写成功即复位告警信号
        return True
    except Exception as ex:
        log(f"[warn] 收藏写入失败: {type(ex).__name__}: {ex}")
        _note_write_fail("收藏", ex)          # B1-3：不再只留一行日志
        return False


def set_fav(path: str, cat: str = "默认", on=None) -> bool:
    """显式收藏 / 取消收藏 / 切换，返回**最终**是否已收藏。

    B9-P2-3（2026-10-18）：`/api/fav` 原先只有「切换」这一种动作，于是调用方写
    `{"op":"remove"}`（本意取消）时实际执行的是反方向切换 —— 已收藏的被取消、
    没收藏的被加上，且没有任何错误信号。现在补 add / remove 两个**显式语义**：
      · on=True  已收藏则原样不动（幂等，不重置时间戳与分类）
      · on=False 未收藏则原样（幂等）
      · on=None  保留原来的切换语义（前端点星标走这条）
    """
    with _store_lock(FAV_LOCK, "收藏"):
        favs = load_favs()
        if on is None:
            want = path not in favs
        else:
            want = bool(on)
            if want and path in favs:
                return True
            if not want and path not in favs:
                return False
        if want:
            favs[path] = {"t": int(time.time()), "cat": cat or "默认"}
        else:
            favs.pop(path, None)
        _need(save_favs(favs), "收藏")
    return want


def toggle_fav(path: str, cat: str = "默认") -> bool:
    return set_fav(path, cat, None)


def set_fav_cat(path: str, cat: str):
    """修改已收藏条目的分类；未收藏返回 None。"""
    with _store_lock(FAV_LOCK, "收藏分类"):
        favs = load_favs()
        if path not in favs:
            return None
        favs[path]["cat"] = cat or "默认"
        _need(save_favs(favs), "收藏分类")
        return True


def fav_cats() -> list:
    """分类列表 [{name, count}]，按数量降序；预建分类（0 条）也保留显示。"""
    cnt = {}
    for v in load_favs().values():
        c = v.get("cat") or "默认"
        cnt[c] = cnt.get(c, 0) + 1
    for name in _preset_cats():          # 预建分类持久化：新建后不挂图也不丢
        if name not in cnt:
            cnt[name] = 0
    return [{"name": k, "count": n} for k, n in
            sorted(cnt.items(), key=lambda x: -x[1])]


def _preset_cats() -> list:
    """settings.json 里持久化的预建分类名。"""
    return [c for c in load_settings().get("fav_cats", [])
            if isinstance(c, str) and c.strip()]


# 分类名上限（B9-P2-9，2026-10-18）。32 = 左栏一行放得下的经验值，同时挡住
# 「5000 字分类名」这种把 settings.json 与分类区一起撑爆的输入。
CAT_NAME_MAX = 32


def _norm_cat(name: str) -> str:
    """分类名归一：去首尾空白，内部连续空白压成一个空格。"""
    return re.sub(r"\s+", " ", (name or "").strip())


def add_preset_cat(name: str) -> bool:
    """预建分类并持久化；重名（含已有收藏的分类）返回 False。

    ε-13（§6.6）：判重（读）与写覆盖此前是「锁外读、锁内整体覆盖」——
    两个并发 add 都读不到对方，后写者把先写者的新分类整个抹掉。
    读也必须放进锁里，读-改-写才算原子。

    B9-P2-9（2026-10-18）：长度与控制字符在这里也挡一道 —— HTTP 层已经会先给
    400，但 `add_preset_cat` 还有别的调用方（迁移 / 桥接），唯一入口才可靠。
    """
    name = _norm_cat(name)
    if not name or len(name) > CAT_NAME_MAX:
        return False
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return False
    with _store_lock(SETTINGS_LOCK, "收藏分类"):
        cats = _preset_cats()
        if name in cats or any(v.get("cat") == name for v in load_favs().values()):
            return False
        cats.append(name)
        s = load_settings()
        s["fav_cats"] = cats
        save_settings(s)
    return True


def del_preset_cat(name: str) -> bool:
    """删除预建分类；该分类下仍有收藏时拒绝（先清空分类再删）。

    兼容保留：`/api/fav` 的 `del_cat` 已改走 `delete_fav_cat(name, "reject")`，
    行为与这里完全一致（非空拒绝、空分类删除）。桥接脚本若直接 import 本函数仍可用。
    """
    with _store_lock(SETTINGS_LOCK, "收藏分类"):
        cats = _preset_cats()
        if name not in cats:
            return False
        if any(v.get("cat") == name for v in load_favs().values()):
            return False
        cats.remove(name)
        s = load_settings()
        s["fav_cats"] = cats
        save_settings(s)
    return True


# ---- 分类管理（重命名 / 删除）· 2026-11-08 -------------------------------
# 背景：分类此前只有「新建」入口。左栏的分类既改不了名也删不掉 —— 界面上唯一
# 能做的是一张一张把图挪到别的分类，而 `del_preset_cat` 又只允许删空分类，
# 于是存量分类（尤其 0 条的预建名）会永久堆在左栏。
#
# ⚠️ 数据模型：分类**不是一张独立的表**，它是 favorites.json 里每条 `cat` 的
#    聚合视图；settings.json 的 `fav_cats` 只记「0 条的预建分类名」（新建后
#    不挂图也不丢）。所以：
#      · 改名 = 批量改 has 这些条目的 cat 字段（撞名即合并，无需额外规则）
#      · 删除 = 先决定这些条目怎么办（归入默认 / 一并取消收藏），再清预建名
#    两处写盘**不嵌套**：`_store_lock` 内的跨进程锁是普通 Lock（不可重入），
#    嵌套会自己等自己。代价是极端情况下（改完 favs、写 settings 前进程被杀）
#    会留下一个 0 条的同名预建分类 —— 用户再删一次即可，不会丢图。

def _fav_cat_hits(favs: dict, name: str) -> list:
    """该分类下的收藏路径。缺失 cat 的旧条目按「默认」算（与 load_favs 同口径）。"""
    return [k for k, v in favs.items() if (v.get("cat") or "默认") == name]


def _norm_cat_or_reason(name: str) -> tuple:
    """分类名校验（与 add_preset_cat / set_cat 同口径）。返回 (归一后的名字, 错误原因)。"""
    name = _norm_cat(name)
    if not name:
        return "", "分类名不能为空"
    if len(name) > CAT_NAME_MAX:
        return "", "分类名最多 %d 个字符" % CAT_NAME_MAX
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return "", "分类名不能包含换行等控制字符"
    return name, ""


def rename_fav_cat(old: str, new: str) -> dict:
    """收藏分类改名；新名已存在则**合并**（而不是报错）。

    返回 {"ok": bool, "moved": int, "merged": bool, "reason": str}。
    `moved` 是跟着改名走的收藏条数；`merged=True` 表示并进了已有分类。
    """
    old, bad = _norm_cat_or_reason(old)
    if bad:
        return {"ok": False, "moved": 0, "merged": False, "reason": bad}
    new, bad = _norm_cat_or_reason(new)
    if bad:
        return {"ok": False, "moved": 0, "merged": False, "reason": bad}
    if old == "默认":
        # 「默认」是未分类条目的兜底容器：改名等于把它们一并塞进某个分类，
        # 用户并没有表达过这个意图，且改完就没有「未分类」可用了。
        return {"ok": False, "moved": 0, "merged": False,
                "reason": "「默认」是兜底分类，不能改名"}
    if old == new:                       # 幂等：同一次点击重发不算失败
        return {"ok": True, "moved": 0, "merged": False, "reason": ""}
    hits: list = []
    merged = False
    with _store_lock(FAV_LOCK, "收藏分类"):
        favs = load_favs()
        merged = (new == "默认"
                  or new in _preset_cats()
                  or bool(_fav_cat_hits(favs, new)))
        hits = _fav_cat_hits(favs, old)
        # 空分类同样可以改名 —— 它只存在于 fav_cats 预建表里
        if not hits and old not in _preset_cats():
            return {"ok": False, "moved": 0, "merged": False,
                    "reason": "分类「%s」不存在或已被删除" % old}
        if hits:
            for k in hits:
                favs[k]["cat"] = new
            _need(save_favs(favs), "收藏分类")
    # 预建表（0 条分类）单独走一次：见上面「两处写盘不嵌套」的说明
    with _store_lock(SETTINGS_LOCK, "收藏分类"):
        cats = _preset_cats()
        if old in cats:
            cats.remove(old)
            # 空分类改名后仍要能以新名字存在；有图的分类不需要预建名
            if not hits and new != "默认":
                cats.append(new)
            s = load_settings()
            s["fav_cats"] = cats
            _need(save_settings(s), "收藏分类")
    return {"ok": True, "moved": len(hits), "merged": merged, "reason": ""}


DEL_CAT_MODES = ("reject", "move", "unfav")


def delete_fav_cat(name: str, mode: str = "reject") -> dict:
    """删除收藏分类。

    mode：
      reject（默认/旧口径）—— 只允许删空分类；有收藏一律拒绝并回报条数
      move             —— 分类下的收藏改挂「默认」再删分类（不丢收藏）
      unfav            —— 分类下的收藏**一并取消收藏**（真的从收藏表移除）
    返回 {"ok", "moved", "removed", "count", "reason"}。
    """
    name, bad = _norm_cat_or_reason(name)
    if bad:
        return {"ok": False, "moved": 0, "removed": 0, "count": 0, "reason": bad}
    if name == "默认":
        return {"ok": False, "moved": 0, "removed": 0, "count": 0,
                "reason": "「默认」是兜底分类，不能删除"}
    if mode not in DEL_CAT_MODES:
        return {"ok": False, "moved": 0, "removed": 0, "count": 0,
                "reason": "未知的处理方式：%s" % mode}
    moved = removed = 0
    with _store_lock(FAV_LOCK, "收藏分类"):
        favs = load_favs()
        hits = _fav_cat_hits(favs, name)
        if hits and mode == "reject":
            return {"ok": False, "moved": 0, "removed": 0, "count": len(hits),
                    "reason": "分类下还有 %d 张收藏，请先选择它们的去向" % len(hits)}
        if not hits and name not in _preset_cats():
            return {"ok": False, "moved": 0, "removed": 0, "count": 0,
                    "reason": "分类「%s」不存在或已被删除" % name}
        if hits:
            if mode == "move":
                for k in hits:
                    favs[k]["cat"] = "默认"
                moved = len(hits)
            else:
                for k in hits:
                    favs.pop(k, None)
                removed = len(hits)
            _need(save_favs(favs), "收藏分类")
    with _store_lock(SETTINGS_LOCK, "收藏分类"):
        cats = _preset_cats()
        if name in cats:
            cats.remove(name)
            s = load_settings()
            s["fav_cats"] = cats
            _need(save_settings(s), "收藏分类")
    return {"ok": True, "moved": moved, "removed": removed,
            "count": moved + removed, "reason": ""}


# ---- 悬空条目清理（收藏 / 标签共用的安全悬空判定）-----------------------
# ⚠️ P1-5：这一族函数是「启动即执行、无备份、无撤销」的整批删除，判定错了用户的
#   收藏就成批归零。原实现用 Path(k).is_file() 判悬空，而 is_file() 内部**吞掉
#   OSError 返回 False**（Python 3.8+ 语义），所以外层那个 `except OSError` 形同虚设：
#     · root 在外接盘 / 网络盘上，启动瞬间盘还没就绪 → 全部收藏被判悬空；
#     · root 下有当前进程无权遍历的目录 → 文件其实存在，被判悬空；
#     · 路径超过 260 字符且未开 LongPathsEnabled → 同上；
#     · 断链的符号链接 / junction → 文件本体可能还在别处。
#   三条防线：
#     ① 只对「确定不存在」（FileNotFoundError / NotADirectoryError）判悬空，
#        其余 OSError（权限、设备未就绪、路径过长）一律跳过；
#     ② 安全阀：root 根本读不到、或本次判定的悬空比例过高时**放弃清理并记日志**；
#     ③ 删除前把整份原表备份成 *.bak，用户/我们能据此恢复。
PRUNE_MAX_RATIO = 0.3      # 大样本（≥ PRUNE_RATIO_MIN 条）时，一次删超过这个比例就停下
PRUNE_MAX_ABS = 200        # 或绝对条数超过这个数也停下
PRUNE_RATIO_MIN = 20       # 比例判据的生效下限：样本太少时比例没有统计意义
PRUNE_SMALL_ABS = 8        # 小样本（< 20 条）时，一次最多清这么多条


def _volume_anchor(path: str):
    """路径所属的「卷根」：盘符根（`C:\\`）或 UNC 共享根（`\\\\host\\share\\`）。取不到返回 None。"""
    try:
        p = os.path.abspath(path)
    except (OSError, ValueError):
        return None
    drive, rest = os.path.splitdrive(p)
    if drive:
        return drive + os.sep
    if rest.startswith("\\\\"):
        seg = [x for x in rest.lstrip("\\").split("\\") if x]
        if len(seg) >= 2:
            return "\\\\" + seg[0] + "\\" + seg[1] + os.sep
    return None


def _anchor_online(path: str, memo: dict = None) -> bool:
    """该路径所在卷的卷根**当前是否可访问**（memo 按卷根缓存，一次清理只探一次）。"""
    a = _volume_anchor(path)
    if a is None:
        return False          # 连卷根都取不出来 ⇒ 不敢判悬空
    if memo is not None and a in memo:
        return memo[a]
    try:
        os.scandir(a).close()
        ok = True
    except OSError:
        ok = False
    if memo is not None:
        memo[a] = ok
    return ok


def _definitely_missing(path: str, _memo: dict = None) -> bool:
    """只有当路径**确定不存在**时才返回 True；不可判定（盘未挂载 / 权限 / 设备未就绪）返回 False。

    P0-18：在 Windows 上「盘不在」与「文件真删了」都抛 `FileNotFoundError`
    （实测 winerror 3 / 53 / 67 三种全走这里），**仅凭异常类型无法区分** —— 于是外接盘
    掉线时收藏会被判成悬空**永久删除**（不进回收站，`.bak` 只有一代）。所以判悬空前
    先独立取证「这个路径所在的卷还在不在」：卷根都扫不动就一律保守留下。
    代价是每个候选多一次 `os.scandir`（按卷根 memo，9700 条仍是毫秒级）。
    """
    if not _anchor_online(path, _memo):
        return False          # 盘未挂载 / UNC 不可达 ⇒ 不可判定，保留
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return True           # 卷在线、路径确实没了 ⇒ 真删了
    except NotADirectoryError:
        return True
    except OSError:
        return False          # 不可判定 ⇒ 保守地留下，绝不当成悬空
    return not stat_mod.S_ISREG(st.st_mode) and not stat_mod.S_ISDIR(st.st_mode)


def _prune_why(why: dict, reason: str, **kw) -> None:
    """给可选出参 `why` 记一条「为什么没清干净」。why 为 None 时什么都不做。"""
    if why is None:
        return
    why["reason"] = reason
    why.update(kw)


def _prune_dangling(entries: dict, root: str, kind: str, why: dict = None) -> list:
    """在 entries 里挑出 root 下「确定已不存在」的键，返回键列表（**不修改** entries）。

    kind 只用于日志文案（收藏 / 标签）。安全阀不通过时返回空列表。

    R13（2026-11-15）：新增可选出参 `why`。此前三种完全不同的情况 ——
    root 不可读、安全阀拦下、真的没有悬空条目 —— 一律返回 `[]`，调用方只能看到
    「清理了 0 条」。对启动时的自动清理无所谓，但对**用户主动点的按钮**就是
    「点了没反应」，说不清是没东西可清还是盘掉线了。`why` 会填
    `{"reason": ok|clean|bad_root|unreadable|abs_guard|ratio_guard|small_guard,
      "gone": 判定悬空数, "mine": 本 root 内的条目数}`。
    """
    try:
        pref = str(Path(root).resolve()).lower().rstrip("\\/")
    except OSError:
        _prune_why(why, "bad_root")
        return []
    # ① root 自己读不到 ⇒ 这个盘/目录现在不可信，一律不清理
    try:
        os.scandir(root).close()
    except OSError as ex:
        log(f"[prune] 跳过{kind}清理：root 当前不可读（{root}）：{type(ex).__name__}: {ex}")
        _prune_why(why, "unreadable")
        return []
    mine, gone = [], []
    memo: dict = {}          # P0-18：卷根可达性按卷缓存，避免逐条重复 scandir
    for k in entries:
        kl = k.lower().rstrip("\\/")
        if kl != pref and not kl.startswith(pref + os.sep):
            continue                       # 只碰 root 内的条目；盘未挂载时不误判别的 root
        mine.append(k)
        if _definitely_missing(k, memo):
            gone.append(k)
    # ② 安全阀：绝对数超限 ⇒ 更可能是环境问题，不是用户真删了这么多
    if len(gone) > PRUNE_MAX_ABS:
        log(f"[prune] 跳过{kind}清理：本次判定悬空 {len(gone)}/{len(mine)} 条，"
            f"超过绝对阈值 {PRUNE_MAX_ABS} 条，疑似盘未就绪 —— "
            f"如需强制清理请手工编辑数据目录里的 JSON")
        _prune_why(why, "abs_guard", gone=len(gone), mine=len(mine))
        return []
    # ③ 比例安全阀：只在样本足够大时才用。小收藏集里「10 条删掉 4 条」完全正常，
    #    拿比例去拦会变成「用户正常操作被拒绝」，比不拦更糟（首轮实测抓到的自己写的 bug）。
    if len(mine) >= PRUNE_RATIO_MIN:
        if len(gone) > max(PRUNE_SMALL_ABS, len(mine) * PRUNE_MAX_RATIO):
            log(f"[prune] 跳过{kind}清理：本次判定悬空 {len(gone)}/{len(mine)} 条，"
                f"超过比例阈值 {int(PRUNE_MAX_RATIO*100)}%，疑似盘未就绪 —— "
                f"如需强制清理请手工编辑数据目录里的 JSON")
            _prune_why(why, "ratio_guard", gone=len(gone), mine=len(mine))
            return []
    # ④ 小样本绝对阀（A04，2026-11-05 审计复核）：`PRUNE_SMALL_ABS` 的注释写的是
    #    「小样本（< 20 条）时，一次最多清这么多条」，但它此前只出现在 ③ 的
    #    `max()` 里 —— 而 ③ 整段以 `len(mine) >= PRUNE_RATIO_MIN` 为前提，
    #    样本少于 20 条时根本进不来 ⇒ 这个阀**恒不生效**（19 条全悬空也照清）。
    #    比例在样本太小时没有统计意义，所以才需要一条与比例无关的固定上限。
    elif len(gone) > PRUNE_SMALL_ABS:
        log(f"[prune] 跳过{kind}清理：本次判定悬空 {len(gone)}/{len(mine)} 条，"
            f"超过小样本阈值 {PRUNE_SMALL_ABS} 条（样本 < {PRUNE_RATIO_MIN} 时"
            f"比例无统计意义，改用固定上限），疑似盘未就绪 —— "
            f"如需强制清理请手工编辑数据目录里的 JSON")
        _prune_why(why, "small_guard", gone=len(gone), mine=len(mine))
        return []
    _prune_why(why, "ok" if gone else "clean", gone=len(gone), mine=len(mine))
    return gone


BAK_KEEP = 5          # 数据文件清理前备份的保留代数（B1-P2-2）


def _backup_json(path: Path) -> str:
    """清理前把数据文件原样备份一份（带时间戳），返回备份路径（失败返回空串）。

    B1-P2-2（2026-10-19）：原来固定写到 `<name>.bak`，**只有一代**。后果是
    「误判清理一次 → 用户还没发现 → 下次启动又清一次」时，第二份备份直接覆盖掉
    出事前的那份，恢复窗口只有一个启动周期。改成按时间戳多代留存 + 只保留最近
    `BAK_KEEP` 份；同时仍更新 `<name>.bak` 作为「最近一次」的快捷入口（旧文档、
    旧习惯都还认它）。
    """
    try:
        if not path.is_file():
            return ""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bak = path.with_name("%s.%s.bak" % (path.name, stamp))
        # 同一秒内清理两次会撞名 ⇒ 加序号，绝不覆盖既有备份
        n = 1
        while bak.exists():
            bak = path.with_name("%s.%s-%d.bak" % (path.name, stamp, n))
            n += 1
        shutil.copy2(str(path), str(bak))
        try:
            shutil.copy2(str(path), str(path.with_suffix(path.suffix + ".bak")))
        except OSError:
            pass                                  # 快捷入口失败不影响多代备份
        _prune_backups(path)
        return str(bak)
    except OSError as ex:
        log(f"[prune] 备份 {path.name} 失败：{type(ex).__name__}: {ex}")
        return ""


def _prune_backups(path: Path, keep: int = BAK_KEEP) -> None:
    """只留最近 `keep` 份带时间戳的备份；`<name>.bak` 不动。"""
    try:
        ones = sorted(path.parent.glob(path.name + ".*.bak"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in ones[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def prune_favs(root: str) -> int:
    """清理 root 下文件已不存在的悬空收藏，返回清理条数。

    只碰 root 内的条目 —— 其他盘 / 其他 root 的收藏在盘未挂载时不误判悬空。
    判定与安全阀见 `_prune_dangling`；写盘前先备份（P1-5）。
    """
    with _store_lock(FAV_LOCK, "收藏"):
        favs = load_favs()
        gone = _prune_dangling(favs, root, "收藏")
        if not gone:
            return 0
        _backup_json(FAV_FILE)
        for k in gone:
            favs.pop(k, None)
        # B1-P2-4（2026-10-19）：原来写盘失败仍 `return len(gone)`，日志同时出现
        # 「本次改动未生效」和「已清理 N 条」—— 排障时后一句会把前一句盖掉，调用方
        # 也分不清「真清了」和「没写进去」。失败一律返回 0，且只留一条日志。
        if not save_favs(favs):
            log(f"[prune] 悬空收藏清理后写盘失败，本次 {len(gone)} 条改动未生效")
            return 0
        log(f"[prune] 已清理 {len(gone)} 条悬空收藏（原表已备份，最近一份为 "
            f"{FAV_FILE.name}.bak）")
    return len(gone)

# ---------------------------------------------------------------- tags(标签)

TAG_SPECIAL = "`~!@#$%^&*()+=[]{}|\\:;\"'<>?,"   # 标签名禁用字符（"/" 作层级分隔）


def _norm_tag(t) -> str:
    """规范化标签名：去首尾空白；禁含特殊字符；层级段用 / 分隔。非法返回空串。"""
    if not isinstance(t, str):
        return ""
    t = "/".join(s.strip() for s in t.split("/") if s.strip())
    if not t or len(t) > 40:
        return ""
    for ch in t:
        if ch in TAG_SPECIAL and ch != "/":
            return ""
    if any(not seg or len(seg) > 16 for seg in t.split("/")):
        return ""
    return t


def load_tags() -> dict:
    """标签表：{绝对路径: [标签...]}；幂等去重、过滤非法名。

    B1-P2-9（2026-10-19）：与 load_favs 同源 —— 改走 `_cached_json`。
    下面会把每个值重新规范化成**新列表**，因此不会就地改到缓存里的对象。
    """
    data = _cached_json(TAGS_FILE, {})
    if not isinstance(data, dict):
        return {}
    out = {}
    dropped = 0
    if isinstance(data, dict):
        for k, v in data.items():
            if not isinstance(v, list):
                dropped += 1
                continue
            seen, ok = [], set()
            for t in v:
                t = _norm_tag(t)
                if t and t not in seen and t not in ok:
                    seen.append(t)
                    ok.add(t)
            if seen:
                out[k] = seen
            else:
                # ε-13（§6.6）：全非法名 / 非列表条目整条丢弃必须留痕（ε-13 口径同 favorites）
                dropped += 1
        if dropped:
            log(f"[warn] tags.json 共丢弃 {dropped} 条无效记录（值非列表或标签全部非法）")
    return out


def save_tags(tags: dict):
    try:
        TAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TAGS_FILE.with_suffix(f".{_tmp_tag()}.tmp")
        # 4.1-2：同 save_favs —— 必须先 fsync 再 replace，否则掉电读回半截。
        with io.open(tmp, "w", encoding="utf-8") as _fh:
            _fh.write(json.dumps(tags, ensure_ascii=False, indent=1))
            _fh.flush()
            os.fsync(_fh.fileno())
        tmp.replace(TAGS_FILE)
        _invalidate_json(TAGS_FILE)          # B1-P2-9：load_tags 走缓存 ⇒ 写后必须失效
        _note_write_ok()                     # B1-3：写成功即复位告警信号
        return True
    except Exception as ex:
        log(f"[warn] 标签写入失败: {type(ex).__name__}: {ex}")
        _note_write_fail("标签", ex)          # B1-3：不再只留一行日志
        return False


def tags_of(path: str) -> list:
    return list(load_tags().get(path, []))


def tags_set(path: str, tags: list) -> list:
    """整表替换单图标签（前端先读后写）；返回落盘后的标签列表。"""
    clean, seen = [], set()
    for t in (tags or []):
        t = _norm_tag(t)
        if t and t not in seen:
            clean.append(t)
            seen.add(t)
    with _store_lock(TAGS_LOCK, "标签"):
        ts = load_tags()
        if clean:
            ts[path] = clean
        else:
            ts.pop(path, None)
        _need(save_tags(ts), "标签")
    return clean


def tags_add(path: str, tag: str) -> list:
    """贴一个标签（不存在则建）；返回该图全部标签。"""
    tag = _norm_tag(tag)
    if not tag:
        return tags_of(path)
    with _store_lock(TAGS_LOCK, "标签"):
        ts = load_tags()
        cur = ts.get(path, [])
        if tag not in cur:
            cur = cur + [tag]
        ts[path] = cur
        _need(save_tags(ts), "标签")
    return cur


def tags_remove(path: str, tag: str) -> list:
    """摘一个标签（按规范化名匹配）；返回该图剩余标签。"""
    tag = _norm_tag(tag)
    with _store_lock(TAGS_LOCK, "标签"):
        ts = load_tags()
        cur = ts.get(path, [])
        if tag and tag in cur:
            cur = [t for t in cur if t != tag]
            if cur:
                ts[path] = cur
            else:
                ts.pop(path, None)
            _need(save_tags(ts), "标签")
    return list(ts.get(path, []))


def tags_add_many(paths: list, tags: list) -> dict:
    """P1-18：一次请求给多张图贴同一组标签。

    前端的「批量贴标签」原来是 **N×M 次串行 await**（500 次也不罕见），
    既慢又没法如实计数：任何一次失败只能笼统记一笔。这里在**一把锁里**读一次、
    写一次盘，逐张累计成功/失败，前端据此说出真话。
    返回 {"ok": 1/True, "pairs": 成功贴上的 (图,标签) 对数,
          "files": 真正改动的图数,
          "failed": [{"path":..., "error":...}], "tags_all": [...]}。

    P1（2026-10-09）：计数**只走 `pairs`**。原先计数落在 `ok` 上，而 HTTP
    handler 返回前会 `r["ok"] = True` 把它覆盖成布尔值 —— 前端 `j.ok||0`
    恒为 1/true，界面上的「共 true 处新增」与「部分图本来已有该标签」的归因
    全是假的。`ok` 从此只当成功标志，数字另起字段，两个语义不再共用一个键。
    """
    clean, seen = [], set()
    for t in (tags or []):
        t = _norm_tag(t)
        if t and t not in seen:
            clean.append(t)
            seen.add(t)
    uniq = []
    dupes = set()
    for p in (paths or []):
        p = str(p or "")
        if p and p not in dupes:
            uniq.append(p)
            dupes.add(p)
    res = {"ok": 0, "pairs": 0, "files": 0, "failed": []}
    if not clean or not uniq:
        res["tags_all"] = tag_stats()
        return res
    # ⚠️ B1-2（2026-10-09 全代码审计）：落盘**必须留在锁内** —— 与 tags_set / tags_add /
    #    tags_remove 保持同一口径。此前 `_need(save_tags(ts))` 写在 `with TAGS_LOCK:` 之外，
    #    而读取快照到写盘之间是一整表「读-改-写」：两个并发 add_many（批量贴标签连点、
    #    多标签页同时操作；ThreadingHTTPServer 天然并发）各自在锁内拿到自洽快照，
    #    随后**都在锁外**写盘 —— 后落盘者用自己那份完整表覆盖前者，两个请求都返回成功，
    #    实际先完成的那批标签静默丢失（界面显示贴上、刷新后没了）。本函数的 docstring
    #    自己写着「在一把锁里读一次、写一次盘」，实现却与注释不符。
    touched = []                       # F5-P2-5：真正被写过的路径（前端内存同步唯一可信的依据）
    with _store_lock(TAGS_LOCK, "标签"):
        ts = load_tags()
        for p in uniq:
            cur = list(ts.get(p, []))
            added = 0
            for t in clean:
                if t not in cur:
                    cur.append(t)
                    added += 1
            if added:
                ts[p] = cur
                touched.append(p)
                res["ok"] += added
                res["pairs"] += added
                res["files"] += 1
        try:
            _need(save_tags(ts), "标签")
        except Exception as ex:                       # 落盘失败：如实报告，不虚报成功
            res["failed"].append({"path": "", "error": f"写入标签失败：{ex}"})
            res["ok"] = 0
            res["pairs"] = 0
            res["files"] = 0
            touched = []                       # 没落盘就等于一张都没贴上
    # F5-P2-5（2026-10-20）：把**真正写成功**的路径回给前端。此前前端按「选中的路径」
    # 就地补标签，而被删除 / 移出 root 的路径会被上面的 uniq/clean 过滤掉（不进 failed）
    # ⇒ 卡片上显示贴上了、磁盘上一笔都没有。
    res["paths"] = touched
    res["tags_all"] = tag_stats()
    return res


def tag_stats_detail() -> dict:
    """标签计数 + 口径可信度：{"tags": [...], "dangling": N, "unknown": bool}。

    层级子标签计入父级（有「风格/汉服」就算「风格」一次）。

    第 16 级（B7 收尾，2026-10-17）：计数口径此前是**全表全量** —— `tags.json` 里
    每一条都算一个，不管文件还在不在、是不是图片/视频、是不是当前根目录下的。
    而点进去的列表（fav_view / marks_view / tags_filter）走的是「媒体格式 + 文件仍在」，
    于是左栏「风景 12」点进去只有 9 条，用户没法解释，只会认为列表漏了。
    现在两侧用同一套判定（`_tag_alive` → `_is_media_alive` + 根范围）。

    `dangling`：当前根下有标签、但在图库里看不到的条目数（文件已删 / 换成了非媒体文件）。
    它是**给用户看的出口** —— 数字比昨天小了，界面上得有一句话说明为什么。
    `unknown`：根目录当前不可读（外接盘掉线等），**无法判定**哪些文件还在；此时不做过滤，
    计数退回全量口径，前端把数字标成「—」。宁可显示「可能偏大」，也不能把整库标签
    显示成 0 —— 那正是 P0-1 在目录总数上犯过的错。
    """
    ts = load_tags()
    al = _tag_alive(ts)
    cnt = {}
    for p in al["live"]:
        for t in ts.get(p, ()):
            chain = ""
            for s in t.split("/"):
                chain = f"{chain}/{s}" if chain else s
                cnt[chain] = cnt.get(chain, 0) + 1
    return {"tags": [{"name": k, "count": n} for k, n in
                     sorted(cnt.items(), key=lambda x: (-x[1], x[0]))],
            "dangling": al["dangling"], "unknown": al["unknown"]}


def tag_stats() -> list:
    """全部标签计数 [{name, count}] 按计数降序；层级子标签计入父级（风格/汉服 计入 风格）。

    口径见 `tag_stats_detail()`：只统计当前图库里看得到的条目。
    """
    return tag_stats_detail()["tags"]


def tag_rename(from_name: str, to_name: str) -> dict:
    """全局改名（合并）：from→to；to 已存在时 from 的图并入 to。

    A16（2026-11-05 审计复核）：改名必须连**子层级**一起改，与 `tag_delete`
    对称。此前只精确匹配 `f` ⇒ 把「风格」改成「画风」后，图上的「风格/汉服」
    原地不动，而父标签已经不存在了 —— 左栏标签树里只剩一个没有根的孤儿节点，
    用户既点不到父级、也没法再按父级筛选。
    """
    f, t = _norm_tag(from_name), _norm_tag(to_name)
    if not f or not t or f == t:
        return {"ok": False, "error": "无效的标签名"}
    moved = 0
    prefix = f + "/"
    with _store_lock(TAGS_LOCK, "标签"):
        ts = load_tags()
        for path, tags in list(ts.items()):
            if not any(x == f or x.startswith(prefix) for x in tags):
                continue
            merged = []
            for x in tags:
                if x == f:
                    merged.append(t)
                elif x.startswith(prefix):
                    merged.append(t + "/" + x[len(prefix):])   # 子层级跟着父级走
                else:
                    merged.append(x)
            seen, out = set(), []
            for x in merged:                  # 合并后可能出现重复（to 那边原本就有）
                if x not in seen:
                    seen.add(x)
                    out.append(x)
            ts[path] = out
            moved += 1
        for path in [p for p, v in ts.items() if not v]:
            del ts[path]
        _need(save_tags(ts), "标签")
    return {"ok": True, "moved": moved, "to": t}


def tag_delete(name: str) -> dict:
    """全局删除标签：从所有图上摘除（含子层级标签，删「风格」会连带删「风格/汉服」）。"""
    n = _norm_tag(name)
    if not n:
        return {"ok": False, "error": "无效的标签名"}
    prefix = n + "/"
    removed = 0
    with _store_lock(TAGS_LOCK, "标签"):
        ts = load_tags()
        for path, tags in list(ts.items()):
            kept = [x for x in tags if x != n and not x.startswith(prefix)]
            if len(kept) != len(tags):
                removed += 1
            if kept:
                ts[path] = kept
            else:
                ts.pop(path, None)
        _need(save_tags(ts), "标签")
    return {"ok": True, "removed": removed}


def prune_tags(root: str, why: dict = None) -> int:
    """清理 root 下文件已不存在的悬空标签，返回清理条数（判定/安全阀/备份同 prune_favs）。

    R13（2026-11-15）：可选出参 `why` 透传给 `_prune_dangling`，并额外区分
    `save_failed` —— 写盘失败时返回 0，调用方靠 `why` 才能把「没清」和「没写成」
    分开报给用户。
    """
    with _store_lock(TAGS_LOCK, "标签"):
        ts = load_tags()
        gone = _prune_dangling(ts, root, "标签", why)
        if not gone:
            return 0
        _backup_json(TAGS_FILE)
        for k in gone:
            ts.pop(k, None)
        # 2026-11-05 P2：与 prune_favs 对齐 —— 写盘失败返回 0、只留一条日志。
        #   旧写法失败后仍 `log("已清理 N 条")` 并 `return len(gone)`，排障时后一句
        #   把前一句盖掉，调用方也分不清「真清了」和「没写进去」。
        #   （load_tags 每次都重建新 dict，失败不会污染缓存，也没有「迟到落盘」。）
        if not save_tags(ts):
            log(f"[prune] 悬空标签清理后写盘失败，本次 {len(gone)} 条改动未生效")
            _prune_why(why, "save_failed", gone=len(gone))
            return 0
        log(f"[prune] 已清理 {len(gone)} 条悬空标签（原表已备份为 {TAGS_FILE.name}.bak）")
    return len(gone)


def startup_prune_tags(root: str, delay: float = 25.0):
    """P8（2026-11-15）：启动后清一次悬空标签。

    此前 `prune_tags()` 只在「换根」和用户点左栏「清理」时被调用。于是应用外删图、
    或把图换成别的扩展名之后，那些键会一直躺在 tags.json 里：计数侧（`_tag_alive`）
    已经把它们排除，用户看得到「还有 N 个标签已失效」，但**不点那个按钮就永远不清**。

    为什么延迟 25 秒：启动那一瞬间是磁盘最忙的时候（索引增量/重建、缩略图 GC、
    悬空收藏清理同时在跑），而这件事一点都不急；也让「刚打开就被改了数据」不至于
    发生在首屏还没画完的时候。

    安全边界（全部落在 `prune_tags` → `_prune_dangling` 里，这里不重复实现）：
    ① 根不可读一律不动 —— 外接盘掉线时任何「文件不存在」判断都是假的；
    ② 只碰当前根前缀内的键，别的库一条不动；
    ③ 绝对 / 比例 / 小样本三道阈值，判掉的条数离谱时整批放弃并留日志。
    另外空 root 在这里就挡掉：`Path("")` 会退化成 cwd，绝不能传进判定口。
    """
    if not root:
        return 0
    if delay and delay > 0:
        time.sleep(delay)
    try:
        n = prune_tags(root)
    except Exception as ex:                  # 启动清理失败不许影响任何主流程
        log(f"[warn] 启动清理悬空标签失败: {type(ex).__name__}: {ex}")
        return 0
    if n:
        log(f"[prune] 启动清理：{n} 条悬空标签已移除")
    return n


def tag_prune_view() -> dict:
    """清理当前根下文件已不存在的悬空标签 —— 左栏「N 个标签已失效」的**出口**。

    R13（2026-11-15）：`prune_tags()` 此前只在换根迁移时被调用（`switch_root` 的
    收尾），启动时只清收藏、不清标签。于是「应用外删掉图 / 图被换成 .txt」留下的
    键永久占着 tags.json：计数已按 `_tag_alive` 把它们排除（用户看得到「还有 N 个
    标签已失效」），却没有任何入口能真清掉，只能等换根或手工编辑 JSON。

    三道保护，每一条都对应一种「自动清理能做、按钮不能做」的差别：
    ① **根不可读一律不动**。外接盘掉线时 scandir 就失败，此时任何「文件不存在」的
       判断都是假的，一按钮就能把整库标签清空。直接回 reason="unreadable"。
    ② **只清当前根内的键**。`_prune_dangling` 按 root 前缀过滤，换根之后旧根的键
       一条都不碰 —— 那是另一个库的数据，不是「失效」。
    ③ **安全阀照旧生效**（绝对 / 比例 / 小样本）。拦下时回 reason="*_guard" 与判定
       数，前端据此提示「疑似盘未就绪，稍后再试」，绝不放行。
    """
    root = STATE.get("root") or ""
    if not root:
        return {"ok": False, "reason": "no_root", "removed": 0, "dangling": 0,
                "error": "尚未选择媒体目录"}
    if not _root_readable(root):
        return {"ok": False, "reason": "unreadable", "removed": 0, "dangling": 0,
                "error": "媒体目录当前读不到（外接盘掉线或被移走），为避免误清已跳过"}
    # 清之前的失效数：前端拿它说「本次清理 X 条，还剩 Y 条」。
    # 口径与左栏那个数字**同一个**（`_tag_alive`），否则用户会觉得对不上账。
    before = _tag_alive(load_tags()).get("dangling", 0)
    why = {}
    n = prune_tags(root, why)
    reason = why.get("reason") or "clean"
    if reason in ("abs_guard", "ratio_guard", "small_guard"):
        return {"ok": False, "reason": reason, "removed": 0, "dangling": before,
                "candidates": why.get("gone", 0),
                "error": ("本次判定可清理 %d 条，超过安全阈值，疑似磁盘未就绪；"
                          "请稍后再试（盘挂回来之后数字会回落）") % why.get("gone", 0)}
    if reason == "save_failed":
        return {"ok": False, "reason": reason, "removed": 0, "dangling": before,
                "error": "清理后写盘失败，本次改动未生效（原表未被修改）"}
    # 清完重新算一次：`_tag_alive` 的缓存签名含 (条目数, 键集合) ⇒ 有删就必然重算，
    # 一条没删时读到的就是刚才那份，两种情况下这个数都是当下的真值。
    after = _tag_alive(load_tags()).get("dangling", 0)
    return {"ok": True, "reason": "ok" if n else "clean", "removed": n,
            "before": before, "dangling": after, "tags_all": tag_stats()}


def _tag_match(got: set, w: str) -> bool:
    """单个标签命中：精确命中，或 got 中有以其为父级前缀的标签（风格 命中 风格/汉服）。"""
    if w in got:
        return True
    p = w + "/"
    return any(t.startswith(p) for t in got)


def _tag_hit(got: set, want: set, mode: str) -> bool:
    """命中判定。mode: and=全部命中 / or=任一命中 / not=一个都不含（排除）。

    注意：mode="not" 时无标签的图也应命中（它当然不含被排除的标签）。
    """
    if mode == "not":
        return not any(_tag_match(got, w) for w in want)
    if mode == "and":
        return all(_tag_match(got, w) for w in want)
    return any(_tag_match(got, w) for w in want)


def tags_filter(tags: list, base_path: str = "", mode: str = "and", rec: bool = True,
                exclude: list = None) -> list:
    """按标签过滤：返回目录（含子目录）下命中媒体的 item 列表（同 api_list 形状）。

    mode: and = 全部命中；or = 任一命中；not = 选中标签一个都不含（排除）。
    exclude: 额外的排除标签（始终 AND-NOT，可与 and/or 组合成「有 A 且不含 B」）。
    父级标签命中其全部子标签(风格 命中 风格/汉服)。
    """
    tags = [_norm_tag(t) for t in (tags or [])]
    tags = [t for t in tags if t]
    excl = [_norm_tag(t) for t in (exclude or [])]
    excl = [t for t in excl if t]
    if not tags and not excl:
        return []
    ts = load_tags()
    favs = load_favs()        # 收藏状态：与 search_index / fav_view 口径一致（此前硬编码 False 导致星标丢失）
    want = set(tags)
    notwant = set(excl)
    base_path = base_path or STATE.get("root") or ""
    if not base_path:
        return []                              # B1-P2-5：root 未设置时 `Path(None)` 会抛 TypeError
    try:
        base = str(Path(base_path).resolve())
    except (OSError, ValueError, TypeError):
        return []
    if not os.path.isdir(base):
        return []
    # B1-P2-5（2026-10-19）：`os.walk` 默认**吞掉** scandir 的 OSError（权限不足 /
    # 目录损坏 / 正被别的进程重写）⇒ 整目录的图在筛选结果里凭空消失，界面既不报错
    # 也不提示，用户只会以为「标签没贴对」。这里显式收集并留痕。
    skipped = []
    out = []
    # 2026-11-08：隐藏目录整棵不筛（与索引 / 统计同口径 —— 否则「筛出来 20 张、
    # 网格里只看得见 3 张」）。基准目录本身在隐藏名单里时照常筛，只剪子目录。
    hid = hidden_snapshot()
    if rec:
        walker = os.walk(base, onerror=lambda e: skipped.append(getattr(e, "filename", "")))
    else:
        try:
            walker = [(base, [], os.listdir(base))]
        except OSError:
            skipped.append(base)
            walker = []
    for d, _sub, files in walker:
        # os.walk 默认 topdown ⇒ 就地改 _sub 即剪枝（这一层以下不会再被列举）。
        if _sub and hid:
            _sub[:] = [x for x in _sub if not _hid_hit(hid, os.path.join(d, x))]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext in IMAGE_EXTS:
                t = "image"
            elif ext in VIDEO_EXTS:
                t = "video"
            else:
                continue
            sp = os.path.join(d, fn)
            got = set(ts.get(sp, []))
            if notwant and any(_tag_match(got, w) for w in notwant):
                continue                       # 命中任一排除标签 → 丢弃
            if want and not _tag_hit(got, want, mode):
                continue
            try:
                st = os.stat(sp)
                out.append({"name": fn, "path": sp, "type": t,
                            "size": st.st_size, "mtime": int(st.st_mtime),
                            "playable": t == "image" or ext in PLAYABLE_VIDEO,
                            "png": ext == ".png", "fav": sp in favs,
                            "favcat": favs[sp]["cat"] if sp in favs else None,
                            "tags": sorted(got)})
            except OSError:
                continue
    out.sort(key=lambda x: -x["mtime"])
    if skipped:
        log("[tags_filter] 有 %d 个目录不可读，筛选结果不含其中内容：%s"
            % (len(skipped), "、".join([s for s in skipped if s][:5]) or "(路径未知)"))
    return out

# ---------------------------------------------------------------- 带缓存的 JSON 读盘
#
# 批次 F（F2 / F3）：这些加载器此前一律「read_text + json.loads」，**零缓存**。
# 在逐条循环里被调一次就是上万次系统调用 —— 实测 `/api/list` 端到端 2.6 s、
# 而同样一件事纯计算只要 41 ms，差的就是这里（settings.json 只有 306 B，
# 开销全在 open/read/parse 本身）。统一走这一个工具，把「读盘零缓存」这个
# **类别**消掉，而不是只修今天踩到的那一个。
#
# 缓存键取 (st_mtime_ns, st_size)：比 TTL 准（文件一改就失效），
# 又比秒级 mtime 稳（同一秒内改写也能区分）。
#
# ⚠️ 唯一且致命的陷阱：**写入方落盘后必须主动失效**（`_invalidate_json`），
#    否则「刚改的设置不生效」—— 批次 F 的 F2 负控就是专门钉这一条的。

_JSON_CACHE = {}
_JSON_CACHE_LOCK = threading.Lock()


def _json_fresh_key(path):
    """(mtime_ns, size)；文件不可 stat 时返回 None（调用方按「无缓存」处理）。"""
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _cached_json(path, default=None):
    """按 (mtime_ns, size) 缓存的 JSON 读盘。命中直接返回，未命中才 read + parse。

    返回**浅拷贝**（dict 顶层复制）而不是缓存对象本身：调用方普遍是
    「读 → 改 → 写回」（`save_delete_mode` / `tags_set` / `save_nsfw_*` …），
    把缓存对象交出去会被就地改脏，写盘失败时脏值还会留在缓存里。
    浅拷贝对「顶层增删键」已经够 —— settings.json 的嵌套值（`roots` 列表）
    在本项目里只有**整体替换**（`s["roots"] = roots[:8]`），没有就地改元素。

    解析失败（文件写坏）**不写缓存**：宁可每次重试，也不要把坏值钉住。
    """
    key = _json_fresh_key(path)
    if key is None:
        return default
    with _JSON_CACHE_LOCK:
        hit = _JSON_CACHE.get(str(path))
        if hit is not None and hit[0] == key:
            val = hit[1]
            return dict(val) if isinstance(val, dict) else val
    try:
        val = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default
    with _JSON_CACHE_LOCK:
        _JSON_CACHE[str(path)] = (key, val)
    return dict(val) if isinstance(val, dict) else val


def _invalidate_json(path):
    """落盘之后调用。不调 = 下一个读者拿到旧值（F2 负控钉的就是这一条）。"""
    with _JSON_CACHE_LOCK:
        _JSON_CACHE.pop(str(path), None)


# ---------------------------------------------------------------- settings

def load_settings() -> dict:
    """批次 F（F2）：走 `_cached_json`。

    此前是每次 `read_text` + `json.loads`，而它在 NSFW 逐条判定路径上被调用 ——
    实测一次 `/api/list` 就是 **9726 次读盘**，这也是那 2.6 s 的主因。
    """
    d = _cached_json(SETTINGS_FILE, {})
    return d if isinstance(d, dict) else {}


def save_settings(d: dict):
    with SETTINGS_LOCK:
        return _save_settings_locked(d)


def _save_settings_locked(d: dict):
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(f".{_tmp_tag()}.tmp")
        # 4.1-2：设置里有根目录记忆 / 偏好，掉电丢成半截会直接乱套，同样要 fsync。
        with io.open(tmp, "w", encoding="utf-8") as _fh:
            _fh.write(json.dumps(d, ensure_ascii=False, indent=1))
            _fh.flush()
            os.fsync(_fh.fileno())
        tmp.replace(SETTINGS_FILE)
        _invalidate_json(SETTINGS_FILE)      # F2：写后必须失效，否则读回旧值
        _note_write_ok()                     # B1-3：写成功即复位告警信号
        return True
    except OSError as ex:
        log(f"[warn] 设置写入失败: {ex}")
        _note_write_fail("设置", ex)          # B1-3：换根记忆 / 偏好全靠它
        return False


# ---------------------------------------------------------------- 隐藏文件夹（2026-11-08）
#
# 「含子文件夹」把整棵树的图铺出来，其中总有些子目录用户根本不想在软件里看到
# （备份、临时出图、别人丢进来的物料）。这份名单就是那些目录的清单。
#
# 口径（2026-11-08 与用户确认）：
#   · 存 settings.json，按**根**分组；条目是**相对根**的 posix 串
#     ⇒ 换盘符 / 换机器后同结构目录仍能命中。目录改名则失配 —— 宁可漏隐藏，
#       不要误隐藏（误隐藏会让图「凭空消失」且用户找不出原因）。
#   · **彻底不扫**，不是只在列表里过滤：索引 / 搜索 / 统计 / 查重 / 收藏与标记视图
#     一并跳过。否则「隐藏了但左栏数字不变、搜索照样能搜到」就是假隐藏。
#   · 不做名字规则：同名目录在别处不该被牵连。
#
# 判定 = 绝对路径前缀集合（normcase）+ 祖先链查找。递归遍历里只对**目录**调用 ——
# 目录一旦剪掉，其子孙根本不会进栈，逐文件判定自然省掉了。
HIDDEN_KEY = "hidden_dirs"
HIDDEN_MAX = 200                     # 单根上限：防 settings.json 膨胀 / 判定退化成 O(n)
_HIDDEN_CACHE = {"root": None, "mat": -1.0, "key": frozenset()}
_HIDDEN_LOCK = threading.Lock()


def _hidden_root_key(root: str) -> str:
    """名单里的根键（规范化绝对路径，保留原始大小写便于人工读 json）。"""
    try:
        return os.path.abspath(str(root))
    except (OSError, ValueError, TypeError):
        return ""


def _hidden_rel(p: str, root: str) -> str:
    """绝对路径 → 相对根的 posix 串；不在根内返回 ""。"""
    try:
        rel = os.path.relpath(str(p), str(root))
    except (OSError, ValueError):
        return ""
    if not rel or rel == "." or rel.startswith(".."):
        return ""
    return rel.replace(os.sep, "/")


def _hidden_clean(rel) -> str:
    """条目规范化：反斜杠转正斜杠、去首尾斜杠；非法返回 ""。"""
    if not isinstance(rel, str):
        return ""
    return rel.strip().replace("\\", "/").strip("/")


def hidden_list(root: str = "") -> list:
    """当前根下被隐藏的目录（相对根的 posix 串，原始大小写，按名排序）。"""
    root = root or STATE.get("root") or ""
    if not root:
        return []
    s = load_settings()
    book = s.get(HIDDEN_KEY)
    if not isinstance(book, dict):
        return []
    got = book.get(_hidden_root_key(root))
    if not isinstance(got, list):
        return []
    out, seen = [], set()
    for x in got:
        v = _hidden_clean(x)
        if not v:
            continue
        k = v.casefold()
        if k in seen:
            continue
        seen.add(k)
        out.append(v)
    out.sort(key=lambda x: x.casefold())
    return out[:HIDDEN_MAX]


def hidden_snapshot(root: str = "") -> frozenset:
    """判定快照：normcase 后的隐藏目录绝对路径集合。空集合 = 什么都没隐藏。

    缓存键 = 根 + settings.json 的 mtime：改名单必写盘 ⇒ mtime 变 ⇒ 自动失效，
    不需要额外的失效钩子（也就不会出现「改了名单、进程内还是老值」）。
    """
    root = root or STATE.get("root") or ""
    if not root:
        return frozenset()
    try:
        mat = SETTINGS_FILE.stat().st_mtime
    except OSError:
        mat = 0.0
    with _HIDDEN_LOCK:
        c = _HIDDEN_CACHE
        if c["root"] == root and c["mat"] == mat:
            return c["key"]
    keys = set()
    for rel in hidden_list(root):
        try:
            keys.add(os.path.normcase(os.path.normpath(
                os.path.join(str(root), *rel.split("/")))))
        except (TypeError, ValueError):
            continue
    key = frozenset(keys)
    with _HIDDEN_LOCK:
        _HIDDEN_CACHE.update({"root": root, "mat": mat, "key": key})
    return key


def _hidden_invalidate():
    """写名单后主动清缓存：mtime 精度受限时（同秒内两次写）也能立刻生效。"""
    with _HIDDEN_LOCK:
        _HIDDEN_CACHE.update({"root": None, "mat": -1.0, "key": frozenset()})


def _hid_hit(hid, p) -> bool:
    """p（绝对路径）是否落在隐藏名单的某棵子树内（含该目录自身）。

    hid 为空（绝大多数用户没设名单）时是一次 `if not hid` 就返回 —— 零成本。
    """
    if not hid:
        return False
    try:
        q = os.path.normcase(os.path.normpath(str(p)))
    except (TypeError, ValueError):
        return False
    while q:
        if q in hid:
            return True
        i = q.rfind(os.sep)
        # i <= 0：已到文件系统根；q[i-1] == ":"：已到盘符根（"g:\"）—— 都不可能被隐藏
        if i <= 0 or q[i - 1] == ":":
            return False
        q = q[:i]
    return False


def hidden_hit(p) -> bool:
    """单点判定（收藏 / 最近 / 标记这类散点路径用）。

    递归遍历**不要**这样调（每次都 stat 一次 settings.json）——先
    `hid = hidden_snapshot()` 取一次快照，循环里用 `_hid_hit(hid, p)`。
    """
    return _hid_hit(hidden_snapshot(), p)


def _hidden_write(root: str, rels: list) -> bool:
    """把某根的名单整体写回 settings.json（调用方负责规范化与去重）。"""
    rk = _hidden_root_key(root)
    if not rk:
        return False
    with _store_lock(SETTINGS_LOCK, "隐藏文件夹"):
        s = load_settings()
        book = s.get(HIDDEN_KEY)
        book = dict(book) if isinstance(book, dict) else {}
        if rels:
            book[rk] = list(rels)
        else:
            book.pop(rk, None)          # 空名单不占位：settings.json 里不留残留键
        s[HIDDEN_KEY] = book
        ok = _save_settings_locked(s)   # 已在锁内，不能再走 save_settings（锁不可重入）
    if ok:
        _hidden_invalidate()
    return ok


def hidden_add(path: str, root: str = "") -> dict:
    """把目录加入隐藏名单。path 可为绝对路径，也可为相对根的 posix 串。"""
    root = root or STATE.get("root") or ""
    if not root:
        return {"ok": False, "reason": "尚未设置根目录"}
    sp = str(path or "").strip()
    if not sp:
        return {"ok": False, "reason": "缺少目录路径"}
    rel = _hidden_rel(sp, root) if os.path.isabs(sp) else _hidden_clean(sp)
    if not rel:
        try:
            if (os.path.isabs(sp) and
                    os.path.normcase(os.path.normpath(sp)) ==
                    os.path.normcase(os.path.normpath(root))):
                return {"ok": False, "reason": "不能隐藏根目录本身"}
        except (TypeError, ValueError):
            pass
        return {"ok": False, "reason": "该目录不在当前根目录内"}
    if rel == ".." or rel.startswith("../"):
        return {"ok": False, "reason": "该目录不在当前根目录内"}
    try:
        fp = os.path.join(root, *rel.split("/"))
        if os.path.isfile(fp):
            return {"ok": False, "reason": "这是一个文件，不是文件夹"}
    except (TypeError, ValueError):
        pass
    cur = hidden_list(root)
    if any(x.casefold() == rel.casefold() for x in cur):
        return {"ok": True, "dirs": cur, "changed": False}     # 幂等：重复隐藏不算错
    if len(cur) >= HIDDEN_MAX:
        return {"ok": False, "reason": "隐藏名单已达上限（%d 条）" % HIDDEN_MAX}
    cur.append(rel)
    cur.sort(key=lambda x: x.casefold())
    if not _hidden_write(root, cur):
        return {"ok": False, "reason": "写入设置失败（磁盘不可写或文件被占用）",
                "dirs": hidden_list(root)}
    return {"ok": True, "dirs": cur, "changed": True, "path": rel}


def hidden_remove(path: str, root: str = "") -> dict:
    """从隐藏名单移除一条（也接受「移除某目录下的整棵子树条目」）。"""
    root = root or STATE.get("root") or ""
    if not root:
        return {"ok": False, "reason": "尚未设置根目录"}
    sp = str(path or "").strip()
    if not sp:
        return {"ok": False, "reason": "缺少目录路径"}
    rel = _hidden_rel(sp, root) if os.path.isabs(sp) else _hidden_clean(sp)
    if not rel:
        return {"ok": False, "reason": "该目录不在当前根目录内"}
    cur = hidden_list(root)
    keep = [x for x in cur if x.casefold() != rel.casefold()]
    if len(keep) == len(cur):
        return {"ok": True, "dirs": cur, "changed": False}     # 幂等：本来就不在名单里
    if not _hidden_write(root, keep):
        return {"ok": False, "reason": "写入设置失败（磁盘不可写或文件被占用）",
                "dirs": cur}
    return {"ok": True, "dirs": keep, "changed": True}


def hidden_clear(root: str = "") -> dict:
    """清空当前根的隐藏名单。"""
    root = root or STATE.get("root") or ""
    if not root:
        return {"ok": False, "reason": "尚未设置根目录"}
    cur = hidden_list(root)
    if not cur:
        return {"ok": True, "dirs": [], "changed": False}
    if not _hidden_write(root, []):
        return {"ok": False, "reason": "写入设置失败（磁盘不可写或文件被占用）",
                "dirs": cur}
    return {"ok": True, "dirs": [], "changed": True}


def _hidden_reindex() -> bool:
    """名单变了 ⇒ 索引与统计口径都跟着变，立刻重扫一次。

    刻意**不删** IDX_FILE：`_index_builder` 是按「本次扫到的文件」整体重建表的，
    隐藏掉的条目自然消失、取消隐藏的条目重新被解析。删缓存只会让大库白等几分钟
    （而这一趟本来就是增量：未变的条目按 mtime+size 直接复用）。
    """
    try:
        with IDX_LOCK:
            IDX_GEN[0] += 1          # 在跑的这一代作废，免得它带着旧名单的结果写回
            IDX_THREAD[0] = None     # 允许 ensure_index 起新线程
            IDX["built_at"] = 0.0    # 绕过 REINDEX_INTERVAL 节流
        census_invalidate()          # 「全部 / 图片 / 视频」重算
        ensure_index()
        return True
    except Exception as ex:
        log(f"[warn] 隐藏名单变更后重建索引失败: {type(ex).__name__}: {ex}")
        return False


def load_last_root() -> str:
    """上次浏览的根目录（仍存在才返回）。"""
    r = load_settings().get("last_root")
    # P0-15：settings.json 可能被手改 / 写坏，`last_root` 不是字符串时 `Path(r)`
    # 会抛 TypeError —— 本函数在启动路径上，等于**启动即崩**。类型不对就当没有。
    if isinstance(r, str) and r and Path(r).is_dir():
        # 2026-11-05：排除落在第三方库目录里的历史脏值。外接盘掉线的那次探测
        # 会把 ...\site-packages\prompt_toolkit\output 写成 last_root，而它
        # 「存在且是目录」⇒ 旧逻辑会一直沿用，用户从此每次打开都进错目录。
        # 这里当作没有记录，让 default_root 重新探测（探测已不会产出这种路径）。
        if _in_lib_tree(r):
            log(f"[root] 忽略历史根目录（位于第三方库目录内）：{r}")
            return None
        return str(Path(r).resolve())
    return None


def load_delete_mode() -> str:
    """删除去向偏好：app=应用内回收站（默认，可在软件里还原）/ system=Windows 回收站。"""
    m = str(load_settings().get("delete_mode") or "").strip().lower()
    return m if m in DELETE_MODES else "app"


def save_delete_mode(m: str) -> bool:
    """写入删除去向偏好（非法值直接拒绝，避免把设置写成脏值）。"""
    m = str(m or "").strip().lower()
    if m not in DELETE_MODES:
        return False
    with _store_lock(SETTINGS_LOCK, "删除方式"):
        s = load_settings()
        s["delete_mode"] = m
        return save_settings(s)


def valid_keep_days(days) -> bool:
    """trash_keep_days 的合法性（与 save_trash_keep_days 同口径）。

    B1-3：独立出来是为了让 `/api/prefs` 能**先校验、后写盘** —— 否则
    「值非法」与「写盘失败」共用同一个 False，数据目录不可写时会被报成
    「trash_keep_days 只能是 0-3650 的整数」，用户怎么改都不对。
    """
    try:
        n = int(str(days).strip())
    except (TypeError, ValueError):
        return False
    return 0 <= n <= 3650


def save_trash_keep_days(days) -> bool:
    """写入回收站保留天数（P1-3）。非法值直接拒绝，避免把设置写成脏值。"""
    if not valid_keep_days(days):
        return False
    n = int(str(days).strip())
    with _store_lock(SETTINGS_LOCK, "回收站保留天数"):
        s = load_settings()
        s["trash_keep_days"] = n
        return save_settings(s)


def load_roots() -> list:
    """常用目录列表（仍存在的），最新在前。"""
    out = []
    for r in load_settings().get("roots", []):
        # B1-P2-3（2026-10-19）：`roots` 里混入非字符串时 `Path(r)` 抛 TypeError，
        # 而下面只捕 OSError ⇒ `/api/roots` 整个 500，表现为「常用目录列表加载不出来」。
        if not isinstance(r, str) or not r.strip():
            continue
        # P0-1：历史态里可能存着卷根（旧版允许登记），一律不作为「常用目录」回显，
        # 免得用户在菜单里点一下就又把它选回来。
        try:
            if _is_volume_root(r):
                continue
            # 2026-11-05：历史脏值（第三方库里的 output）不再出现在「常用目录」里，
            # 免得用户点一下就又把它选回来，也免得菜单里一直摆着一条假目录。
            if _in_lib_tree(r):
                continue
            if Path(r).is_dir():
                out.append(str(Path(r).resolve()))
        except (OSError, ValueError, TypeError):
            pass
    return out


def add_root(root: str) -> bool:
    """把 root 记入常用目录（去重、置顶），同时更新 last_root，最多 8 个。

    B1-3（2026-10-09）：返回**是否真的持久化成功**。此前无返回值、调用方也都不看，
    数据目录不可写时「常用目录」看着加上了、重启全没了，且界面上没有任何提示。
    """
    r = str(root)
    with _store_lock(SETTINGS_LOCK, "常用目录"):
        s = load_settings()
        roots = [x for x in s.get("roots", []) if x != r]
        roots.insert(0, r)
        s["roots"] = roots[:8]
        s["last_root"] = r
        return bool(save_settings(s))


def _root_key(p) -> str:
    """比较两条目录是否同一处（Windows 大小写 / 分隔符 / 末尾斜杠都不敏感）。

    历史 settings.json 里同一个目录可能存成 `G:\\a`、`g:\\a\\`、`G:/a` 三种写法，
    直接字符串比较会「删了却还在列表里」——实测点删除没有任何变化就是这么来的。
    """
    try:
        s = os.path.abspath(str(p or "")).strip()
        if not s:
            return ""
        return os.path.normcase(s).rstrip("\\/")
    except (OSError, ValueError, TypeError):
        return ""


def remove_root(root: str):
    """把 root 从常用目录移除（不碰当前根、不碰 last_root）。

    返回 `(持久化成功, 是否真的移除了)`：本来就不在列表里时算成功但未变更，
    这样重复点删除不会报错（幂等）。
    """
    r = _root_key(root)
    if not r:
        return True, False
    with _store_lock(SETTINGS_LOCK, "常用目录"):
        s = load_settings()
        old = [x for x in s.get("roots", []) if isinstance(x, str)]
        new = [x for x in old if _root_key(x) != r]
        if len(new) == len(old):
            return True, False
        s["roots"] = new
        # 顺手清掉该根的隐藏名单（2026-11-11）。不清的后果：这条键没有任何界面入口
        # 再能清到（设置页只列当前根），而目录将来重新加回常用目录时，旧隐藏项会
        # 原地「复活」——用户早已忘了自己藏过什么。键的写法与 _hidden_root_key 一致，
        # 但历史数据可能大小写/分隔符不同，仍走 _root_key 归一比较。
        book = s.get(HIDDEN_KEY)
        stale_cleared = False
        if isinstance(book, dict) and book:
            stale = [k for k in book if _root_key(k) == r]
            if stale:
                book = dict(book)
                for k in stale:
                    book.pop(k, None)
                if book:
                    s[HIDDEN_KEY] = book
                else:
                    s.pop(HIDDEN_KEY, None)   # 空名单不占位（与 _hidden_write 同口径）
                stale_cleared = True
        ok = bool(save_settings(s))
    if ok and stale_cleared:
        _hidden_invalidate()      # 名单变了 ⇒ 同进程内的快照必须立刻失效
    return ok, True


# ComfyUI 的 output 只会在「程序目录」下，绝不会在这个位置出现：
#   ...\python_embeded\Lib\site-packages\prompt_toolkit\output
#   ...\python\Lib\site-packages\scenedetect\output
# 这些是第三方库自带的测试/示例输出目录。2026-11-05 实测：外接盘 G: 休眠或掉线时，
# 探测只剩 D 盘那两个假 output，`prompt_toolkit\output`（11 个条目）就会被选成
# 「主力输出目录」并写进 last_root —— 之后即使 G: 回来，也一直默认打开它。
# 判据放在「路径段」上而不是目录名上：真 output 的父链里不会有这些段。
_LIB_PATH_SEGS = frozenset({
    "site-packages", "dist-packages", "python_embeded", "python_embedded",
    "node_modules", "__pycache__", "venv", ".venv",
})


def _in_lib_tree(p) -> bool:
    """路径是否落在第三方库/虚拟环境里（这类目录下的 output 一律不是 ComfyUI 的）。"""
    try:
        parts = [x.lower() for x in Path(p).parts]
    except (TypeError, ValueError):
        return False
    return any(seg in _LIB_PATH_SEGS for seg in parts)


def _find_output_under(base: Path, maxdepth: int = 4):
    """在 base 下递归查找名为 output 的子目录（限深度，栈式迭代）。

    用于识别 ComfyUI_windows_portable_amd 这种嵌套布局里的 output。

    2026-11-05：遇到第三方库目录直接**不下钻**。这既是正确性（避免把
    prompt_toolkit/scenedetect 的 output 当候选），也是性能（不再递归进
    Lib/site-packages 那棵上万文件的树）。
    """
    stack = [(base, 0)]
    while stack:
        d, depth = stack.pop()
        if depth > maxdepth:
            continue
        try:
            for child in d.iterdir():
                if not child.is_dir():
                    continue
                if child.name.lower() == "output":
                    yield child
                elif depth < maxdepth and child.name.lower() not in _LIB_PATH_SEGS:
                    stack.append((child, depth + 1))
        except OSError:
            pass


def _fixed_drives():
    """枚举本地盘（固定盘 + 可移动盘；跳过光驱/网络映射盘）。

    ε-13（§6.6）：此前只认 DRIVE_FIXED(3)，USB 外接盒（用户的 ComfyUI 就在
    G: 外接 NVMe 上）若是 DRIVE_REMOVABLE(2) 就既探测不到也选不到 ——
    盘符栏直接没有 G:，自动找 output 也扫不到它。GetDriveTypeW 本身不触媒体、
    不会卡；真正可能卡的是后续 iterdir，那层本来就有 OSError 兜底。
    网络盘(4) / 光驱(5) 维持排除（探测时长时间卡住的元凶）。
    """
    import string
    try:
        import ctypes
        get_type = ctypes.windll.kernel32.GetDriveTypeW
        # B1-P2-10（2026-10-19）：ε-13 把 DRIVE_REMOVABLE 纳入是有意的（用户的 ComfyUI
        # 就在外接 NVMe 上），但「读卡器里没插卡」的盘符也是 REMOVABLE —— 对它做
        # iterdir / 自动找 output 会卡在「请插入磁盘」上，最长几十秒。
        # 这里只对 REMOVABLE 多做一次**快速就绪判定**：`GetVolumeNameForVolumeMountPointW`
        # 对无媒体盘立即失败（不触发挂载），就绪的真盘正常返回。
        # 有 media 的 REMOVABLE 一律保留（否则 ε-13 会回归）；API 不可用时按就绪处理。
        try:
            get_vol = ctypes.windll.kernel32.GetVolumeNameForVolumeMountPointW
        except AttributeError:
            get_vol = None

        def _removable_ready(letter):
            if get_vol is None:
                return True
            buf = ctypes.create_unicode_buffer(1024)
            try:
                ok = get_vol(ctypes.c_wchar_p(letter + ":\\"), buf, 1024)
            except Exception:
                return True
            return bool(ok)

        for letter in string.ascii_uppercase:
            try:
                dt = get_type(letter + ":\\")
                if dt == 3:                              # DRIVE_FIXED
                    yield Path(letter + ":\\")
                elif dt == 2 and _removable_ready(letter):   # DRIVE_REMOVABLE 且已就绪
                    yield Path(letter + ":\\")
            except Exception:
                continue
    except (AttributeError, OSError):                  # 非 Windows 兜底
        for letter in string.ascii_uppercase:
            yield Path(letter + ":\\")


def _fixed_dirs() -> list:
    """返回固定盘符字符串列表（如 [\"C:\\\\\", \"D:\\\\\"]），供目录选择器盘符栏用。"""
    return [str(d) for d in _fixed_drives()]


# 顶层明显不可能藏 ComfyUI 的系统目录：对它们做二级 scandir 纯属浪费
# （C:\Windows 一层就有几万个文件，冷缓存时单次 scandir 可达数百毫秒）。
# 注意**不包含** Users / ProgramData 之外的常用位置 —— 用户完全可能把 ComfyUI
# 放在 C:\Users\xxx\ComfyUI，那必须能扫到。
_SKIP_TOP_NAMES = frozenset({
    "windows", "windows.old", "program files", "program files (x86)", "programdata",
    "$recycle.bin", "system volume information", "recovery", "perflogs",
    "msocache", "$winreagent", "config.msi", "$sysreset", "dell", "intel",
})


def _count_media_bounded(p, limit: int = 800):
    """有界统计目录内容：返回 (媒体文件数, 子目录数)，最多看 limit 个条目。

    为什么必须有界：真 output 可能有上万张图，全量 `sum(1 for _ in iterdir())`
    在冷缓存的外接盘上实测要 1~3 秒（2026-11-05：10357 条被数了两遍 = 3.5s，
    全耗在「哪个候选条目更多」这一个判据上）。排序只需要「谁更像主力」，
    不需要精确总数。
    """
    media = 0
    sub = 0
    n = 0
    exts = IMAGE_EXTS | VIDEO_EXTS
    try:
        with os.scandir(str(p)) as it:
            for e in it:
                n += 1
                if n > limit:
                    break
                try:
                    if e.is_dir():
                        sub += 1
                    elif os.path.splitext(e.name)[1].lower() in exts:
                        media += 1
                except OSError:
                    continue
    except OSError:
        return (0, 0)
    return (media, sub)


def _detect_comfyui_output():
    """自动搜索 ComfyUI 的 output 目录。

    只浅层扫描各固定盘根目录的前两级，寻找名称以 ComfyUI 开头的目录
    （如 ComfyUI、ComfyUI-aki-v3），并在其下递归（限深度）寻找 output
    子目录。不做全盘递归，保证启动速度。

    2026-10-27（代码梳理 O15）：枚举方式从 `Path.iterdir()` + `is_dir()`
    （每个条目一次额外 stat）改成 `os.scandir()`（Windows 的目录项自带类型，
    多数情况下零额外系统调用）；并且**边扫边判名**，不再先把整层目录收进
    `bases` 列表 —— 原先扫 C:\\ 时要给 Windows / Program Files / Users 下的
    全部子项各建一个 Path 对象，是启动探测里最重的一步。
    语义没变：仍然只看两级、仍然只在名字以 comfyui 开头时才下钻。

    2026-11-05（本轮）：修三个问题 ——
      ① 候选跨 base 重复（`ComfyUI-aki-v3` 与它下面的 `ComfyUI` 都命中，
         同一个 output 被数两遍）；
      ② 排序用全量条目数 ⇒ 上万张图时要遍历两遍（实测 3.5s）；
      ③ 第三方库里的同名 output 混进候选（已由 `_find_output_under` 不下钻
         Lib 目录解决，这里再兜一层 `_in_lib_tree`）。
    """
    candidates = []
    seen = set()
    for drive in _fixed_drives():
        try:
            if not drive.is_dir():
                continue
            with os.scandir(str(drive)) as it:
                tops = [e.path for e in it if e.is_dir()]
        except OSError:
            continue
        for top in tops:
            if os.path.basename(top).lower() in _SKIP_TOP_NAMES:
                continue
            hits = []
            try:
                if os.path.basename(top).lower().startswith("comfyui"):
                    hits.append(top)                     # 一级：D:\ComfyUI
                # 二级：D:\AI\ComfyUI（只下钻一层，不做全盘递归）
                with os.scandir(top) as it:
                    for e in it:
                        if not e.name.lower().startswith("comfyui"):
                            continue                     # 先比名字，再问类型：省掉绝大多数 is_dir
                        try:
                            if e.is_dir():
                                hits.append(e.path)
                        except OSError:
                            continue
            except OSError:
                continue
            for b in hits:
                try:
                    # 匹配到 comfyui* 后在其下递归找 output（覆盖便携版嵌套布局）
                    for o in _find_output_under(Path(b)):
                        try:
                            key = os.path.normcase(os.path.realpath(str(o)))
                        except OSError:
                            key = str(o)
                        if key in seen or _in_lib_tree(key):
                            continue
                        seen.add(key)
                        candidates.append(str(o.resolve()))
                except OSError:
                    pass
    if not candidates:
        return None
    # 多个候选时选「最像主力出图目录」的那个：有媒体的 > 媒体多的 > 子目录多的 >
    # 最近修改过的。用有界统计，避免为排序遍历上万条目。
    best, best_key = None, None
    for c in candidates:
        media, sub = _count_media_bounded(c)
        try:
            mtime = os.stat(c).st_mtime
        except OSError:
            mtime = 0.0
        key = (1 if media > 0 else 0, media, sub, mtime)
        if best_key is None or key > best_key:
            best, best_key = c, key
    return best


def default_root() -> str:
    r = load_last_root()          # 记住用户上次选的目录
    if r and not _is_volume_root(r):   # P0-1：旧版可能把卷根记进 last_root，不再沿用
        return r
    d = _detect_comfyui_output()
    if d:
        return d
    # ε-13（§6.6）：Path.home()/Path.cwd() 此前在 try **之外**求值 ——
    # cwd 被删（从临时目录启动后目录消失）时 Path.cwd() 直接抛 FileNotFoundError，
    # 整个 default_root 连兜底都走不到。现在求值本身也包进 try。
    try:
        for c in (Path.home() / "Pictures", Path.home() / "Documents"):
            try:
                if c.is_dir():
                    return str(c.resolve())
            except (OSError, RuntimeError):
                pass
        return str(Path.cwd().resolve())
    except (OSError, RuntimeError):
        return str(Path(__file__).resolve().parent)


def _count_media(d: Path, hid=None) -> int:
    """递归统计目录内媒体文件数（用于文件夹卡片）；scandir 栈式遍历，一趟完成。

    带 visited(realpath) 去重 + 深度上限，防 junction/符号链接环导致重复计数或死循环。
    hid：隐藏名单快照（2026-11-08）—— 被隐藏的子目录整棵跳过，卡片上的数字与
    展开后实际能看到的内容保持一致（否则「12 个文件」点进去只有 3 个）。
    """
    media = IMAGE_EXTS | VIDEO_EXTS
    n, stack = 0, [(str(d), 0)]
    visited = {os.path.realpath(str(d))}
    while stack:
        cur, depth = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            if depth < 10 and not _hid_hit(hid, e.path):
                                rp = os.path.realpath(e.path)
                                if rp not in visited:
                                    visited.add(rp)
                                    stack.append((e.path, depth + 1))
                        elif os.path.splitext(e.name)[1].lower() in media:
                            n += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return n


def _dir_media_count(d: str) -> int:
    """单层目录内媒体文件数（scandir 一趟，DirEntry 类型信息免二次 stat）。"""
    media = IMAGE_EXTS | VIDEO_EXTS
    try:
        with os.scandir(d) as it:
            return sum(1 for e in it
                       if e.is_file() and
                       os.path.splitext(e.name)[1].lower() in media)
    except OSError:
        return 0


def _is_volume_root(p) -> bool:
    """p 是否是「整卷」路径（C:\\ / D:\\ / \\\\srv\\share / /）—— 这类路径绝不许当媒体根。

    P0-1（10-15 全代码审计，2.12.9 同样成立）：根目录一旦登记成卷根，
    `inside_root` 的 `root in q.parents` 对**整卷任意路径**恒真
    ⇒ /api/file、/api/thumb、/api/delete 等全部端点对全盘放开，
    等于「任意文件可被删」。必须在登记口（/api/root）与判定口（inside_root）双重堵死。

    判据用 `q == q.parent`（`PurePath('C:/').parent` 就是自己；`\\\\srv\\share`、`/` 同理），
    比枚举盘符更稳（网络盘 / UNC / 挂载点都覆盖）。空串也一并拒 ——
    `Path("")` 会退化成 cwd，属于「认不出来的危险值」。
    """
    try:
        if not str(p).strip():
            return True                      # 空串：不许
        q = Path(p)
        return q == q.parent                 # 整卷 / 共享根 / 文件系统根 ⇒ 自身即父
    except Exception:
        return True                          # 认不出来的一律当危险（fail closed）


def inside_root(p: Path) -> bool:
    """该路径是否允许被服务：库根目录内，或单图暂存目录内。

    单图查看（V2.10.7）把外部图片复制进 SOLO_DIR，这里一并放行 —— 于是
    /api/file、/api/thumb、/api/meta 等既有端点无需任何改动就能服务它。
    放行范围只有这一个目录（哈希命名、由本进程写入），不是「库外一律放行」。
    """
    try:
        q = p.resolve()
    except OSError:
        return False
    # B1-P2-6（2026-10-19）：`SOLO_DIR.resolve()` 原来在 try 之外。便携模式把数据目录
    # 放在 U 盘/外接盘上时，盘一掉线（或盘符变化）这里就抛 OSError —— 而 inside_root
    # 几乎被所有文件类端点调用（/api/file、/api/thumb、/api/meta…），结果是**整片 500**
    # 而不是「仅该文件读不到」。失败按「不在暂存区」处理，让后续 root 判定继续走。
    try:
        solo = SOLO_DIR.resolve()
    except OSError:
        solo = None
    if solo is not None and (q == solo or solo in q.parents):
        return True
    if not STATE.get("root"):
        return False
    root = Path(STATE["root"]).resolve()
    # P0-1：坏 root（整卷）= 对全盘放行。宁可整库不可用（用户会立刻发现并重选目录），
    # 也不能让「删任意文件」的口子留着。正常登记路径已在 /api/root 拦住，这里是兜底。
    if _is_volume_root(root):
        return False
    return q == root or root in q.parents


# 单图查看允许的 Content-Type（用于挡跨站简单请求，真实类型另按文件头判定）
SOLO_CTYPES = ("image/png", "image/jpeg", "image/jpg", "image/webp",
               "image/gif", "image/bmp", "application/octet-stream")


def solo_sniff(raw: bytes):
    """按文件头判图像类型，返回规范后缀；不认识则 None。

    刻意不信任客户端给的后缀/Content-Type：内容是任意字节、后缀却是 .png 时，
    前端 <img src="/api/file?p=..."> 会把这段字节当图片渲染，等于开了个
    「往库里塞任意文件」的口子。这里只认真正的图片魔数。
    """
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if raw[:2] == b"BM":
        return ".bmp"
    return None


def solo_prune() -> int:
    """暂存区清理：扫掉崩溃残留的半截文件，并把总张数压到 SOLO_KEEP_MAX 内。

    按 mtime 从旧删 —— 刚放进来的那张一定是最新的，不会被自己挤掉。
    """
    try:
        files = [f for f in SOLO_DIR.iterdir() if f.is_file()]
    except OSError:
        return 0
    now = time.time()
    removed, keep = 0, []
    for f in files:
        if f.name.endswith(".part"):
            try:                          # 半截文件只可能是写盘途中退出留下的
                if now - f.stat().st_mtime > 600:
                    f.unlink()
                    removed += 1
                    continue
            except OSError:
                pass
            # B1-P2-8（2026-10-19）：新鲜的 `.part` 原来直接 continue，既不进 keep
            # 也不算进 SOLO_KEEP_MAX —— 频繁崩溃时暂存区会长期超出上限（每个最大
            # 64MB）。现在一并纳入总数：超限时**先从 `.part` 删**（它们本来就没用）。
            keep.append(f)
            continue
        keep.append(f)

    def _mt(f):
        try:
            return f.stat().st_mtime
        except OSError:
            return 0.0

    # 超限时优先删 `.part`（半截文件本来就不可能被读到），再按 mtime 从旧删
    keep.sort(key=lambda f: (0 if f.name.endswith(".part") else 1, _mt(f)))
    for f in keep[:max(0, len(keep) - SOLO_KEEP_MAX)]:
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


SOLO_CLEAR_FAILED = []      # 上一次清暂存时删不掉的文件名（B1-P2-7，供端点如实上报）


def clear_solo_cache() -> int:
    """清空单图暂存区，返回删除数量（设置 · 数据页的清除按钮）。

    B1-P2-7（2026-10-19）：原来删不掉的文件 `except OSError: pass` 静默跳过，
    端点照样回 `{"ok": true, "removed": n}`、界面提示「已清除 N 项」——
    实际有残留（预览正在读该文件 / 文件只读）。现在把失败项收集到
    `SOLO_CLEAR_FAILED` 并由 /api/cache_clear 一并回给前端。
    """
    n = 0
    failed = []
    try:
        for f in SOLO_DIR.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    failed.append(f.name)
    except OSError as ex:
        log(f"[solo] 清暂存区时列目录失败：{type(ex).__name__}: {ex}")
    if failed:
        log("[solo] 有 %d 个暂存文件未能删除（被占用或只读）：%s"
            % (len(failed), "、".join(failed[:5])))
    SOLO_CLEAR_FAILED[:] = failed
    _dir_usage_invalidate(SOLO_DIR)   # F5：清完不失效 ⇒ 设置页数字不变，像没清成功
    return n


# E5-7（2026-09-23，ε-4 遗留）：「读取失败」与「本来就没有」的**结构区分**。
#
# 背景：ε-4 已在 png_meta / thumb_for / _save_idx_cache 三处**留痕**（写日志），但
#   **返回值语义仍不可区分** —— png_meta 读失败返回 {} 与「这张图真的没有元数据」一模一样，
#   于是 /api/meta 照样 200、前端打出「这张图没有内嵌生成信息」——
#   一句**与事实相反**的陈述（其实是我们没读到）。
#
# 为什么不用「改返回值」：那正是 ε-4 标注的风险 —— png_meta 有 7 个调用点，
#   下游 _wf_class / 索引 / /api/meta 都按「空 = 没有」在用（见 png_meta 自己的注释）。
#   所以这里用**旁路状态**：不动任何函数签名，只在失败处登记、由 api_meta 顺带回带。
#
# ⚠️ 读成功时必须**清掉**旧登记 —— 失败往往是瞬时的（文件被别的进程占着），
#   不清就会出现「现在明明读得到、界面还说读失败」的反向谎报。
_META_FAIL = {}
_META_FAIL_LOCK = threading.Lock()
_META_FAIL_MAX = 500


def _meta_fail_mark(path, why):
    """登记一次「元数据读取失败」。第一条最接近根因，后续同路径不覆盖。"""
    try:
        k = str(path)
        with _META_FAIL_LOCK:
            if k not in _META_FAIL and len(_META_FAIL) < _META_FAIL_MAX:
                _META_FAIL[k] = str(why)
    except Exception:
        pass                       # 纯旁路：登记失败绝不能影响主流程


def _meta_fail_clear(path):
    try:
        with _META_FAIL_LOCK:
            _META_FAIL.pop(str(path), None)
    except Exception:
        pass


def meta_fail_reason(path):
    """本次进程内该路径读到过失败吗？返回原因字符串；None = 干净（即「本来就没有」）。"""
    with _META_FAIL_LOCK:
        return _META_FAIL.get(str(path))


def png_meta(path: Path) -> dict:
    """解析 PNG 的 tEXt / zTXt / iTXt chunk（ComfyUI 的 workflow / prompt 就在这里）。"""
    meta = {}
    try:
        with open(path, "rb") as f:
            # E5-7：**能打开就等于读得到** ⇒ 立刻清掉可能存在的旧登记（失败常常是瞬时的，
            # 文件被别的进程占着一会儿）。放在这里而不是函数末尾，是因为函数中段还有
            # 「不是 PNG ⇒ return」的早退路径，走那条路时不该把旧登记留下来。
            _meta_fail_clear(path)
            if f.read(8) != b"\x89PNG\r\n\x1a\n":
                return meta
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                length, ctype = struct.unpack(">I4s", hdr)
                # §6.1：旧实现遇到 IDAT 就 break，理由是「文本块都在图像数据之前」——
                # 但 PNG 规范允许 tEXt/zTXt/iTXt 出现在 IDAT **之后**（部分工具会这么写），
                # 这些元数据此前**永远读不到**，且与「本来就没有生成参数」无法区分。
                # 现在：IDAT 只 seek 跳过（不读进内存，开销与原先的 break 同量级），
                # 一路读到 IEND 才停。
                if ctype == b"IDAT":
                    f.seek(length + 4, 1)
                    continue
                if ctype == b"IEND":
                    break
                if length <= (1 << 26):
                    data = f.read(length)
                else:
                    f.seek(length, 1)          # 超大块：跳过数据但保持解析位置
                    data = b""
                f.read(4)
                if ctype == b"tEXt" and data:
                    k, _, v = data.partition(b"\x00")
                    meta[k.decode("latin-1", "replace")] = v.decode("latin-1", "replace")
                elif ctype == b"zTXt" and len(data) > 1:
                    k, _, rest = data.partition(b"\x00")
                    try:
                        d = zlib.decompressobj()
                        raw = d.decompress(rest[1:], MAX_TEXT_BYTES)
                        if not d.eof or d.unconsumed_tail:   # 解压结果超限/被截断 → 丢弃
                            raw = b""
                        meta[k.decode("latin-1", "replace")] = raw.decode("utf-8", "replace")
                    except zlib.error:
                        pass
                elif ctype == b"iTXt" and data:
                    k, _, rest = data.partition(b"\x00")
                    comp = rest[0] if rest else 0
                    body = rest[2:] if len(rest) > 2 else b""
                    _lang, _, body = body.partition(b"\x00")
                    _tr, _, body = body.partition(b"\x00")
                    if comp == 1:
                        try:
                            d = zlib.decompressobj()
                            body = d.decompress(body, MAX_TEXT_BYTES)
                            if not d.eof or d.unconsumed_tail:
                                body = b""
                        except zlib.error:
                            # 解压失败就**整条丢弃**：旧写法让 body 保持压缩字节、
                            # 下面照样 decode 成 UTF-8 ⇒ 界面上是一串乱码。
                            body = None
                        if body is not None:
                            meta[k.decode("latin-1", "replace")] = body.decode("utf-8", "replace")
                    else:
                        meta[k.decode("latin-1", "replace")] = body.decode("utf-8", "replace")
                if ctype == b"IHDR" and len(data) >= 8:
                    w, h = struct.unpack_from(">II", data)
                    meta["IHDR"] = f"{w} x {h}"
                if ctype == b"IEND":
                    break
    except OSError as ex:
        # §6.1：读失败此前静默 ⇒ 与「这张图本来就没有元数据」不可区分。
        # 返回值结构不动（下游按「空 = 没有」用），但至少让日志能查出是**读不到**。
        # E5-7：除日志外再登记到旁路状态 —— api_meta 会回带给前端，
        #       让界面把「读不到」与「本来没有」分开说（见 _META_FAIL 注释）。
        _meta_fail_mark(path, "%s: %s" % (type(ex).__name__, ex))
        log(f"[warn] PNG 元数据读取失败 {path.name}: {type(ex).__name__}: {ex}")
    return meta


# ------------------------------------------------- 提示词解析（人话版）

def _inp(node) -> dict:
    """节点的 inputs 字典。

    畸形 PNG 里它可能是 list / 字符串 / None，直接 .get / .items() 会抛
    AttributeError —— 而索引构建是逐文件的，一次抛出整批索引就没了。
    """
    v = node.get("inputs") if isinstance(node, dict) else None
    return v if isinstance(v, dict) else {}


def _walk_nodes(start, nodes, visit, stop_keys=None, prune=None, skip_key=None,
                ref_filter=None):
    """从 start 节点沿上游连线遍历，visit(node) 按序回调。

    stop_keys 里的输入名不再向上回溯（提示词只走条件/文本链路，见 collect_texts）；
    prune(node) 为真时该节点的上游一律不再回溯（如 ConditioningZeroOut 已把条件清零）；
    skip_key(name)->bool 与 stop_keys 同义，但按**判定函数**过滤 —— 键名是中文或各家
    自定义拼写时没法穷举成元组（见 _is_non_prompt_key）；
    ref_filter(node, key)->bool 按「节点 + 输入名」决定这一路要不要走 —— 给直通开关
    用（ImpactSwitch 同时接着多条提示词分支时，只该收 select 指向的那一条）。
    """
    seen, stack = set(), [start]
    while stack:
        nid = stack.pop()
        if not nid or nid in seen or nid not in nodes:
            continue
        seen.add(nid)
        n = nodes[nid]
        if isinstance(n, dict):
            visit(n)
            if prune and prune(n):
                continue
            # D3（ε-17）：同一节点的上游**按输入顺序**回溯 —— 栈本身是 LIFO，
            # 逐个 append 会让多源头时的收块顺序与工作流里的输入顺序相反（提示词前後颠倒）。
            # 逆序压栈 ⇒ 弹栈顺序 == 输入顺序，DFS 深度语义不变。
            refs = [v[0] for k, v in _inp(n).items()
                    if not (stop_keys and k in stop_keys)
                    and not (skip_key and skip_key(k))
                    and not (ref_filter and not ref_filter(n, k))
                    and isinstance(v, list) and len(v) == 2 and isinstance(v[0], str)]
            stack.extend(reversed(refs))


def _switch_selected_input(n):
    """直通开关（ImpactSwitch / LatentSwitch…）当前选中的那一路 → "input<k>"。

    不是开关、或 select 本身就是输入口（`sel_mode` 为真，运行时才知道）时返回 None，
    表示「不替它做选择」。Impact Pack 的判定口径见 impact_server.onprompt_for_switch：
    选中项就是 `input{select}`（select 为 1 起始的整数）。
    """
    if "switch" not in _ct_key(n):
        return None
    i2 = _inp(n)
    if i2.get("sel_mode"):
        return None
    sel = i2.get("select")
    if isinstance(sel, bool) or not isinstance(sel, int):
        return None
    key = "input%d" % sel
    return key if key in i2 else None


def _strength_fmt(st):
    """强度统一转成数字（兼容 "0.25" 这类字符串），转不了返回 None。"""
    if isinstance(st, bool):
        return None
    if isinstance(st, (int, float)):
        return st
    if isinstance(st, str):
        try:
            return float(st)
        except ValueError:
            return None
    return None


def _lora_entries_from_json(v):
    """槽位表被 JSON 塞进字符串（或本身就是数组）的形态 → 条目列表 / None。

    有些自定义 LoRA 加载器不允许把列表存成 widget，就把整张槽位表
    `json.dumps` 进一个 STRING 输入（例：VisualLoRALoader 的 `lora_data`）。
    这时 API prompt 里拿到的是**字符串**，不是数组 —— 常规三条分支
    （`loras` / `lora_N` / `lora_name`）全落空，LoRA 一条都读不出来。
    解出来仍是 `[{name, strength_model, enabled, …}]`，与 LoraManager 同构。

    解不出、顶层不是数组、或数组里没有任何带字符串 `name` 的对象 ⇒ 返回 None：
    不能把一段普通文本（或提示词里的 JSON）当槽位表。
    """
    if isinstance(v, list):
        arr = v
    elif isinstance(v, str):
        s = v.strip()
        if not s.startswith("[") or len(s) > 200000:
            return None
        try:
            arr = json.loads(s)
        except (ValueError, RecursionError):
            return None
        if not isinstance(arr, list):
            return None
    else:
        return None
    ents = [e for e in arr if isinstance(e, dict) and isinstance(e.get("name"), str)]
    return ents or None


def _entry_strength(e: dict):
    """槽位条目的模型强度（字段名各家不同，取第一个有值的）。"""
    for k in ("strength", "strength_model", "strengthModel", "weight"):
        v = e.get(k)
        if v is not None:
            return _strength_fmt(v)
    return None


def _ct_key(n: dict) -> str:
    """节点类名归一化：去掉空格/下划线/括号等。

    这样 "Checkpoint Loader with Name (Image Saver)" 也能匹配 "checkpointloader"。
    """
    return re.sub(r"[^a-z0-9]", "", str(n.get("class_type", "")).lower())


# ---- 出图尺寸：分辨率字符串 + 缩放链 ---------------------------------------
# 有两类工作流让「出图尺寸」读不出来，且原因完全不同：
#   ① 宽高不在 width/height 两个数值里，而是塞进一个 STRING widget（"分辨率"）——
#      例：ZImageLatent 的 `resolution` = "1536x864 ( 16:9 )"。旧逻辑只认
#      `empty*latent*` 节点上的 width/height ⇒ 整行读不到。
#   ② 潜变量链上再挂放大节点（LatentUpscaleBy ×1.5 等）。这时读到的只是
#      **放大前**的基础尺寸，而 PNG 实际像素是放大后的（1536×864 → 2304×1296）。
# 两者都修，但输出拆成两个键：`size` 仍是基础尺寸（「应用到工作流」要写回
# EmptyLatentImage 的 width/height，写成品尺寸会让出图再放大一倍），
# `size_out` 才是成品尺寸，只在真的经过缩放时出现。

_RES_SIZE_RE = re.compile(r"(\d{2,5})\s*[x×*]\s*(\d{2,5})")

# 等比缩放节点（类名归一化后精确匹配）。刻意用白名单而不是 `"scale" in ct`：
# ImageScaleToTotalPixels 这类按「总像素数」反推宽高的节点公式不同，混进来会算错。
_SCALE_BY = ("latentupscaleby", "imagescaleby", "upscalelatentby")
_SCALE_ABS = ("latentupscale", "imagescale", "upscalelatent")


def _size_from_resolution(v):
    """「分辨率」字符串 → (w, h)；不是尺寸字符串返回 None。

    只认 `数字 x 数字` 形状。比例写法 `16:9` 用的是冒号，不会误命中；
    上限按 _parse_size 的口径（8192）挡掉明显的垃圾值。
    """
    if not isinstance(v, str):
        return None
    m = _RES_SIZE_RE.search(v)
    if not m:
        return None
    w, h = int(m.group(1)), int(m.group(2))
    if 1 <= w <= 8192 and 1 <= h <= 8192:
        return (w, h)
    return None


def _node_id_of(nodes: dict, node) -> str:
    """节点字典在 prompt 里的 id（按对象身份反查，走不到就返回空串）。"""
    for k, v in nodes.items():
        if v is node:
            return k
    return ""


def _latent_scale_size(nodes: dict, origin_id: str, w: int, h: int, num=None):
    """从潜变量源头沿**下游**累计缩放 → (w, h)；没经过缩放返回 None。

    为什么必须往下游走：采样器的 `latent_image` 指向"喂给它的潜变量"，而放大节点
    在采样器**输出侧**（KSampler → LatentUpscaleBy → 下一个采样阶段），沿上游回溯
    永远看不到。而 `ks` 选中链上第一个采样器 —— 究竟是放大前还是放大后那个，
    取决于节点在 JSON 里的顺序，不能依赖（实测这张图 ks 就是放大后的第二阶段）。

    num(v) 负责把可能是连线引用的数值取出来；缺省只认字面量。
    """
    if not (isinstance(origin_id, str) and origin_id and isinstance(nodes, dict)):
        return None
    if num is None:
        num = _strength_fmt

    def _n(v):
        """先走调用方的解析器（能跟连线），再兜底字面量字符串（"2" 这种）。"""
        r = num(v)
        return _strength_fmt(v) if r is None else r

    down = {}
    for nid, n in nodes.items():
        if not isinstance(n, dict):
            continue
        for v in _inp(n).values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                down.setdefault(v[0], []).append(nid)
    seen, stack, out = set(), [(origin_id, w, h)], None
    while stack:
        nid, cw, ch = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        n = nodes.get(nid)
        if not isinstance(n, dict):
            continue
        ct = _ct_key(n)
        i2 = _inp(n)
        if ct in _SCALE_BY:
            s = _n(i2.get("scale_by"))
            if isinstance(s, (int, float)) and 0 < s <= 8:
                cw, ch = max(1, int(round(cw * s))), max(1, int(round(ch * s)))
                out = (cw, ch)
        elif ct in _SCALE_ABS:
            nw, nh = _n(i2.get("width")), _n(i2.get("height"))
            if isinstance(nw, (int, float)) and isinstance(nh, (int, float)) \
                    and nw > 0 and nh > 0:
                cw, ch = int(round(nw)), int(round(nh))
                out = (cw, ch)
        for d in down.get(nid, ()):
            stack.append((d, cw, ch))
    return out


def _latent_stage_sizes(nodes: dict, origin_id: str, w: int, h: int, is_sampler, num=None):
    """从潜变量源头沿**下游**走，记下**每个采样器节点**当时的 latent 尺寸 → {节点id: (w,h)}。

    与 `_latent_scale_size` 的区别：那个只回「最后一个缩放结果」（= 成品尺寸），
    而多段流要的是「每一段采样时用的尺寸」—— 两段 hires 流里第一段 1216×832、
    第二段 1824×1248 是**各自独立**的，不能用一个数代表全图。

    为什么从源头往下走而不是从采样器往上回溯：放大节点挂在采样器**输出侧**
    （KSampler → LatentUpscaleBy → 下一个采样阶段），往上永远看不到。
    走法与 `_latent_scale_size` 完全一致（只认白名单里的等比/绝对缩放节点），
    差别只是「路过采样器时记一笔」，而不是最后统一返回一个值。

    num(v) 负责把可能是连线引用的数值取出来；缺省只认字面量。
    """
    if not (isinstance(origin_id, str) and origin_id and isinstance(nodes, dict)):
        return {}
    if num is None:
        num = _strength_fmt

    def _n(v):
        r = num(v)
        return _strength_fmt(v) if r is None else r

    down = {}
    for nid, n in nodes.items():
        if not isinstance(n, dict):
            continue
        for v in _inp(n).values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                down.setdefault(v[0], []).append(nid)
    out, seen, stack = {}, set(), [(origin_id, w, h)]
    while stack:
        nid, cw, ch = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        n = nodes.get(nid)
        if not isinstance(n, dict):
            continue
        ct = _ct_key(n)
        i2 = _inp(n)
        if is_sampler(n):
            out[nid] = (cw, ch)
        elif ct in _SCALE_BY:
            s = _n(i2.get("scale_by"))
            if isinstance(s, (int, float)) and 0 < s <= 8:
                cw, ch = max(1, int(round(cw * s))), max(1, int(round(ch * s)))
        elif ct in _SCALE_ABS:
            nw, nh = _n(i2.get("width")), _n(i2.get("height"))
            if isinstance(nw, (int, float)) and isinstance(nh, (int, float)) \
                    and nw > 0 and nh > 0:
                cw, ch = int(round(nw)), int(round(nh))
        for d in down.get(nid, ()):
            stack.append((d, cw, ch))
    return out


def _entry_active(e: dict) -> bool:
    """多槽位加载器条目的启用开关；无开关字段视为启用。"""
    for k in ("active", "on", "enabled", "enable"):
        v = e.get(k)
        if isinstance(v, bool):
            return v
    return True


def _weilin_lora_slots(obj: dict):
    """WeiLinPromptUI 的 lora_str / temp_lora_str → 生效槽位条目（保持槽位顺序）。

    该节点是**独立节点**（`opt_model` 指向 UNETLoader，自己不在 sampler 的 model
    连线上），所以只沿模型链 visit_m 的实现走不到它 ⇒ LoRA 一条都读不出来。

    两个字段的区别（2026-11-03 样图 F1317CF4… 实测）：
      · lora_str      = 最终生效列表（10 条，全部 hidden:false）
      · temp_lora_str = 编辑器里的完整堆（12 条，多出的 2 条 hidden:true）
    hidden:true 是「堆里有、但没启用」，不进结果。lora_str 非空时以它为准，
    它为空才退回 temp_lora_str 里非 hidden 的条目。
    """
    if not isinstance(obj, dict):
        return None

    def _slots(key):
        out = []
        for n in obj.values():
            if not isinstance(n, dict):
                continue
            for e in (_lora_entries_from_json(_inp(n).get(key)) or ()):
                if e.get("hidden") is True or not _entry_active(e):
                    continue
                out.append(e)
        return out

    for key in ("lora_str", "temp_lora_str"):
        got = _slots(key)
        if got:
            return got
    return None


# 槽位被摊平成「点号输入」的 LoRA 堆叠节点（2026-11-03 样图 Krea2-2ST-… 实测）：
#   ComfyUI 的「动态输入」在 API prompt 里不写数组，而是把每个槽位拆成独立输入名 ——
#     loras.0.lora_name / loras.0.strength / loras.0.enabled
#   常规三条分支（`loras` 数组 / rgthree `lora_N` / `lora_name`）全部落空，
#   `_lora_entries_from_json` 也解不出（值是纯文件名，不是 JSON）⇒ 一条都读不到。
_DOTTED_LORA_RE = re.compile(
    r"^loras?\.(\d+)\.(lora_name|name|strength|strength_model|enabled|on|active)$", re.I)


def _node_dotted_lora_entries(n: dict):
    """单个节点上的点号槽位 → 条目列表（按槽位序号排序，保持编辑器里的顺序）。"""
    slots = {}
    for k, v in _inp(n).items():
        if not isinstance(k, str):
            continue
        m = _DOTTED_LORA_RE.match(k)
        if not m:
            continue
        slots.setdefault(int(m.group(1)), {})[m.group(2).lower()] = v
    out = []
    for i in sorted(slots):
        e = slots[i]
        nm = e.get("lora_name")
        if not isinstance(nm, str):
            nm = e.get("name")
        if not isinstance(nm, str) or not nm.strip():
            continue                    # 空槽位（用户点了「加一条」但没选文件）
        ent = {"name": nm.strip()}
        for k in ("strength", "strength_model"):
            if e.get(k) is not None:
                ent[k] = e[k]
                break
        for k in ("enabled", "on", "active"):
            if k in e:
                v = e[k]
                # 动态输入的 BOOLEAN 一般是真 bool；个别节点存成字符串，这里归一
                if isinstance(v, str):
                    _sv = v.strip().lower()
                    if _sv in ("false", "0", "off", "no"):
                        v = False
                    elif _sv in ("true", "1", "on", "yes"):
                        v = True
                ent[k] = v
                break
        if not _entry_active(ent):
            continue                    # 槽位在、但编辑器里勾掉了 ⇒ 不算生效
        out.append(ent)
    return out or None


def _dotted_lora_slots(obj: dict):
    """全图扫点号槽位。

    和 `_weilin_lora_slots` 同一个理由：这类堆叠节点常是**独立节点**，不一定落在
    主采样器的 model 连线上。本图（Krea2-2ST-…）的 `LoadLoraModel` 挂的是**第二段**
    `KSamplerAdvanced` 的模型链，而 ks 选中的是第一段 KSampler ⇒ visit_m 沿 [`30`→`28`]
    走，压根到不了 `285`。只修格式不修可达性，这条 LoRA 仍然是 0 条。
    """
    if not isinstance(obj, dict):
        return None
    out = []
    for n in obj.values():
        if isinstance(n, dict):
            out.extend(_node_dotted_lora_entries(n) or ())
    return out or None


# 提示词里的 LoRA 引用语法：
#   <lora:名字:强度[:CLIP强度]>   LoraManager / A1111 / tag loader 通用
#   <wlr:名字:模型强度:文本编码强度:触发词强度>
#                                 WeiLinPromptUI（2026-11-02 实测）—— 它把 LoRA 直接内联在
#                                 最终正向正文里，不走 LoraLoader 节点链，所以只沿 model
#                                 连线收 LoRA 的旧实现会漏掉整批。
_LORA_TAG_RE = re.compile(r"<\s*l(?:ora|yco|ocon)\s*:[^<>]*>"
                          r"|<\s*wlr\s*:[^<>]*>", re.I)

# 多块提示词节点（ComfyUI-Prompt-Composer 等）把提示词拆在 text_1 … text_10 上，
# 没有单数的 text 字段。见 collect_texts.visit() 里的收块分支。
_MULTI_TEXT_RE = re.compile(r"text_(\d+)$")


def _split_lora_tags(text: str):
    """拆出提示词里的 <lora:...> / <wlr:...> 标签，返回 (去掉标签的文本, 标签列表)。"""
    tags = _LORA_TAG_RE.findall(text)
    if not tags:
        return text, []
    out = _LORA_TAG_RE.sub("", text)
    # 标签一般用 ", " 串成一排（`<wlr:a:1>, <wlr:b:1>, 正文`）。直接替换成空格会留下
    # 一串空段（`,  ,  ,  , 正文`）—— 实测 4 个 wlr 标签剥完，正向正文就以 5 个逗号开头。
    # 把「连续逗号且中间只有空白」压成一个，再吃掉首尾的逗号。
    out = re.sub(r"[ \t]*,[ \t]*(?:,[ \t]*)+", ", ", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = out.strip().strip(",").strip()
    return out, tags


# 原生文本编码节点（ComfyUI 核心）—— 它们**不解析** <lora:…> 语法：
# CLIPTextEncode.encode() 只是 clip.encode(text)，标签在编码器眼里就是几个普通词。
# 2026-11-11 用户反馈 ComfyUI_12005_「读出了根本没有的 LoRA」：
#   164 是 CLIPTextEncode，正文末尾粘着从别处拷来的 `<lora:Faded80s:1.45>`；
#   真正加载 LoRA 的是 357 Lora Loader (LoraManager)，而它 32 个槽位 active 全为 false
#   （插件 lora_loader.py：`if not lora.get("active", False): continue`，且 `del text`）。
#   旧逻辑（B2-P2-5 的标签兜底）把那段文字登记成 LoRA ⇒ 界面出现幻觉条目。
# 全库 10053 张实测：571 张的 CLIPTextEncode 带标签，其中 540 张的标签不在任何加载器槽位里。
# ⚠️ 只认**核心类名精确匹配**：会解析 lora 语法的自定义节点不会叫这几个名字
#    （它们要么自己实现执行类，要么把语法放在专属字段里），留给兜底逻辑继续处理。
_RAW_CLIP_TEXT_NODES = frozenset({
    "cliptextencode", "cliptextencodesdxl", "cliptextencodeflux", "cliptextencodesd3",
})


def _split_lora_tags_node(ct: str, text: str):
    """按来源节点决定 `<lora:…>` 标签算不算 LoRA。

    原生 CLIPTextEncode 系列：标签只是文字（正文照旧剥掉，界面更干净），不登记 LoRA。
    其它节点（含 A1111 / WeiLin / 各类自定义解析器）：维持原有兜底行为。
    """
    txt, tags = _split_lora_tags(text)
    if tags and ct in _RAW_CLIP_TEXT_NODES:
        return txt, []
    return txt, tags


def _lora_tag_entry(tag: str):
    """<lora:名字:模型强度[:CLIP强度]> → "名字 (强度)"；解析不出返回 None。"""
    body = tag[tag.index(":") + 1:-1].strip()
    parts = [p.strip() for p in body.split(":") if p.strip()]
    if not parts:
        return None
    st = _strength_fmt(parts[1]) if len(parts) > 1 else None
    return parts[0] + (f" ({st:g})" if st is not None else "")


def _lora_dup_key(v: str) -> str:
    """LoRA 条目的去重键 —— 同一个文件可能以「带扩展名」和「裸名」两种形态出现。

    来源差异（2026-11-03 实测，同一张图里两者并存）：
      · WeiLinPromptUI `lora_str` 的 `lora` 字段 → "moss\\油画湿润厚涂二次元.safetensors"
      · 正文 `<wlr:…>` 标签的 parts[0]          → "moss\\油画湿润厚涂二次元"
    直接比字符串会各留一条（实测 5 条 → 9 条，后 4 条是同一批 LoRA 的裸名版本）。

    键 = 去尾部权重 + 统一斜杠 + 去扩展名 + 小写。**保留目录** —— 不同目录下的同名
    LoRA 是不同文件，不能合并；目录前是否带前缀由 `_lora_same` 再放宽。
    """
    s = re.sub(r"\s*\([-+]?\d+(?:\.\d+)?\)\s*$", "", str(v)).strip()
    s = s.replace("\\", "/")
    s = re.sub(r"\.(?:safetensors|ckpt|pt|pth|bin)$", "", s, flags=re.I)
    return s.strip().lower()


def _lora_same(k1: str, k2: str) -> bool:
    """两个去重键是否指同一个 LoRA：相等，或一方是另一方的路径后缀。

    放宽这一层是因为标签里常只写 basename（`油画湿润厚涂二次元`），而加载器的
    `lora_name` 带子目录（`moss\\油画湿润厚涂二次元.safetensors`）。按 `/` 边界比，
    避免 `moss/a` 与 `smoss/a` 这种字符后缀误判成同一个。
    """
    return k1 == k2 or k1.endswith("/" + k2) or k2.endswith("/" + k1)


def _lora_add(names: list, v: str) -> bool:
    """按上面两条口径去重后追加；真的追加了返回 True。"""
    k = _lora_dup_key(v)
    for x in names:
        if _lora_same(k, _lora_dup_key(x)):
            return False
    names.append(v)
    return True


def _is_lora_slot_node(ct_key: str, inputs: dict) -> bool:
    """带 LoRA 槽位的加载/堆叠节点：其 text 槽位存的是 <lora:...> 语法，不是提示词。

    例：LoraManager 的 Lora Loader / Lora Stacker——前端把加过的 LoRA 都写进 text，
    关掉槽位后标签仍留在文本里，早期版本会把它们当成正向提示词读出来。
    """
    if "trigger" in ct_key or not ("lora" in ct_key or "lyco" in ct_key):
        return False
    return any(k in inputs for k in ("loras", "lora_stack", "lora_name", "lora_name_1",
                                     "lora_data"))


def _wf_kind(obj: dict, ks: dict) -> str:
    """工作流类型：t2i=文生图 / i2i=图生图 / cn=ControlNet / ""=认不出。

    判定顺序（互斥，取优先级最高者）：
      1. 图里出现任何 ControlNet 节点 → cn
      2. 采样器的 latent_image 上游是 EmptyLatent* → t2i；是 VAEEncode* → i2i
      3. 兜底：整图里有 LoadImage 且没有 EmptyLatent → i2i
    """
    for n in obj.values():
        if isinstance(n, dict) and "controlnet" in _ct_key(n):
            return "cn"
    lat = _inp(ks).get("latent_image")
    if lat is None:
        # 自定义采样器把潜变量叫 latent（XB_ROCmKSampler）；漏了这条 ⇒ wfk 认不出 t2i
        lat = _inp(ks).get("latent")
    kind = [""]

    def visit(n):
        if kind[0]:
            return
        ct = _ct_key(n)
        if "empty" in ct and "latent" in ct:          # EmptyLatentImage / EmptySD3LatentImage…
            kind[0] = "t2i"
        elif "vaeencode" in ct:                       # VAEEncode / VAEEncodeForInpaint
            kind[0] = "i2i"

    if isinstance(lat, list) and lat and isinstance(lat[0], str):
        _walk_nodes(lat[0], obj, visit, stop_keys=("model", "clip"))
    if kind[0]:
        return kind[0]
    has_img = any(isinstance(n, dict) and "loadimage" in _ct_key(n) for n in obj.values())
    return "i2i" if has_img else ""


# 连线中转链的解析深度上限。
# B2-P2-1：旧值 6 是「防环」的粗估，但 rgthree「To String」/ 各类常量中转节点
# 串联时很容易超过 6 层（实测 11 层链 ⇒ sampler / steps / seed 全部静默丢失，
# 既不报错也不回退，合成出的工作流静默沿用模板默认值）。24 层足够覆盖真实工作流，
# 仍能兜住环（环内键名固定，走到上限就返回 None）；**超过上限时要记账**，
# 由 parse_comfy_prompt 汇总成 partial/warnings 回传，不静默。
_RESOLVE_MAX_DEPTH = 24


# 节点 inputs 里的「提示词快照」键名：形如 anima_v4_prompt / anima_v3_prompt。
# 注意 `prompt` 本身（API 格式的整份节点字典）不在其中 —— 它没有前导下划线。
_PROMPT_SNAP_RE = re.compile(r"^[A-Za-z0-9_]*_prompt$")


def _prompt_snapshot(obj: dict, reachable=None):
    """从节点 inputs 里取「提示词快照」JSON，返回其中一份 dict；没有返回 None。

    背景（2026-10-28 用户实测 ANIMA12 工作流）：有一类节点在**运行时**生成提示词
    （AnimaV4RandomPrompt 按权重抽角色/服装/构图），API prompt 里正文本来就不存在，
    只靠连线回溯只能读到触发词 —— 界面会显示半截提示词，**静默给错**。
    这类节点在保存时会把成稿写回自己的 inputs（键名 `<插件>_prompt`，JSON 里带
    positive / positive_full / negative / workflow_ok），这是唯一可靠来源。

    三重守卫，避免把别的工作流里的任意 JSON 误当提示词：
      ① 键名必须匹配 `*_prompt`；② 值必须是 JSON **对象**；
      ③ 对象里必须有非空字符串的 positive（或 positive_full）。
    另有 reachable（可给 None）：节点 id 白名单。工作流里留着**没接线**的旧采样节点
    是常态，不设这道闸就会把那张「废图」的提示词贴到当前图上。

    同一张图可能同时带 v3 / v4 两份 ⇒ 取 positive 最长的那份（更完整的成稿）。
    """
    best, best_len = None, -1
    for nid, n in obj.items():
        if not isinstance(n, dict):
            continue
        if reachable is not None and nid not in reachable:
            continue
        for k, v in _inp(n).items():
            if not (isinstance(k, str) and _PROMPT_SNAP_RE.match(k)):
                continue
            if not (isinstance(v, str) and v.lstrip().startswith("{") and len(v) <= (1 << 20)):
                continue
            try:
                d = json.loads(v)
            except ValueError:      # NaN/Infinity 也会落到这里，同样跳过
                continue
            if not isinstance(d, dict):
                continue
            pos = d.get("positive_full") or d.get("positive")
            if not (isinstance(pos, str) and pos.strip()):
                continue
            if len(pos) > best_len:
                best, best_len = d, len(pos)
    return best


# 自定义节点上「正文就是这个字符串」的字段白名单。
# 只收语义明确的键名 —— 任意字符串都当提示词会把模型名、路径、作者名一并读进来。
#   base_prompt → AnimaArtistPack（ANIMA 艺术家混合，实测 2026-11-02）
_PLAIN_PROMPT_KEYS = ("base_prompt",)


def _plain_prompt_value(obj: dict, nodes) -> str:
    """在给定节点集合里找最长的一段「正文型」纯文本字段；没有返回空串。

    与 `_prompt_snapshot` 的分工：那边认 JSON 快照（`*_prompt` 且值是对象），
    这边认**普通字符串字段**。同一个工作流可能两者都有，由调用方按优先级取舍。
    取最长的那份：一张图里可能有多个同类节点，接到正向链路上的那个才是当前图用的
    （调用方传入的 nodes 已限定为正向侧可达集合，见 parse_comfy_prompt）。
    """
    best = ""
    for nid in nodes:
        n = obj.get(nid)
        if not isinstance(n, dict):
            continue
        for k, v in _inp(n).items():
            if k not in _PLAIN_PROMPT_KEYS:
                continue
            if not (isinstance(v, str) and v.strip()):
                continue
            if len(v) > (1 << 16):      # 异常超大字段不当正文（防畸形 PNG 卡解析）
                continue
            if len(v) > len(best):
                best = v.strip()
    return best


# MOSS-Image 的「布局」工作流（MOSS_JSON_Importer / Layout Prompt 一类节点）：
# 提示词不是字符串，而是一份 JSON —— summary.caption 写整体画面，items[] 逐条写元素
# 的描述、要渲染的文字和边界框。节点把这份 JSON 编译成**一行紧凑 JSON** 送进
# CLIPTextEncode，链路上没有任何字面量文本节点（实测 2026-11-05：positive 直连
# PreviewAny → 导入节点的「完整提示词」输出）。
# 旧解析只认 text/prompt 等字符串字段，一路回溯全落空 ⇒ 整张图读到「正向提示词为空」，
# 面板上连「正向提示词」这一块都不出现（用户反馈「提示词读取好像有点不太对」那张琵琶图）。
_MOSS_LAYOUT_MAX = 1 << 16


def _moss_layout_text(v) -> str:
    """布局 JSON → 可读正文；不是 MOSS 布局返回空串。

    判据刻意收紧：顶层是对象 + summary.caption 是非空字符串 + items 是数组
    （元素逐条收 caption / text，空数组就只留整体描述）。只认这个结构，
    任意别的 JSON（LoRA 槽位表、工作流快照）不会误判成提示词。
    """
    if not (isinstance(v, str) and v.lstrip().startswith("{") and len(v) <= _MOSS_LAYOUT_MAX):
        return ""
    try:
        d = json.loads(v)
    except (ValueError, RecursionError):
        return ""
    if not isinstance(d, dict):
        return ""
    sm, items = d.get("summary"), d.get("items")
    if not isinstance(sm, dict) or not isinstance(items, list):
        return ""
    cap = sm.get("caption")
    if not (isinstance(cap, str) and cap.strip()):
        return ""
    lines = [cap.strip()]
    elems = []
    for it in items:
        if not isinstance(it, dict):
            continue
        c = it.get("caption") if isinstance(it.get("caption"), str) else ""
        t = it.get("text") if isinstance(it.get("text"), str) else ""
        c, t = c.strip(), t.strip()
        if t and c:
            elems.append('· "%s"（%s）' % (t, c))
        elif t:
            elems.append('· "%s"' % t)
        elif c:
            elems.append("· " + c)
    if elems:                       # 元素行另起一段：上面是整体描述，下面才是布局明细
        # 同一段描述可能对应多个框（左右手、两扇窗帘各一个框），逐条罗列在提示词面板里
        # 只是重复行，不影响「这张图画了什么」—— 按原顺序去重，保住第一次出现的位置。
        elems = list(dict.fromkeys(elems))
        lines.append("")
        lines.extend(elems)
    return "\n".join(lines).strip()


def _moss_layout_prompt(obj: dict, nodes) -> str:
    """在给定节点集合里找 MOSS 布局 JSON，返回可读正文；没有返回空串。"""
    for nid in nodes:
        n = obj.get(nid)
        if not isinstance(n, dict):
            continue
        for _k, v in _inp(n).items():
            if isinstance(v, str):
                txt = _moss_layout_text(v)
                if txt:
                    return txt
    return ""


def _flat_key(k) -> str:
    """输入名归一化：小写、去空格/下划线/连字符，便于比较中英混写的键名。"""
    if not isinstance(k, str):
        return ""
    return re.sub(r"[\s_\-]+", "", k.strip().lower())


# 「不是提示词正文」的输入名（2026-11-05 用户反馈「反推模板被读进正向」）。
# 反推 / 打标 / 图像理解这类节点（QwenTE_ImageInfer、WD14、Florence…）自己既有图片输入，
# 也有**给模型看的指令**（系统提示词 / negative prompt / 模板）。旧逻辑对任意连线都回溯，
# 于是「你是专注于图像反推提示词生成的专业AI助手…【最高指令】…」整段模板被当成正向正文，
# 拼接在实际提示词后面（实测 7489 字 / 实际正文 2265 字）。
# 只拦语义明确的旁路字段；text / prompt / string / 提示词 这些仍照常回溯。
_NON_PROMPT_KEY_PREFIX = ("system", "sysprompt", "negative", "negprompt",
                          "反向", "负向", "负面", "系统", "反推")
_NON_PROMPT_KEY_EXACT = ("neg", "template", "模板", "prompttemplate", "系统词")


def _is_non_prompt_key(k) -> bool:
    """该输入名是否「指令/模板」旁路，而不是提示词正文。"""
    n = _flat_key(k)
    if not n:
        return False
    if n in _NON_PROMPT_KEY_EXACT:
        return True
    return n.startswith(_NON_PROMPT_KEY_PREFIX)


# 运行时**产出**文本的节点（反推 / 打标 / 图像理解）：它的输入是「怎么生成」的配置，
# 不是当前这张图用的正向提示词；产出结果由下游文本框缓存，已由 visit 收到。
# 这类节点整体剪枝 —— 光靠键名拦不住把指令写在 prompt / 提示词 里的实现。
_GEN_NODE_HINTS = ("imginfer", "imageinfer", "caption", "tagger", "wd14",
                   "florence", "joycaption", "interrogator", "反推")


def _is_gen_node(n) -> bool:
    ct = _ct_key(n)
    return any(h in ct for h in _GEN_NODE_HINTS)


# 「运行时算出来的提示词」节点把**上一次**的执行结果写回自己的 widget，
# 等到**下一次**排队才被序列化进 PNG 的 prompt。因此文件里那一份永远差一轮。
# 只认语义明确的产出字段名；prompt / system_prompt 这类**指令**字段一律不碰
#（历史事故：反推节点的整段系统提示词被当成正向正文，实测 7489 字）。
_RUNTIME_CACHE_FIELDS = ("tags", "caption")


# 「文本输入是一条连线」的字段名 —— 顺着它往下追，能落到真正产出正文的那个节点。
# 用**全匹配**（含可选数字后缀）而不是子串：`prompt_file` / `negative_prompt` 这类
# 长得像但不是正文的键必须落在外面（前者是清单文件名，后者属于负向侧）。
_TEXT_EDGE_RE = re.compile(
    r"(?:text|prompt|t5xxl|clip_l|clip_g|string|value"
    r"|text_a|text_b|string_a|string_b|文本)(?:_\d+|\d+)?")

# 中文编号文本槽：ZML_SelectTextV2（「选择文本V2」）这类**拼装节点**的输入名是
# `文本1 … 文本5` + `启用1 … 启用5` + `分隔符`，按序号拼成一整段再交给 CLIPTextEncode。
# 2026-11-08 实测（E639D3B6….png）：键名是中文，`_TEXT_EDGE_RE` 只认英文 text/prompt
# ⇒ 字面量「best quality, highres」明明在文件里却一个字符都收不到，正向整段消失；
# 同时这个拼装节点被当成「生成时算出提示词的节点」报给用户（张冠李戴）。
_CN_TEXT_SLOT_RE = re.compile(r"文本(\d{1,2})")


def _slot_enabled(i2, k) -> bool:
    """中文编号文本槽的 `启用N` 开关；不是这种槽位、或没写开关键时一律视为启用。

    只在**关掉**时才写 `启用N=False` 是这类节点的普遍做法（实测 ZML_SelectTextV2
    的 启用4 / 启用5 为 False 且对应槽位为空），所以缺省必须按「启用」处理。
    """
    m = _CN_TEXT_SLOT_RE.fullmatch(k) if isinstance(k, str) else None
    if not m:
        return True
    en = i2.get("启用%s" % m.group(1), True)
    return not (en is False or en == 0)


def _cn_text_slots(i2) -> list:
    """中文编号文本槽 → [(序号, 键名, 值, 是否启用)]，按序号升序。

    调用方必须按「是否启用」过滤：关掉的槽位既不该收字面量，也不该顺它的连线上溯。
    """
    out = []
    if not isinstance(i2, dict):
        return out
    for k, v in i2.items():
        if not isinstance(k, str):
            continue
        m = _CN_TEXT_SLOT_RE.fullmatch(k)
        if not m:
            continue
        out.append((int(m.group(1)), k, v, _slot_enabled(i2, k)))
    out.sort(key=lambda x: x[0])
    return out

# 条件本身的**透传**字段：正向链路上常夹着 FluxKontextMultiReferenceLatentMethod、
# FluxGuidance、ConditioningCombine 这类「只搬条件、不产出文字」的节点。不穿过它们，
# 全库实测那批 Qwen-Edit 多角度图（ComfyUI_03547_ 等 42 张）就永远追不到真正的文本源。
# ⚠️ 刻意不含 negative / neg_*：那是另一侧的正文，穿过去就是把负向当正向读。
_TEXT_PASS_KEYS = ("conditioning", "conditioning_1", "conditioning_2",
                   "conditioning_to", "conditioning_from", "conditioning_to_start",
                   "positive", "pos",
                   # 展示型节点的透传口：PreviewAny.source / easy showAnything.anything
                   # 只是把上游的值摆出来，真正产出正文的是它们上游那一层。
                   "source", "anything")

# 产出节点自报「这次是从哪来的」的字段（RandomPromptPicker 的 prompt_file 等）：
# 只用于界面上的说明文字，不是正文。
_TEXT_SOURCE_HINT_FIELDS = ("prompt_file",)

# 直通型开关节点的输入名：ImpactSwitch（input1..inputN）、rgthree Any Switch（any_01…）。
# 2026-11-08 实测（5B9C944A….png）：CLIPTextEncode.text 接的是 Any Switch (rgthree)，
# 只认 input\d 时这条路走不过去，生成方被误报成开关本身。
_SWITCH_INPUT_RE = re.compile(r"(?:input|any)_?\d{1,2}")


def _text_source_tail(obj: dict, entry):
    """从条件入口往上游追，返回运行时产出节点 {"node","file"}；没有返回 None。

    2026-11-07 第二例（EFCFF29….png）：`CLIPTextEncode.text ← RandomPromptPicker`，
    该节点执行时从 `krea2_prompts.txt` 里随机抽一句送进采样器，抽中的那句**任何地方都没落盘**。
    与打标/反推节点是同一种病，只是类名不同 —— 老口径按类名白名单匹配，这类节点一条都盖不到，
    面板于是什么都不显示（用户看到的就是「读取有问题」）。

    判据（三条，缺一条就会误报）：
      · **必须由文本键进入**：只有「靠 text / prompt / string… 这条连线到达」的节点才算产出者。
        条件透传键只负责把路走通 —— 纯图生图链路（CLIPVisionEncode 那种，压根没有文字）
        最后一步是 image 键，不会被当成「运行时产出提示词」；
      · 这条支路**一路没有非空字面量**：有字面量说明文件里有正文，直接剪掉这一支；
        空串字面量是「这侧本来就没提示词」，同样不算（入口节点自己就是这种情形）；
      · 追到没有文本连线的末端为止，中途继续往下追（StrConcat / easy promptList 这类拼装节点）。
    """
    if not entry:
        return None
    # via: 0=入口（条件侧进来）/ 1=由文本键进来 / 2=由条件透传键进来
    seen, stack, hits = set(), [(str(entry), 0)], []
    while stack:
        nid, via = stack.pop()
        if nid in seen or nid not in obj:
            continue
        seen.add(nid)
        n = obj[nid]
        if not isinstance(n, dict):
            continue
        i2 = _inp(n)
        if "zeroout" in _ct_key(n):
            continue                     # 条件被清零：这一侧确实是空的
        literal, tlinks, plinks = False, [], []
        is_switch = "switch" in _ct_key(n)
        for k, v in i2.items():
            is_text = _TEXT_EDGE_RE.fullmatch(k) is not None
            if is_text and not _slot_enabled(i2, k):
                continue                 # 关掉的文本槽不参与拼接，别顺它上溯
            if isinstance(v, str):
                if is_text and v.strip():
                    literal = True
                continue
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                if is_text:
                    tlinks.append(v[0])
                elif k in _TEXT_PASS_KEYS or (
                        is_switch and _SWITCH_INPUT_RE.fullmatch(k)
                        and (_switch_selected_input(n) or k) == k):
                    plinks.append(v[0])
        if literal:
            # 2026-11-08（E639D3B6….png）：拼装型节点可能**一半槽位是字面量、一半是连线**
            # （文本1 = "best quality, highres"，文本3 ← ImageCaptionNode）。
            # 旧逻辑一见字面量就整支剪掉 ⇒ 真正在生成时产出正文的节点永远追不到，
            # 面板只能把拼装节点本身当成产出者。有文本连线就继续追：
            # 字面量那部分由 collect_texts 收，这里只负责回答「还缺哪一截、谁产的」。
            if not tlinks:
                continue                 # 这一支在文件里有正文
        if tlinks:
            for x in tlinks:
                stack.append((x, 1))
            continue                     # 还有文本上游 ⇒ 它不是源头
        for x in plinks:
            # 直通开关只是把文本原样搬过去，**不改变「这是不是文本上下文」**。
            # 2026-11-08 实测（5B9C944A….png）：CLIPTextEncode ← Any Switch (rgthree)
            # ← ImageCaptionNode，旧逻辑在开关处按「条件透传」降级成 via=2 ⇒ 真正的
            # 产出节点不被记录，面板把生成方写成「Any Switch (rgthree)」（张冠李戴）。
            stack.append((x, 1 if (is_switch and via <= 1) else 2))
        # 有文本上游的节点是**拼装者**，不是产出者 —— 否则 ZML_SelectTextV2 会顶替
        # 真正在生成时算文本的那个节点（这正是本次 E639D3B6 报错的形态）。
        if via == 1 and not plinks and not tlinks:
            hint = ""
            for k in _TEXT_SOURCE_HINT_FIELDS:
                v = i2.get(k)
                if isinstance(v, str) and v.strip():
                    hint = v.strip()
                    break
            hits.append((nid, str(n.get("class_type") or ""), hint))
    if not hits:
        return None
    # 多个末端（StrConcat 之类两边都是连线）时取 id 最小的那个，保证同一张图结果稳定
    nid, cls, hint = sorted(hits, key=lambda x: (len(x[0]), x[0]))[0]
    return {"id": nid, "node": cls, "file": hint}


def _downstream_display_cache(obj: dict, pid, depth: int = 4) -> str:
    """产出节点**下游展示节点**里残留的那段文本（上一轮运行的结果）。

    2026-11-08 实测（5B9C944A….png）：正向是 ImageCaptionNode（VLM 反推）当场生成的，
    文件里没有；用户在工作流里看到的那段中文，是下游 `easy showAnything.text`
    —— 它由 `anything ← Any Switch ← ImageCaptionNode` 拿到**上一轮**的反推结果。
    不说清楚，用户只会觉得「明明有提示词，你却读不出来」。
    只认展示类节点（show / display / textbox），且必须能在 depth 跳内回溯到产出节点；
    取不到就返回空串（绝不拿无关节点的文本凑数）。
    """
    best = ""
    pid = str(pid)
    for nid, n in obj.items():
        if not isinstance(n, dict):
            continue
        ct = _ct_key(n)
        if "encode" in ct or "concat" in ct:
            continue
        if not any(h in ct for h in ("show", "display", "textbox", "previewany")):
            continue
        i2 = _inp(n)
        seen, stack, ok = set(), [], False
        for v in i2.values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                stack.append((v[0], 1))
        while stack:
            x, d = stack.pop()
            if x == pid:
                ok = True
                break
            if d >= depth or x in seen or x not in obj:
                continue
            seen.add(x)
            nn = obj.get(x)
            if isinstance(nn, dict):
                for v in _inp(nn).values():
                    if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                        stack.append((v[0], d + 1))
        if not ok:
            continue
        for k in ("text", "text_0", "string", "value", "anything"):
            v = i2.get(k)
            if isinstance(v, str) and v.strip() and len(v) <= (1 << 16):
                if len(v.strip()) > len(best):
                    best = v.strip()
    return best


def _runtime_prompt_node(obj: dict, nodes, entry=None, strict: bool = False) -> dict:
    """正向链路上是否有「运行时才产出文本」的节点；有则回 {"node","title","cache","file"}。

    调用方在**正向确实为空**时用它解释「为什么读不到」；正向非空但链路里夹着反推节点时
    （部分可读）也会调用，此时 `strict=True` —— 只认真的顺着文本连线追到的产出者，
    不许退回按类名白名单猜（否则普通工作流会平白多出一句「只读到一部分」）。
    这只解释、绝不补内容。
    判据以 `_text_source_tail`（顺着文本连线追到的那一个）为准 —— 它比类名白名单准：
      · 反例：Florence2 那条链是 Florence2Run → StringReplace×3 → PreviewAny → StrConcat → CLIPTextEncode，
        可达集合里同时有 Florence2Run 和 DownloadAndLoadFlorence2Model（类名都含 florence），
        按类名取「第一个命中的」会报成模型加载器（全库实测 22 张全是这句错话）。
      · cache 只从**产出者本人**的产出字段取（它上一次运行留下的残留）；本人没有产出字段时，
        才退一步取**下游展示节点**里那段残留（见 `_downstream_display_cache`）——
        随机抽取类节点两处都没有，就不给缓存。
    title 取节点 `_meta.title`（ComfyUI 里用户自己起的名字，如「✨图像反推提示词」），
    给界面拼成「标题（类名）」，比只报类名好认。
    找不到文本末端时退回老口径（类名命中），至少说出是谁，不静默。
    """
    tail = _text_source_tail(obj, entry)
    if tail:
        cache = ""
        title = ""
        n = obj.get(tail["id"])
        if isinstance(n, dict):
            for k in _RUNTIME_CACHE_FIELDS:
                v = _inp(n).get(k)
                if not (isinstance(v, str) and v.strip()) or len(v) > (1 << 16):
                    continue
                if len(v.strip()) > len(cache):
                    cache = v.strip()
            m = n.get("_meta")
            if isinstance(m, dict):
                t = m.get("title")
                if isinstance(t, str) and t.strip():
                    title = t.strip()
        if not cache:
            cache = _downstream_display_cache(obj, tail["id"])
        return {"node": tail["node"], "title": title,
                "cache": cache, "file": tail.get("file") or ""}
    if strict:
        return None                      # 正向非空时不做类名兜底：宁可不说，不许瞎指
    for nid in sorted(nodes, key=lambda x: (len(str(x)), str(x))):
        n = obj.get(nid)
        if isinstance(n, dict) and _is_gen_node(n):
            return {"node": str(n.get("class_type") or ""), "title": "", "cache": ""}
    return None


def _dedupe_texts(txts) -> list:
    """同一段文本只留一份，并去掉被更长段完整包含的段。

    2026-11-05 实测（QwenTE 反推工作流）：
      CLIPTextEncode.text → TextInput_ → ShowText|pysssss.text →
      NaibaTextbox(passthrough) → Textbox(passthrough) → StrConcat
    ShowText / NaibaTextbox / Textbox 三个节点都把**同一段成稿**缓存在自己的
    text / text_0 上（运行时写回），旧逻辑只对类名含 show/display 的第一个跳过，
    后两个照收 ⇒ 正向变成「正文 + 正文 + 模板」，7489 字里有 2265 字是重复的。
    拼接顺序是「下游缓存先访问、上游原文后访问」，所以这里不按位置取舍：
    完全相同的只留首次出现；被别的段完整包含的（StrConcat 拼出来的前缀等）直接丢弃。
    """
    uniq, seen = [], set()
    for t in txts:
        if not t:
            continue
        if t in seen:
            continue
        seen.add(t)
        uniq.append(t)
    if len(uniq) < 2:
        return uniq
    # 包含关系只对「成段正文」生效（≥4 字）：单两个字符的触发词/标签不参与，
    # 避免 "a" 被 "cat" 这种偶然包含吃掉。
    return [t for t in uniq
            if not (len(t) >= 4 and any(t != u and t in u for u in uniq))]


def parse_comfy_prompt(obj: dict) -> dict:
    """ComfyUI API 格式 prompt（节点字典）→ 人类可读摘要。"""
    s = {}
    _deep_hits = []          # 超过 _RESOLVE_MAX_DEPTH 的键名（B2-P2-1 的回执）
    _empty_sides = []        # 确认「该侧提示词就是空」的侧别（B2-P2-4 的回执）
    if not isinstance(obj, dict):        # 畸形 PNG：prompt 解出来可能压根不是节点字典
        return s
    ks = None
    # 采样节点按优先级分三轮找：标准 KSampler 系 > SamplerCustom 系 > 带 sampler_name 的兜底。
    # 必须分级而不是合在一轮：一张图可能有多个采样阶段（先生成、再高清修复），
    # 合在一起会变成「谁先出现谁当选」，把原本稳定的结果换掉。
    for n in obj.values():
        if isinstance(n, dict) and n.get("class_type") in ("KSampler", "KSamplerAdvanced"):
            ks = n
            break
    if ks is None:      # Flux / MiniMaxH3 等新式工作流：SamplerCustom / SamplerCustomAdvanced
        for n in obj.values():
            if isinstance(n, dict) and n.get("class_type") in ("SamplerCustom", "SamplerCustomAdvanced"):
                ks = n
                break
    if ks is None:  # 兜底①：找任何带 sampler_name 的节点
        for n in obj.values():
            if not isinstance(n, dict):
                continue
            ct = _ct_key(n)
            # KSamplerSelect / SamplerSelector 只是「采样器选择器」，本身不采样。
            # 选中它会让模型 / 提示词 / 步数 / 种子全部读不到
            #（SamplerCustomAdvanced 工作流的典型症状：只剩「采样器」一行）。
            if "select" in ct or "selector" in ct:
                continue
            if "sampler_name" in _inp(n):
                ks = n
                break
    if ks is None:  # 兜底②：输入名是 sampler 而不是 sampler_name 的自定义采样器
        # 2026-10-28 实测（XB_ROCmKSampler，ComfyUI-XB 节点包）：它把采样器名字放在
        # `inputs.sampler`（不是 sampler_name）、潜变量放在 `latent`（不是 latent_image）。
        # 上面三轮全落空 ⇒ 本函数直接 return {}，界面上这张图「没有生成参数」，
        # 连步数 / 种子都没有 —— 而 PNG 里的 prompt 其实完整无缺。
        # 判据必须带「真在采样」的证据（steps / cfg / denoise 至少一个）：否则只挂一个
        # sampler 名字的 UI 节点（选择器）会被选中，重演上面那条注释里的症状。
        for n in obj.values():
            if not isinstance(n, dict):
                continue
            ct = _ct_key(n)
            if "select" in ct or "selector" in ct:
                continue
            _i2 = _inp(n)
            if "sampler" not in _i2:
                continue
            if not any(k in _i2 for k in ("steps", "cfg", "denoise")):
                continue
            ks = n
            break
    if ks is None:
        return s

    def resolve_str(v, key="", depth=0):
        """名称类输入（sampler_name/scheduler）可能是连线引用，沿连线取真实字符串。

        例：ClownsharKSampler_Beta.sampler_name → ClownSamplerSelector_Beta，
        中间还可能隔着 rgthree「To String」等中转节点。
        """
        if isinstance(v, str):
            return v.strip() or None
        if depth > _RESOLVE_MAX_DEPTH:
            # B2-P2-1：走到上限说明链路比预想的长，本参数取不到 —— 记账，
            # 由 parse_comfy_prompt 汇总成 _warn 回传（旧行为是静默 None）。
            if key:
                _deep_hits.append(key)
            return None
        if not (isinstance(v, list) and v and isinstance(v[0], str)):
            return None
        up = obj.get(v[0])
        if not isinstance(up, dict):
            return None
        uinp = _inp(up)
        for k in (key, "value", "string", "text", "sampler_name", "scheduler"):
            if k and k in uinp and uinp[k] is not None:
                r = resolve_str(uinp[k], key, depth + 1)
                if r:
                    return r
        for _k, val in uinp.items():      # 兜底：跟随首个连线引用（中转节点）
            if isinstance(val, list) and val and isinstance(val[0], str):
                r = resolve_str(val, key, depth + 1)
                if r:
                    return r
        return None

    inp = _inp(ks)

    def from_ref(node_ref, key):
        """取「引用节点」身上的某个字段（只下一层，不做连线兜底遍历）。"""
        if not (isinstance(node_ref, list) and node_ref and isinstance(node_ref[0], str)):
            return None
        up = obj.get(node_ref[0])
        return _inp(up).get(key) if isinstance(up, dict) else None

    seed = next((inp[k] for k in ("seed", "noise_seed", "seed_value")
                 if inp.get(k) is not None), None)
    if seed is None:                    # SamplerCustom*：种子在 noise 节点（RandomNoise.noise_seed）
        seed = from_ref(inp.get("noise"), "noise_seed")

    def resolve_num(v, keys, depth=0):
        """数值输入（seed/steps/cfg）可能是连线引用，沿连线取真实数值。"""
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            if isinstance(v, float) and not math.isfinite(v):
                return None      # B2-P1-1：prompt JSON 里的 NaN / Infinity 不往下传
            try:
                return int(v) if float(v).is_integer() else v
            except (OverflowError, ValueError):
                return v if isinstance(v, int) else None
        if depth > _RESOLVE_MAX_DEPTH:
            _deep_hits.append(keys[0] if keys else "")   # 同上：超深要记账
            return None
        if not (isinstance(v, list) and v and isinstance(v[0], str)):
            return None
        up = obj.get(v[0])
        if not isinstance(up, dict):
            return None
        uinp = _inp(up)
        for k in keys:
            if k in uinp and uinp[k] is not None:
                return resolve_num(uinp[k], keys, depth + 1)
        return None

    # 采样器名字的字段名有两种拼写：标准 KSampler 用 sampler_name，
    # 自定义采样器（XB_ROCmKSampler 等）用 sampler。写死前者会让「采样器」与
    # 紧随其后的 scheduler 一起静默丢失（下面循环里 v is None 直接 continue）。
    _sn_key = "sampler_name" if inp.get("sampler_name") is not None else "sampler"
    for k_src, k_dst, num_keys in (
            (_sn_key, "sampler", None), ("scheduler", "scheduler", None),
            ("steps", "steps", ("steps", "steps_total", "value")),
            ("cfg", "cfg", ("cfg", "value"))):
        v = inp.get(k_src)
        if v is None:
            continue
        if num_keys:
            v = resolve_num(v, num_keys)
        elif not isinstance(v, str):
            v = resolve_str(v, k_src)   # 名称可能是连线引用，沿连线取真实字符串
        if v is not None and not isinstance(v, (list, dict)):
            s[k_dst] = v
    if not s.get("sampler"):
        # SamplerCustom*：采样器名字挂在 sampler 引用的节点上（KSamplerSelect.sampler_name）
        _sn = from_ref(inp.get("sampler"), "sampler_name")
        if isinstance(_sn, str) and _sn.strip():
            s["sampler"] = _sn.strip()
    if seed is not None:
        seed = resolve_num(seed, ("seed", "value", "noise_seed"))
    if seed is not None:
        s["seed"] = seed

    def ref(k):
        v = inp.get(k)
        return v[0] if isinstance(v, list) and v and isinstance(v[0], str) else None

    tag_loras = []                       # 提示词里 <lora:...> 标签解析出的 LoRA（兜底用）

    def _neg_input_keys(n):
        """节点上「负向专属」输入名（negative_prompt / negative / neg_prompt …）。

        双输出编码节点（如 TextEncodeQwenImage21：单节点同时输出正向/负向两路条件，
        KSampler.positive=[id,0]、negative=[id,1] 指向同一节点）靠这些字段区分两侧。
        """
        return [k for k in _inp(n)
                if k.startswith("negative") or k == "neg_prompt"]

    def cond_entry(side):
        """该侧条件链路的起点节点 id。

        标准 KSampler：inputs.positive / inputs.negative；
        SamplerCustom*（Flux / MiniMaxH3 等新式工作流）：没有 positive/negative，
        条件挂在 inputs.guider 上：
          · CFGGuider —— 自己的 positive / negative（双侧）
          · BasicGuider —— 只有 conditioning（**单侧**，故 negative 返回 None，不虚构负向提示词）
        """
        v = inp.get(side)
        if isinstance(v, list) and v and isinstance(v[0], str):
            return v[0]
        g = inp.get("guider")
        if not (isinstance(g, list) and g and isinstance(g[0], str)):
            return None
        gn = obj.get(g[0])
        if not isinstance(gn, dict):
            return None
        gi = _inp(gn)
        gv = gi.get(side)
        if isinstance(gv, list) and gv and isinstance(gv[0], str):
            return gv[0]
        if side == "positive":
            c = gi.get("conditioning")
            if isinstance(c, list) and c and isinstance(c[0], str):
                return c[0]
        return None

    def collect_texts(start_key):
        side = start_key                 # "positive" / "negative"
        other = "negative" if side == "positive" else "positive"
        keys = ("text", "prompt", "t5xxl", "clip_l", "clip_g", side)   # 兼容 Flux/SD3 双编码、
                                                                       # Qwen-Image-Edit / Flux2-Klein-Edit
                                                                       # 等编辑类 TextEncode（字段名是 prompt）、提示词 UI 节点
        direct = inp.get(start_key)
        if isinstance(direct, str) and direct.strip():
            txt, tags = _split_lora_tags(direct.strip())
            tag_loras.extend(tags)
            return [txt] if txt else []
        texts = []
        # 显示类节点去重模式：False = 跳过缓存副本只收上游（首轮）；
        # 整条链路收不到文本时置 True 重走一遍（旧行为兜底，保证 0 丢失）。
        cache_ok = False

        def _prune_node(n):
            # 条件被清零 ⇒ 该侧没有提示词；反推/打标节点 ⇒ 上游是模板不是正文
            return ("zeroout" in _ct_key(n)) or _is_gen_node(n)

        def _ref_selected(n, k):
            # 直通开关只走 select 指向的那一路：同时接着 A/B 两条提示词分支时，
            # 全走会把 A+B 拼成本侧正文（本侧其实只用了其中一条）。
            sel = _switch_selected_input(n)
            if sel is not None and sel != k:
                return False
            # 中文编号文本槽的 `启用N=False` ⇒ 这一路没参与拼接，别顺着它收文本
            # （ZML_SelectTextV2 的 文本4/文本5 常留着旧连线但被关掉）。
            return _slot_enabled(_inp(n), k)

        def _links_ahead(n):
            # 该节点是否还有「会被继续回溯」的连线输入（与 _walk_nodes 同一套 stop_keys）
            for k, v in _inp(n).items():
                if k in ("model", "clip", other):
                    continue
                if _is_non_prompt_key(k):
                    continue
                if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                    return True
            return False
        def visit(n):
            ct = _ct_key(n)
            i2 = _inp(n)
            if _is_gen_node(n):
                # 反推 / 打标 / 图像理解节点：整体不收。
                # `_prune_node` 只挡住「继续向上回溯」，而 `_walk_nodes` 是**先 visit 再
                # prune** ⇒ 节点自身的 prompt / text 字段仍会被下面的候选循环收下。
                # 2026-11-05 增量复读实测：反推节点把指令写在 `prompt`（不是中文键名
                # `系统提示词`）时，模板照样拼进正向 —— 键名黑白名单管不到这条。
                # 产出结果由下游文本框缓存，那一份已由 visit 收到，这里 return 不丢正文。
                return
            if _is_lora_slot_node(ct, i2):
                return                   # 槽位节点的 text 是 LoRA 语法，不是提示词
            # 显示类节点（easy showAnything / ShowText|pysssss 等）的字面 text /
            # text_N 是运行时写回的缓存副本，上游链路里还有同一份原文。
            # 有可回溯上游时跳过缓存改收上游，避免同一段提示词被收两遍
            # （2026-09-19 实测：Krea2 工作流 easy showAnything 缓存 A+B，
            #   上游 CR Text×2 又是 A、B，旧逻辑正向变成 A+B+A+B）。
            if not cache_ok and ("show" in ct or "display" in ct) and _links_ahead(n):
                return
            cand = keys
            if "string" in ct or "primitive" in ct:
                # PrimitiveStringMultiline 用 value；KJNodes StringConstant /
                # StringConstantMultiline 用 string（INPUT_TYPES 里的 required 键就叫 string）。
                # 2026-10-06 实测：漏了 string 会让 Krea2 + TE_MAN 工作流读不到正文 ——
                # 首轮收不到文本 ⇒ 回退收「显示节点缓存」⇒ 界面显示上一张图的提示词。
                cand = keys + ("value", "string")
            if "triggerword" in ct:
                # LoraManager TriggerWord Toggle：LoRA 触发词放在 orinalMessage
                # （字段名就是少一个 g 的拼写，值与其前端 active 词表一致）。
                # 该节点常经 JoinStrings 拼在正文之前，属正向提示词的一部分。
                # 2026-10-06 全库 9683 张实测：此字段唯 TriggerWord Toggle 一类节点持有
                # （1042 处，且多数未接进提示词链路）⇒ 加它不误伤其它工作流。
                cand = cand + ("orinalMessage",)
            # 通配符提示词节点：正文在两个专用字段里，没有 text / prompt
            #（2026-11-08 用户反馈 Anima_00126_：CLIPTextEncode ← ImpactSwitch ←
            #  ImpactWildcardProcessor，正负向**同时**读空，界面像「没有提示词」）。
            # 优先 populated_text（实际送进采样器的那份），空则退回 wildcard_text。
            # 「populated_text 就是本张图的正文」有源码依据，不是猜的：
            #   · 排队时 Impact 的 onprompt 钩子（impact_server.py:552）在 populate 模式下
            #     把 wildcard_text 的处理结果**写回** populated_text，并把 mode 改成 reproduce；
            #   · doit()（impact_pack.py:2700）直接拿 populated_text 当输入。
            #   ⇒ PNG 里存的 mode 已是 reproduce，那一份就是这一张用的正文，不是上一轮的残留。
            # 命中即 return：这类节点的正文就是它自己，上游只有 model / clip。
            _wc = i2.get("populated_text")
            if not (isinstance(_wc, str) and _wc.strip()):
                _wc = i2.get("wildcard_text")
            if isinstance(_wc, str) and _wc.strip():
                txt, tags = _split_lora_tags_node(ct, _wc.strip())
                tag_loras.extend(tags)
                if txt:
                    texts.append(txt)
                return
            hit = False
            for k in cand:
                t = i2.get(k)
                if isinstance(t, str) and t.strip():
                    txt, tags = _split_lora_tags_node(ct, t.strip())
                    tag_loras.extend(tags)
                    if txt:
                        texts.append(txt)
                    hit = True
                    break
            if hit:
                return
            # 多块提示词节点（ComfyUI-Prompt-Composer 的 promptComposerTextMultiple 等）：
            # 提示词被拆在 text_1 … text_N 上，没有单数的 text / prompt 字段，按 N 升序全收。
            # 放在 keys 之后是刻意的 —— 命中标准字段就保持原行为，不被这条分支抢走。
            # ⚠️ 这是「通配」：显示类节点（ShowText|pysssss 等）也带 text_N，其值是上游文本的
            # 缓存，当前一并读出（实测全库 9038 张只影响 7 张，丢失 0 / 改写 0）；
            # 若日后要收紧语义，把这行换成 class_type 白名单判断即可。
            blocks = []
            for k2, t2 in i2.items():
                m2 = _MULTI_TEXT_RE.fullmatch(k2)
                if m2 and isinstance(t2, str) and t2.strip():
                    # B2-P1-4：Python 3.11+ 对超长十进制串转 int 有 4300 位上限，
                    # `text_<4400 位数字>` 这样的畸形键会抛 ValueError 把整条解析链带崩
                    # （外层 except 吞掉 ⇒ 界面只剩「没有生成参数」）。这种键本来就不
                    # 是多块提示词，跳过即可。
                    try:
                        _bi = int(m2.group(1))
                    except ValueError:
                        continue
                    blocks.append((_bi, t2.strip()))
            for _n, t2 in sorted(blocks):
                txt, tags = _split_lora_tags_node(ct, t2)
                tag_loras.extend(tags)
                if txt:
                    texts.append(txt)
            # ComfyUI 核心 StringConcatenate（string_a / string_b / delimiter）：
            # 两个槽位既可能是字面量、也可能是连线。字面量此前**完全收不到**
            # —— 候选字段里没有 string_a / string_b，也不是 text_N，
            # 于是「masterpiece, best quality, score_9…」这类质量前缀整段消失，
            # 界面上只剩触发词 / 下游节点那一截（2026-10-28 实测 ANIMA12 工作流）。
            # 只收字面量：是连线的那一侧交给 _walk_nodes 继续回溯，避免两边重复收。
            if "stringconcat" in ct:
                for _ca in ("string_a", "string_b"):
                    _ta = i2.get(_ca)
                    if isinstance(_ta, str) and _ta.strip():
                        txt, tags = _split_lora_tags_node(ct, _ta.strip())
                        tag_loras.extend(tags)
                        if txt:
                            texts.append(txt)
            # 中文编号文本槽（ZML_SelectTextV2「选择文本V2」及同形节点）：
            # `文本1 … 文本5` 各配一个 `启用N` 开关，按序号用 `分隔符` 拼成一整段。
            # 2026-11-08 实测（E639D3B6….png）：CLIPTextEncode.text ← ZML_SelectTextV2，
            # 文本1 = "best quality, highres" 是**字面量而且启用**，却因为键名是中文
            # 一个字符都收不到 ⇒ 正向整段消失，面板还把这个拼装节点误报成产出者。
            # 只收字面量：连线那几槽交给 _walk_nodes 继续回溯（同样是避免两边重复收）；
            # 关掉的槽位直接跳过（启用4 / 启用5 常留着旧连线但没参与拼接）。
            for _sn, _sk, _sv, _son in _cn_text_slots(i2):
                if not _son or not (isinstance(_sv, str) and _sv.strip()):
                    continue
                txt, tags = _split_lora_tags_node(ct, _sv.strip())
                tag_loras.extend(tags)
                if txt:
                    texts.append(txt)
        # —— L2（2026-09-27）：双输出编码节点的侧别隔离 ——
        # 用户实测（QW21-20260922145354_00001_.png，Qwen-Image 2.1 工作流）：
        #   KSampler.positive=["522",0]、negative=["522",1] 指向同一个
        #   TextEncodeQwenImage21 节点，该节点 prompt=["517",0] 是连线（正向正文在上游）、
        #   negative_prompt="" 是空字面量。旧逻辑忽略输出槽位，负向侧也从本节点的
        #   prompt 连线一路爬到 517 ⇒ 负向 = 正向的复制。
        # 规则：负向侧若入口节点带 neg* 专属字段，则【只认这些字段】——
        #   字面量直接用（空就是空，CFG=1 的负向本就常空）；连线只沿该连线回溯；
        #   绝不顺 prompt/text 等通用字段爬上游。正向侧对称保护：不许回溯进 neg* 连线。
        ent_id = cond_entry(side)
        ent = obj.get(ent_id) if isinstance(ent_id, str) else None
        negk = _neg_input_keys(ent) if isinstance(ent, dict) else []
        if side == "negative" and negk:
            for k in negk:
                t = _inp(ent).get(k)
                if isinstance(t, str):
                    if t.strip():
                        txt, tags = _split_lora_tags_node(ct, t.strip())
                        tag_loras.extend(tags)
                        if txt:
                            texts.append(txt)
                    continue            # 空字面量 ⇒ 该字段就是空，不虚构、不兜底
                if isinstance(t, list) and len(t) == 2 and isinstance(t[0], str):
                    def _wneg(cache, _ref=t):
                        nonlocal cache_ok
                        cache_ok = cache
                        _walk_nodes(_ref[0], obj, visit,
                                    stop_keys=("model", "clip", other),
                                    prune=_prune_node, skip_key=_is_non_prompt_key,
                                    ref_filter=_ref_selected)
                    _wneg(False)
                    if not texts:
                        _wneg(True)     # 与主路径同款：首轮空则回退收缓存（防 0 丢失变丢失）
                    break
            return texts                # neg* 字段全空 ⇒ 负向为空（比旧逻辑的「抄正向」正确）
        # 正向侧：入口节点带 neg* 字段时，禁止回溯进这些连线（负向文本不得混入正向）
        stop = ("model", "clip", other)
        if side == "positive" and negk:
            stop = stop + tuple(negk)

        # 只沿条件/文本连线回溯：不钻进 clip/model 加载器管线，也不跨到另一侧
        # （否则 LoRA 加载器槽位、对面提示词都会被当成本侧提示词）；
        # ConditioningZeroOut 把条件清零，该侧就没有提示词可读；
        # 反推/打标节点整体剪枝（它的系统提示词是模板，不是正文）。
        def _walk(cache):
            nonlocal cache_ok
            cache_ok = cache
            _walk_nodes(cond_entry(side), obj, visit,
                        stop_keys=stop, prune=_prune_node,
                        skip_key=_is_non_prompt_key, ref_filter=_ref_selected)
        _walk(False)
        if not texts:
            _walk(True)                  # 首轮只收上游没收到 → 回退旧行为收缓存
        return texts

    # B2-P2-4：区分「本图该侧确认为空」与「本图该侧解析不到」。
    #   链路入口在、但一路没收着文本（典型：CFG=1 的 Flux 工作流，负向字面量就是空串）
    #   ⇒ 确认空，合成模板时应把该侧写成空串，而不是把模板自带的提示词留下；
    #   连入口都没有（找不到 positive/negative/guider 连线）⇒ 解析不到 ⇒ 保留模板原值。
    # 旧行为两种情况一律「不写」，用户拿到的负向其实是模板作者写的，且完全无声。
    _walked = {}
    for _side in ("positive", "negative"):
        # 同一段成稿会被多个文本框节点各缓存一份（见 _dedupe_texts 注释）：
        # 不去重就会「正文 + 正文 + 模板」，界面读起来全是重复段落。
        _txts = _dedupe_texts(collect_texts(_side))
        _walked[_side] = _txts
        if _txts:
            s[_side] = "\n".join(_txts)
        elif cond_entry(_side) is not None:
            _empty_sides.append(_side)

    # —— 运行时生成提示词的工作流：用好节点自带的成稿快照 ——
    # 放在连线回溯**之后**：快照是保存那一刻的最终文本（含质量前缀、已展开的角色/服装
    # 标签），比连线能凑出来的那半截权威；为空则完全不动（不覆盖更准的连线结果）。
    # 先算「条件链路上能走到的节点」，只在那批节点里找快照 —— 工作流里留着没接线的旧
    # 采样节点是常态，不设这道闸就会把废图的提示词贴到当前图上。
    _reach = set()
    _pos_reach = set()                     # 正向侧单留一份：base_prompt 只认正侧（见下方兜底）
    for _side in ("positive", "negative"):
        _e = cond_entry(_side)
        if _e is None:
            continue
        _stack = [_e]
        while _stack:                      # 与 _walk_nodes 同口径：不钻 model / clip
            _nid = _stack.pop()
            if not _nid or _nid in _reach or _nid not in obj:
                continue
            _reach.add(_nid)
            if _side == "positive":
                _pos_reach.add(_nid)
            _nn = obj[_nid]
            if not isinstance(_nn, dict):
                continue
            for _k2, _v2 in _inp(_nn).items():
                if _k2 in ("model", "clip"):
                    continue
                if isinstance(_v2, list) and len(_v2) == 2 and isinstance(_v2[0], str):
                    _stack.append(_v2[0])
    _snap = _prompt_snapshot(obj, _reach)
    if _snap:
        _p = _snap.get("positive_full") or _snap.get("positive") or ""
        if isinstance(_p, str) and _p.strip():
            _p = _p.strip()
            # 快照正文里同样可能夹着 LoRA 标签：A1111 的 <lora:…>、WeiLinPromptUI 的
            # <wlr:名字:模型强度:文本编码强度:触发词强度>。旧实现只剥连线收到的文本，
            # 快照这一段原样落进 s["positive"] ⇒ 用户看到提示词开头一排标签，而 LoRA
            # 列表里没有它们（2026-11-02 用户反馈「读不出完整的 LoRA」）。
            _p, _p_tags = _split_lora_tags(_p)
            if _p_tags:
                tag_loras.extend(_p_tags)
            # 快照只是「插件自己产出的那一段」，不含下游再拼上去的东西
            # （ANIMA12 实测：触发词 @4x0style 挂在 TriggerWord Toggle 上，不在 positive_full 里）。
            # 连线收到、又没被快照包含的文本 = 这类下游补充，按原样保留在前面
            # （触发词的惯例位置就是正文之前，LoraManager 自己的界面也是这么排的）。
            _keep = [t for t in (_walked.get("positive") or [])
                     if t.strip() and t.strip() not in _p]
            if _p.strip():
                s["positive"] = "\n".join(_keep + [_p]) if _keep else _p
                if "positive" in _empty_sides:
                    _empty_sides.remove("positive")
        _n2 = _snap.get("negative")
        if isinstance(_n2, str) and _n2.strip():
            s["negative"] = _n2.strip()
            if "negative" in _empty_sides:
                _empty_sides.remove("negative")

    # —— 纯文本正文兜底：自定义节点把成稿写在普通字符串字段上 ——
    # 实测（AnimaArtistMixer_01146_）：ANIMA 艺术家混合工作流的正向正文只存在于
    # `AnimaArtistPack.base_prompt`，它下游的 CLIPTextEncode 接的是**负向**。
    # 只认 CLIPTextEncode 的连线回溯 ⇒ 正向整段消失，界面显示空提示词（静默给错）。
    # 与上面的 `*_prompt` JSON 快照是同一种「正文不在连线里」的情况，只是载体不同：
    #   · 快照是 JSON（positive_full/positive），优先级最高，已在上方处理；
    #   · 这里只认**结构化字段名**（白名单），不去猜任意字符串；
    #   · 只在正侧仍为空时补位，绝不像素级覆盖已收到更准的正文。
    if not (s.get("positive") or "").strip():
        _bp = _plain_prompt_value(obj, _pos_reach)
        if _bp:
            _bp, _bp_tags = _split_lora_tags(_bp)   # 正文里若带 <lora:...> 一并剥离
            if _bp_tags:
                tag_loras.extend(_bp_tags)
            if _bp.strip():
                s["positive"] = _bp.strip()
                if "positive" in _empty_sides:
                    _empty_sides.remove("positive")

    # —— MOSS-Image 布局工作流：正文是一份 JSON，不是字符串 ——
    # 复用上面那份「正向侧可达节点」清单：只有接在条件链路上的导入节点才算数
    # （工作流里常留着没接线的旧布局节点，不设闸会把废图的描述贴上来）。
    # ⚠️ 2026-11-07 第三例（B0026F7F….png）：不能再「只在正向为空时才取」——
    #    那条链是 Prompt(LoraManager).text ← MOSS_JSON_Importer.layout_json，
    #    同时 TriggerWord Toggle 的 orinalMessage 也挂在同一个节点上；
    #    连线回溯先收着触发词就 return 了，正向变成「miaomiao,, miaomiao,, miaomiao」，
    #    6097 字的画面描述一个字都没读到。触发词是真的、正文也是真的，两者该合起来：
    #    触发词按 LoraManager 自己的顺序排在最前（与上面 *_prompt 快照的 _keep 同一套做法）。
    _ml = _moss_layout_prompt(obj, _pos_reach)
    if _ml:
        _cur = (s.get("positive") or "").strip()
        if not _cur:
            s["positive"] = _ml
            if "positive" in _empty_sides:
                _empty_sides.remove("positive")
        elif _ml not in _cur:
            _keep = [t for t in (_walked.get("positive") or [])
                     if t.strip() and t.strip() not in _ml]
            s["positive"] = "\n".join(_keep + [_ml]) if _keep else _ml
    # —— 正文由「打标 / 反推」节点在生成时实时产出：PNG 里根本没有它 ——
    # 2026-11-07 实测（12CADAF8….png）：CLIPTextEncode.text 直连 WD14Tagger 的 tags，
    # 该节点的 tags 不参与本次执行，只在跑完后写回 widget；文件里存的是**上一次运行**
    # 的标签（内容是 swimsuit / beach / volleyball，画面却是白裙血剑，完全对不上）。
    # 旧逻辑把这类节点整体剪枝 ⇒ 正向整段消失，面板上连「正向提示词」都不出现，
    # 用户看到的就是「读取有问题」。这里**只解释、不补内容**：
    #   positive 保持空，另给 positive_src / positive_node / positive_cache 供界面说明。
    # ⚠️ 绝不可把 cache 写进 positive —— 那等于把上一张图的标签冒充成本图提示词。
    # 2026-11-08 补：只在「正向为空」时才查，会漏掉**部分可读**的情形
    # （E639D3B6….png：文本1 是字面量、文本3 是 VLM 反推，正向只读到前缀）。
    # 正向非空时先做一次便宜的预筛 —— 正向可达集合里有没有反推/打标节点，
    # 命中才真走一遍，且 strict=True（不许按类名兜底猜产出者）：
    # 普通工作流没有这类节点，一次多余遍历都不会有。
    _pos_txt = (s.get("positive") or "").strip()
    _has_gen = any(isinstance(obj.get(_x), dict) and _is_gen_node(obj[_x])
                   for _x in _pos_reach)
    if not _pos_txt or _has_gen:
        _rt = _runtime_prompt_node(obj, _pos_reach, cond_entry("positive"),
                                   strict=bool(_pos_txt))
        if _rt:
            s["positive_src"] = "runtime"
            s["positive_node"] = _rt["node"]
            if _rt.get("title"):
                s["positive_title"] = _rt["title"]
            if _rt.get("cache"):
                s["positive_cache"] = _rt["cache"]
            if _rt.get("file"):
                s["positive_file"] = _rt["file"]
            if _pos_txt:
                # 界面据此把「正向提示词」的头衔从断言改成「这只是文件里那一段」
                s["positive_partial"] = True

    model_ref = next((ref(k) for k in ("model", "modelname", "ckpt_name", "unet_name")
                      if ref(k)), None)
    if model_ref is None:
        # SamplerCustom*：模型挂在 guider 上（BasicGuider.model / CFGGuider.model）
        _m = from_ref(inp.get("guider"), "model")
        if isinstance(_m, list) and _m and isinstance(_m[0], str):
            model_ref = _m[0]
    if model_ref:
        loras, ckpt = [], []
        def visit_m(n):
            ct = _ct_key(n)
            i2 = _inp(n)
            if "lora" in ct and "trigger" not in ct:
                got = False
                lv = i2.get("loras")
                if isinstance(lv, dict):                      # LoraManager: {"__value__": [...]}
                    lv = lv.get("__value__")
                if isinstance(lv, list):
                    for e in lv:
                        if isinstance(e, dict) and isinstance(e.get("name"), str):
                            if not _entry_active(e):          # 跳过未启用的槽位
                                continue
                            st = _strength_fmt(e.get("strength", e.get("strengthModel")))
                            loras.append(e["name"] + (f" ({st:g})" if st is not None else ""))
                            got = True
                if not got:
                    for k, v in i2.items():                   # rgthree Power Lora Loader: lora_1..lora_N
                        if (re.fullmatch(r"lora(_\d+)?", k) and isinstance(v, dict)
                                and isinstance(v.get("lora"), str)):
                            if not _entry_active(v):
                                continue
                            st = _strength_fmt(v.get("strength"))
                            loras.append(v["lora"] + (f" ({st:g})" if st is not None else ""))
                            got = True
                if not got:
                    for k, v in i2.items():                   # 常规格式: lora_name / lora_name_2
                        if re.fullmatch(r"lora_name(_\d+)?", k) and isinstance(v, str):
                            st = i2.get("strength_model" if k == "lora_name" else
                                        "strength_model" + (k[len("lora_name"):]))
                            st = _strength_fmt(st)
                            loras.append(v + (f" ({st:g})" if st is not None else ""))
                if not got:
                    for k, v in i2.items():                   # 槽位表被 JSON.stringify 成字符串
                        if "lora" not in k.lower():           # （VisualLoRALoader 的 lora_data）
                            continue
                        # WeiLinPromptUI 的 `temp_lora_str` 是前端「待加入」的暂存区，
                        # 里面是**下一堆要加的槽位 + 已生效那一堆的副本**；真正被执行的是
                        # `lora_str`（见 load_lora_ing：只解析 lora_str）。两个都收会出重复，
                        # 2026-11-08 实测 Anima_00126_：15 条里有 10 条是重复项。
                        if _flat_key(k).startswith(("temp", "tmp", "draft", "pending")):
                            continue
                        ents = _lora_entries_from_json(v)
                        if not ents:
                            continue
                        for e in ents:
                            if not _entry_active(e):          # 跳过未启用的槽位
                                continue
                            st = _entry_strength(e)
                            loras.append(e["name"] + (f" ({st:g})" if st is not None else ""))
                            got = True
            elif "checkpointloader" in ct and isinstance(i2.get("ckpt_name"), str):
                ckpt.append(i2["ckpt_name"])
            elif "unetloader" in ct and isinstance(i2.get("unet_name"), str):
                ckpt.append(i2["unet_name"])
        _walk_nodes(model_ref, obj, visit_m)
        if ckpt:
            s["model"] = ckpt[0]
        if loras:
            s["loras"] = loras

    # ---- 多段采样流：把每一段真正用到的模型 / 采样参数记下来（2026-11-03）----
    # 背景：两段式（先底模出图、再换模型 turbo 精修/放大）在 Krea2 系工作流里很常见。
    # 上面只从**主采样器**（ks）那一条 model 链收 ckpt ⇒ 界面只显示第一段，而 LoRA 可能
    # 来自第二段（实测 Krea2-2ST：第一段 moss、第二段 krea2_turbo + 2 条 LoRA），显示上割裂。
    #
    # 口径（与前端约定）：
    #   · `s["model"]` **语义不变**，仍是第一段 —— 筛选下拉 / 出图统计 / 搜索索引 / 导出
    #     全都依赖它，改了会让全库统计口径漂移。
    #   · 新增 `s["models"] = [{name, stage, sampler?, scheduler?, steps?, cfg?, seed?, w?, h?}]`：
    #     stage 从 1 起，按「主采样器在前、其余按节点出现顺序」排；**同一模型只留首次出现**。
    #   · **只有 ≥2 条不同模型时才写这个字段**：单模型图（全库绝大多数）一个字节都不多，
    #     前端「维持现状」那条分支也就不需要额外判空。
    #   · 每段参数是**该段采样器节点自己的**，取不到就不写那个键（前端显示 —）。
    #     顶层 `sampler/steps/cfg/seed` 仍是主采样器那一份，语义不变。
    #   · `w/h` 是该段采样时的 latent 尺寸（int，由下面潜变量块补写）——两段 hires 流里
    #     第一段 1216×832、第二段 1824×1248，**不能用一个 size 代表全图**。
    def _sampler_model(n):
        """该采样器那一段最终作用的模型名（沿 model 链取**最近**的 loader）。"""
        i2 = _inp(n)
        rid = None
        for k in ("model", "modelname", "ckpt_name", "unet_name"):
            v = i2.get(k)
            if isinstance(v, list) and v and isinstance(v[0], str):
                rid = v[0]
                break
        if rid is None:                     # SamplerCustom*：模型挂在 guider 上
            g = i2.get("guider")
            if isinstance(g, list) and g and isinstance(g[0], str):
                gn = obj.get(g[0])
                if isinstance(gn, dict):
                    gv = _inp(gn).get("model")
                    if isinstance(gv, list) and gv and isinstance(gv[0], str):
                        rid = gv[0]
        if not rid:
            return None
        got = []

        def _v(n):
            ct = _ct_key(n)
            i3 = _inp(n)
            if "checkpointloader" in ct and isinstance(i3.get("ckpt_name"), str):
                got.append(i3["ckpt_name"].strip())
            elif "unetloader" in ct and isinstance(i3.get("unet_name"), str):
                got.append(i3["unet_name"].strip())

        _walk_nodes(rid, obj, _v)
        # got[0] = 离采样器最近的那个 loader（DFS 先走 model 分支）—— 那才是这一段
        # 实际吃进去的权重；再往上游的 base 模型只算「用来拼的原料」。
        return next((x for x in got if x), None)

    def _is_sampler_node(n):
        """是否是「真在采样」的节点。

        只靠类名含 sampler 不够，三类必须排除：
          · `*Select*` / `*Selector*` —— 只是采样器选择器，本身不采样；
          · `*Warmup*` / `*Cache*` / `*Preview*` —— 预热/缓存/预览节点，会引用模型但不采样，
            放进来会给同一张图凭空多出一段模型；
          · 没有模型证据的（既无 model 连线也无 guider）。
        `SamplerCustomAdvanced` 把步数挂在 sigmas/noise 上、自身没有 steps/cfg，
        所以有 guider 就直接算；只有 model 的必须带 steps/cfg/denoise 之类的采样证据。
        """
        ct = _ct_key(n)
        if any(w in ct for w in ("select", "selector", "warmup", "cache", "preview")):
            return False
        if "sampler" not in ct:
            return False
        i2 = _inp(n)
        if isinstance(i2.get("guider"), list) and i2.get("guider"):
            return True
        if not (isinstance(i2.get("model"), list) and i2.get("model")):
            return False
        return any(k in i2 for k in ("steps", "cfg", "denoise", "noise_seed", "add_noise"))

    def _stage_params(n):
        """某一段采样器节点上的 sampler/scheduler/steps/cfg/seed（取不到就不写该键）。

        ⚠️ 与顶层那份取值逻辑**同源但不复用**：顶层写的是 `s[...]`（整图口径），这里要
           按段各取一份。字段拼写差异这里必须再认一遍 —— KSamplerAdvanced 的种子是
           `noise_seed`、SamplerCustom 的种子在 noise 节点的 `noise_seed` 上、
           自定义采样器的名字键是 `sampler`（不是 `sampler_name`）。
        """
        if not isinstance(n, dict):
            return {}
        i2 = _inp(n)
        o = {}
        _nk = "sampler_name" if i2.get("sampler_name") is not None else "sampler"
        _v = i2.get(_nk)
        if _v is not None:
            if not isinstance(_v, str):
                _v = resolve_str(_v, _nk)      # 名字可能是连线引用（选择器 / To String）
            if isinstance(_v, str) and _v.strip():
                o["sampler"] = _v.strip()
        if not o.get("sampler"):
            _sn = from_ref(i2.get("sampler"), "sampler_name")
            if isinstance(_sn, str) and _sn.strip():
                o["sampler"] = _sn.strip()
        _sc = i2.get("scheduler")
        if _sc is not None:
            if not isinstance(_sc, str):
                _sc = resolve_str(_sc, "scheduler")
            if isinstance(_sc, str) and _sc.strip():
                o["scheduler"] = _sc.strip()
        for _k, _keys in (("steps", ("steps", "steps_total", "value")),
                          ("cfg", ("cfg", "value"))):
            _x = i2.get(_k)
            if _x is None:
                continue
            _x = resolve_num(_x, _keys)
            if isinstance(_x, (int, float)) and not isinstance(_x, bool):
                o[_k] = _x
        _sd = next((i2[k] for k in ("seed", "noise_seed", "seed_value")
                    if i2.get(k) is not None), None)
        if _sd is None:                    # SamplerCustom*：种子在 noise 节点上
            _sd = from_ref(i2.get("noise"), "noise_seed")
        if _sd is not None:
            _sd = resolve_num(_sd, ("seed", "noise_seed", "seed_value", "value"))
        if isinstance(_sd, (int, float)) and not isinstance(_sd, bool):
            o["seed"] = int(_sd)
        return o

    if s.get("model"):
        _seq, _seen = [], set()

        def _add_model(nm, node=None):
            if not isinstance(nm, str) or not nm.strip():
                return
            nm = nm.strip()
            # 去重键 = **文件名**（保留扩展名、大小写不敏感，丢掉目录前缀）。
            # 同一个权重在不同节点上常以「带子目录」和「裸名」两种写法出现
            # （`krea2\turbo.safetensors` vs `turbo.safetensors`），按全路径比会把
            # 同一段算成两段；按文件名比才认得是一个。不同目录下的同名底模极罕见，
            # 真要撞了也只是少显示一行，不会污染 `model` 字段（它是第一段，写死来源）。
            k = re.split(r"[\\/]", nm)[-1].lower()
            if not k:
                return
            if k in _seen:
                return
            _seen.add(k)
            _seq.append((nm, node))

        # 第一段 = `s["model"]`（它就是主采样器那条链的 ckpt[0]，上面刚算过）。
        # ⚠️ 不要再单独加一次 `_sampler_model(ks)`：那是同一个值，去重后看不出差别，
        #    但负控一摘去重就会暴露成「同一模型占了两段」（N2-a 实测）。
        _add_model(s["model"], ks)
        for _n in obj.values():
            if not isinstance(_n, dict) or _n is ks:
                continue
            if _is_sampler_node(_n):
                _add_model(_sampler_model(_n), _n)
        if len(_seq) >= 2:
            _out = []
            for _i, (_nm, _nd) in enumerate(_seq):
                _e = {"name": _nm, "stage": _i + 1}
                _p = _stage_params(_nd)
                if _nd is ks:
                    # 第一段补顶层那份：两条路取自同一个节点，这里只是防「_stage_params
                    # 的某条分支没覆盖到」时第一段反而比顶层更空（显示上会很怪）。
                    for _k in ("sampler", "scheduler", "steps", "cfg", "seed"):
                        if _k not in _p and s.get(_k) is not None:
                            _p[_k] = s[_k]
                _e.update(_p)
                _out.append(_e)
            s["models"] = _out

    # WeiLinPromptUI：LoRA 既不在 <wlr:…> 标签里，也不在模型链的加载器节点上，而是整张
    # 槽位表 json.dumps 进 `lora_str`（独立节点，opt_model 只是引用 UNETLoader）。上面
    # 的 visit_m 沿 model 连线走，碰不到它 ⇒ 必须单独全图扫一遍。放在 model_ref 块外，
    # 因为「模型读不到」不该连带把 LoRA 一起丢掉。
    # 去重按 `_lora_dup_key`（裸名 vs 带扩展名）—— 这张图**同时**有 `<wlr:…>` 标签，
    # 标签给裸名、槽位给带扩展名的全名，纯字符串比会各留一条（实测 5 → 9 条）。
    _names = list(s.get("loras") or [])
    for _e in (list(_weilin_lora_slots(obj) or ()) + list(_dotted_lora_slots(obj) or ())):
        _nm = _e.get("lora") if isinstance(_e.get("lora"), str) and _e.get("lora") \
            else _e.get("name")
        if not isinstance(_nm, str) or not _nm.strip():
            continue
        _st = _entry_strength(_e)
        _lora_add(_names, _nm.strip() + (f" ({_st:g})" if _st is not None else ""))

    if tag_loras:
        # B2-P2-5：提示词里的 <lora:...> 已被 _split_lora_tags 从正文剥掉，所以这里的
        # 信息**必须**落地，否则两边都不出现（正文没了、LoRA 列表也没有）——界面上的
        # 提示词已被改写且不可恢复。旧逻辑用 `not s.get("loras")` 二选一：一旦加载器
        # 也给了 LoRA（LoraManager 与常规 LoraLoader 混用、或用户手写补标签），标签这份
        # 就被丢弃。改为**合并去重**：加载器顺序在前（那是有序的槽位），标签补在后。
        for _tag in tag_loras:
            _v = _lora_tag_entry(_tag)
            if _v:
                _lora_add(_names, _v)
        if _names:
            s["loras"] = _names
    elif _names != list(s.get("loras") or []):
        s["loras"] = _names

    _lat_ref = ref("latent_image") or ref("latent")   # 后者：自定义采样器的字段名
    if _lat_ref:
        sizes, _sizes_nid = [], []
        def visit_l(n):
            ctl = _ct_key(n)
            i2 = _inp(n)
            # 「分辨率」字符串形态（ZImageLatent 的 resolution）只在 latent 类节点上认，
            # 避免把某个 UI 节点的说明文本当尺寸。
            _res = _size_from_resolution(i2.get("resolution")) if "latent" in ctl else None
            if ("empty" in ctl and "latent" in ctl) or _res:   # EmptyLatent / ZImageLatent 等
                # 宽高也可能不是字面量（XB_ImageParamsMaster 这类节点把 800×1424
                # 算成一组参数再连线给 EmptyLatentImage）⇒ 沿连线取值，别只认 int。
                _w = i2.get("width")
                _h = i2.get("height")
                if not (isinstance(_w, int) and isinstance(_h, int)):
                    _w = resolve_num(_w, ("width", "value"))
                    _h = resolve_num(_h, ("height", "value"))
                if not (isinstance(_w, int) and isinstance(_h, int)) and _res:
                    _w, _h = _res          # 没有 width/height ⇒ 用分辨率字符串
                if isinstance(_w, int) and isinstance(_h, int):
                    sizes.append((_w, _h))
                    _sizes_nid.append(_node_id_of(obj, n))
        _walk_nodes(_lat_ref, obj, visit_l)
        if sizes:
            _w0, _h0 = sizes[0]
            s["size"] = f"{_w0} x {_h0}"
            # 下游放大链：成品尺寸另立一键（见 _latent_scale_size 的注释）
            _out = _latent_scale_size(
                obj, _sizes_nid[0], _w0, _h0,
                lambda v: resolve_num(v, ("scale_by", "value", "float", "int")))
            if _out and _out != (_w0, _h0):
                s["size_out"] = f"{_out[0]} x {_out[1]}"
    if not s.get("size"):                        # 兜底：节点直接给了宽高（可能是连线引用）
        _w = resolve_num(inp.get("width"), ("width", "value"))
        _h = resolve_num(inp.get("height"), ("height", "value"))
        if isinstance(_w, int) and isinstance(_h, int):
            s["size"] = f"{_w} x {_h}"
    # 多段流的**每段尺寸**：必须等上面的潜变量源头（`_sizes_nid[0]`）算完才能补 ——
    # 从源头沿下游走，路过每个采样器时记下当时的尺寸。取不到就整条不写（前端显示 —），
    # 绝不拿顶层 `size` 兜底：那正是「两段明明不同却被显示成一样」的来源。
    if s.get("models") and _lat_ref and _sizes_nid and sizes:
        try:
            _st_sz = _latent_stage_sizes(
                obj, _sizes_nid[0], sizes[0][0], sizes[0][1], _is_sampler_node,
                lambda v: resolve_num(v, ("scale_by", "value", "float", "int")))
        except Exception:
            _st_sz = {}                          # 尺寸是附加信息，读不到不能拖垮整条解析
        for _e, (_nm, _nd) in zip(s["models"], _seq):
            _wh = _st_sz.get(_node_id_of(obj, _nd)) if _nd is not None else None
            if _wh:
                _e["w"], _e["h"] = int(_wh[0]), int(_wh[1])
    _k = _wf_kind(obj, ks)                       # 工作流类型（文生图/图生图/ControlNet）
    if _k:
        s["wfk"] = _k
    # 私有键（下划线开头）：解析过程中「有东西没读到」的回执，由消费方决定怎么呈现。
    # 前端参数面板只按已知键取值，不会把它们显示成参数。
    if _deep_hits:
        _lbl = {"sampler_name": "采样器", "sampler": "采样器", "scheduler": "调度器",
                "steps": "步数",
                "cfg": "CFG", "seed": "Seed", "noise_seed": "Seed", "value": "参数",
                "width": "宽度", "height": "高度"}
        _miss = []
        for _h in _deep_hits:
            _n = _lbl.get(_h, _h)
            if _n and _n not in _miss:
                _miss.append(_n)
        if _miss:
            s["_warn"] = ["部分参数经多层连线传递，未能读取：" + "、".join(_miss)]
    if _empty_sides:
        s["_empty"] = _empty_sides
    return s


_A1111_KV_SPLIT_RE = re.compile(r",\s*(?=[A-Z][A-Za-z0-9 _]*:)")


def _split_a1111_kv(tail: str) -> list:
    """按 `, Key:` 切分 A1111 参数块，但**跳过成对引号内的逗号**。

    `Lora hashes: "Foo: 0c3b, Bar: 1a2f"` 里的 `, Bar:` 完全符合「大写开头 + 冒号」，
    裸正则把它当成新键 ⇒ 只拿到第一个 LoRA，还多出一个垃圾键 `Bar`。
    这里先把引号内的逗号换成哨兵、切完再还原；引号不闭合（脏数据）时退回裸切分，
    与原行为一致 —— 不会因为一个没写完的引号把整块参数吞成一段。
    """
    buf, quote = [], ""
    for ch in tail:
        if quote:
            buf.append("\x00" if ch == "," else ch)
            if ch == quote:
                quote = ""
        else:
            if ch in "\"'":
                quote = ch
            buf.append(ch)
    if quote:                       # 引号未闭合 ⇒ 保护不可信
        return _A1111_KV_SPLIT_RE.split(tail)
    return [p.replace("\x00", ",") for p in _A1111_KV_SPLIT_RE.split("".join(buf))]


def parse_a1111_parameters(text: str) -> dict:
    """A1111/Forge 的 parameters 纯文本 → 人类可读摘要。"""
    s = {}

    def _fill_loras():
        """把正文里的 <lora:...> 标签拆成 loras，并用 Lora hashes 补漏。

        A1111/Forge 把 LoRA 写在**提示词正文**里（`<lora:名字:强度>`），参数块里只剩
        名字+哈希（`Lora hashes: "a: 0c3b, b: 1a2f"`）。旧实现两处都没读 ⇒ 界面上
        LoRA 恒为空（实测 13-36-13-…_通用放大_00001_：标签就在正文里、哈希也在，
        结果 parsed.loras = []）。口径与 ComfyUI 侧完全一致：正文剥掉标签，
        列表元素 "名字 (强度)"，前端 metaLoras 才能按尾括号拆权重。
        ⚠️ `Lora hashes` 键**只读不删** —— 既有门禁 B3a 断言它的值完整。
        """
        tags = []
        for side in ("positive", "negative"):
            t = s.get(side)
            if not isinstance(t, str) or not t:
                continue
            clean, found = _split_lora_tags(t)
            if found:
                s[side] = clean
                tags.extend(found)
        names = []
        for tag in tags:
            v = _lora_tag_entry(tag)
            if v and v not in names:
                names.append(v)
        lh = s.get("Lora hashes")
        if isinstance(lh, str):
            # 值在 A1111 里是**带引号**的：Lora hashes: "a: 0c3b, b: 1a2f"
            # 不去引号的话首个名字会是 '"a'，与正文解析出的 "a" 对不上 ⇒ 列表里重复一条。
            lh = lh.strip().strip('"').strip("'")
            for part in lh.split(","):
                nm = part.split(":")[0].strip()
                if not nm:
                    continue
                # 名字可能自带括号（"foo (v2).safetensors"）⇒ 只剥**末尾的权重括号**
                if any(re.sub(r"\s*\([-+]?\d+(?:\.\d+)?\)\s*$", "", x) == nm
                       for x in names):
                    continue
                names.append(nm)      # 只有哈希、没有强度：不虚构权重，原样列名字
        if names:
            s["loras"] = names

    # B2-P2-2：取**最后一个**行首 `Steps: 数字`，不是第一个。
    #   真正的参数块永远在文末；而提示词正文里可能恰好有一行以 `Steps: 20` 开头
    #   （用户把上次的参数块贴进提示词、或正文写了「Steps: 20 是我常用的设置」）。
    #   旧写法 re.search 取首个匹配 ⇒ 断点落进正文，后面的提示词 / Negative prompt
    #   全被当成 steps 的值吞掉，界面上是一串垃圾步数 + 空提示词，且无任何异常。
    _hits = list(re.finditer(r"^Steps:\s*\d+", text, re.M))
    m = _hits[-1] if _hits else None
    if not m:
        if text.strip():
            s["positive"] = text.strip()
        _fill_loras()
        return s
    head, tail = text[:m.start()], text[m.start():]
    # 7-5：新键必须「大写字母开头」——A1111 全部已知参数键（Steps/CFG scale/Model hash/
    #      Schedule type/Lora hashes…）都符合；而值内部的「xx: yy」片段（Lora hashes 值里的
    #      "a: 0c3b, b: 1a2f"、Hires prompt 里的英文子句）几乎全是小写开头。
    #      旧正则 [A-Za-z] 大小写通吃 ⇒ 断点落进值内部，前一个键只剩半截、还多出垃圾键。
    for part in _split_a1111_kv(tail):
        k, _, v = part.partition(":")
        k, v = k.strip(), v.strip()
        if k and v:
            key = {"Steps": "steps", "CFG scale": "cfg", "Sampler": "sampler",
                   "Seed": "seed", "Size": "size", "Model": "model",
                   "Model hash": "model_hash", "Schedule type": "scheduler"}.get(k, k)
            s[key] = v
    nm = re.match(r"(?s)(.*?)\nNegative prompt:\s?(.*)", head)
    if nm:
        if nm.group(1).strip():
            s["positive"] = nm.group(1).strip()
        if nm.group(2).strip():
            s["negative"] = nm.group(2).strip()
    elif head.strip():
        s["positive"] = head.strip()
    _fill_loras()
    return s


# ---- A1111 → ComfyUI 名称/类型适配（模板合成用）----

_A1111_SAMPLERS = {
    "euler": "euler", "euler a": "euler_ancestral", "heun": "heun",
    "dpm2": "dpm2", "dpm2 a": "dpm2_ancestral", "lms": "lms",
    "dpm fast": "dpm_fast", "dpm adaptive": "dpm_adaptive",
    "dpm++ 2m": "dpmpp_2m", "dpm++ 2m sde": "dpmpp_2m_sde",
    "dpm++ sde": "dpmpp_sde", "dpm++ 3m sde": "dpmpp_3m_sde",
    "ddim": "ddim", "plms": "plms", "uni_pc": "uni_pc",
    "lcm": "lcm", "restart": "restart",
}
_A1111_SCHEDULERS = {
    "automatic": "normal", "uniform": "normal", "normal": "normal",
    "karras": "karras", "exponential": "exponential",
    "polyexponential": "polyexponential", "sgm uniform": "sgm_uniform",
    "ddim uniform": "ddim_uniform", "beta": "beta",
}


def _a1111_sampler(name):
    """A1111 采样器名 → (ComfyUI sampler 名, 名称里隐含的 scheduler 或 None)。

    ComfyUI 原生名字（如 euler_ancestral）会原样透传。
    """
    n = (name or "").strip().lower()
    sch = None
    for suf, s in ((" karras", "karras"), (" exponential", "exponential"),
                   (" sgm uniform", "sgm_uniform"), (" uniform", "normal")):
        if n.endswith(suf):
            n, sch = n[:-len(suf)].strip(), s
            break
    return _A1111_SAMPLERS.get(n, n.replace("++", "pp").replace(" ", "_")), sch


def _num(v, as_int=False):
    """宽松数值转换（A1111 参数是字符串），失败返回 None。

    P0-14：整数目标**先走 int()**，不走 float() 中转 —— float() 是 53 位有效位，
    对 > 2^53 的整数会静默改写（实测 2^64-1 → …616）。seed 一旦被改写，合成出来的
    工作流就**永远无法复现**，而界面上看不出任何异常。ComfyUI 的 seed 是 JSON 整数、
    A1111 的是十进制字符串，两条来源都会经过本函数，所以两条都会中招。

    B2-P1-1：`float()` 认得 "nan" / "inf" / "-inf"，而 NaN 不是合法 JSON 字面量 ——
    它一旦进 widgets_values 或随 `parsed` 回传，`json.dumps` 会写出裸 `NaN`，
    前端 `JSON.parse` 与 ComfyUI 侧严格解析器都会直接失败。所以非有限值一律当
    「转换失败」处理（与 "_num 失败 ⇒ 跳过该参数" 同一口径，且有回执，见 _apply_parsed）。
    """
    if as_int:
        try:
            return int(v)
        except (TypeError, ValueError, OverflowError):
            pass          # 形如 "12.7" / 12.7：退回「截断取整」的老口径
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(f):
        return None
    return int(f) if as_int else f


def _finite_clean(v):
    """把 NaN / ±Inf 换成 None（递归）。

    B2-P1-1 的第二道闸：`_num` 只管模板合成那一路，而 `parsed` 本身是从
    上游工作流 parse 出来的，可能原样带着 nan（`json.loads` 默认接受 NaN 字面量），
    随后被塞进 HTTP 响应体 ⇒ 整个响应变成非法 JSON。输出前统一洗一次。
    """
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {k: _finite_clean(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_finite_clean(x) for x in v]
    return v


# ---- 显示类节点（TE_text_display 等）的文本修正（2026-11-15）----------------
# 实测 ComfyUI_01394_.png（用户桌面）：CLIPTextEncode.text ← TE_text_display，
# 而该节点在 API prompt 里的 `inputs.text` 是**上一次**的显示串：
#   ① 内容对不上画面（"2girls + 森林 + 性行为" vs 白蕾丝单人室内照）；
#   ② 结构是显示模板 `POSITIVE: …\nNEGATIVE: …\nSETTINGS: Quality=…`，
#      于是正向里混进了前缀、一整段负面、以及一行调试参数。
#
# 本次真实文本在 workflow 的节点 properties 里（onExecuted 时写入）：
#   te_text_display_preview_text / te_text_display_source_text
# 与 11-07 那条「打标节点 tags 是上一张图的」同族；区别是**这里有本次值的副本**，
# 所以不剪枝（不清空 positive），而是换成副本。
#
# 只对 class_type 命中白名单、且正向确实等于这段显示串的节点动手 ——
# 普通 CLIPTextEncode 的字面量一个字都不碰。
_DISPLAY_CLASS_HINTS = ("te_text_display",)
_DISPLAY_PROP_KEYS = ("te_text_display_preview_text",
                      "te_text_display_source_text")
_DISPLAY_LABEL_RE = re.compile(r"(?m)^(POSITIVE|NEGATIVE|SETTINGS)\s*[:：]\s*")


def _split_display_text(txt):
    """把显示模板切成 {POSITIVE/NEGATIVE/SETTINGS: 正文}；不是模板返回 None。

    只有**首行就是标签**时才认模板 —— 用户正文里写 "NEGATIVE:" 这种词完全可能，
    不能让一段正常提示词被误切。
    """
    if not isinstance(txt, str):
        return None
    txt = txt.lstrip()
    ms = list(_DISPLAY_LABEL_RE.finditer(txt))
    if not ms or ms[0].start() != 0:
        return None
    out = {}
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(txt)
        out[m.group(1)] = txt[m.end():end].strip()
    return out


def _fix_display_text(s: dict, obj: dict, wf_raw) -> None:
    """就地把显示节点的陈旧模板串换成真实正文（见上方长注释）。"""
    if not isinstance(s, dict) or not isinstance(obj, dict):
        return
    wf = wf_raw
    if isinstance(wf, str) and wf.strip():
        try:
            wf = json.loads(wf)
        except ValueError:
            # 畸形 workflow 只是「拿不到本次副本」，不该连**兜底切段**一起放弃 ——
            # 切段只依赖条目里的 stale 文本本身，仍然能救回正向正文。
            wf = None
    byid = {}
    if isinstance(wf, dict):
        for n in (wf.get("nodes") or []):
            if isinstance(n, dict) and n.get("id") is not None:
                byid[str(n["id"])] = n
    cur_pos = s.get("positive") or ""
    cur_neg = s.get("negative") or ""
    for nid, node in obj.items():
        if not isinstance(node, dict):
            continue
        ct = str(node.get("class_type") or "").lower()
        if not any(k in ct for k in _DISPLAY_CLASS_HINTS):
            continue
        inp = node.get("inputs") or {}
        if inp.get("use_edited_text") is True:      # 用户手改过：以他的话为准
            continue
        stale = inp.get("text")
        if not isinstance(stale, str) or not stale.strip():
            continue
        stem = stale.strip()
        if not (cur_pos.strip() == stem or cur_pos.strip().startswith(stem)):
            continue                                # 正向不是这段显示串 —— 不动
        # 本次真实文本的可靠副本（执行后由 onExecuted 写进 workflow properties）
        real = ""
        props = (byid.get(str(nid)) or {}).get("properties") or {}
        for pk in _DISPLAY_PROP_KEYS:
            v = props.get(pk)
            if isinstance(v, str) and v.strip():
                real = v.strip()
                break
        parts = _split_display_text(real) if real else _split_display_text(stem)
        if real and not parts:
            parts = {"POSITIVE": real}
        if not parts or not parts.get("POSITIVE"):
            continue
        s["positive"] = parts["POSITIVE"]
        if not cur_neg.strip() and parts.get("NEGATIVE"):
            s["negative"] = parts["NEGATIVE"]


def parse_meta(meta: dict, diag: dict = None):
    """从 png_meta 结果提取人类可读摘要；失败返回 None。

    diag（可选，出参）：解析链**抛异常**时写入 {"parse_error": "类型: 原文"}。
    B2-P1-4：旧代码是裸 `except Exception: pass` —— 异常、堆栈、日志全无，
    「解析器崩了」与「这张图本来就没有生成参数」在界面上完全一样（都是空白），
    排障只能靠复现。现在至少留一条 warn 日志，并给调用方一个可上报的出口。
    """
    try:
        if isinstance(meta.get("prompt"), str):
            obj = json.loads(meta["prompt"])
            if isinstance(obj, dict) and obj:
                s = parse_comfy_prompt(obj)
                if s:
                    # 2026-11-15：显示类节点（TE_text_display）的 text 是上一次的
                    # 显示串（带 POSITIVE/NEGATIVE/SETTINGS 模板），本次真值在
                    # workflow 的 properties 里。修正放在这里 —— 图片/视频/详情页
                    # 全部经由 parse_meta，一处收口。
                    _fix_display_text(s, obj, meta.get("workflow"))
                    s["source"] = "ComfyUI"
                    return s
    except Exception as ex:    # 畸形 PNG 的 prompt 可能是任意结构：解析不了就当没有生成参数
        log("[warn] 元数据解析失败: %s: %s" % (type(ex).__name__, ex))
        if diag is not None:
            diag["parse_error"] = "%s: %s" % (type(ex).__name__, ex)
    if isinstance(meta.get("parameters"), str):
        s = parse_a1111_parameters(meta["parameters"])
        if s:
            s["source"] = "A1111/Forge"
            return s
    return None


def parse_jpeg_meta(path: Path) -> dict:
    """从 PIL 可读的位图（JPEG/WebP/GIF/BMP）提取尺寸与生成参数。

    A1111/Forge 的参数存于 EXIF UserComment(0x9286)。
    """
    if not HAS_PIL:
        return {}
    try:
        with Image.open(path) as im:
            meta = {"IHDR": f"{im.width} x {im.height}"}
            try:
                exif = im.getexif()
            except Exception:
                return meta
            v = exif.get(0x9286)
            if isinstance(v, bytes):
                text = v[8:].decode("utf-8", "replace")  # 跳过 8 字节编码头
            elif isinstance(v, str):
                text = v
            else:
                return meta
            text = text.strip()
            if "Steps:" in text or "Negative prompt:" in text:
                meta["parameters"] = text
            return meta
    except Exception:
        return {}


# ---- 视频生成参数读取（V2.13.2）------------------------------------------
# 目标：像读 PNG 元数据那样，把视频里 ComfyUI 写的提示词读出来。
#
# 位置（由 strip_mp4_meta 的注释与实测共同确认）：
#   mp4/mov/m4v → moov/udta/meta/ilst
#   webm/mkv    → EBML 的 Segment/Tags/Tag/SimpleTag
#
# 设计要点：**只负责把原始文本取出来**，解析成人类可读参数交给已有的 parse_meta()。
# 也就是说视频产出一份和 png_meta 同构的 {"prompt": "<json>"}，解析层一行都不用改 ——
# 共用一条路，才不会长出第二套口径（两套口径迟早会出现「图片能看视频不能看」这种不一致）。

# 头 / 尾各读这么多字节去找元数据。**刻意不整体读文件**：实测用户的 AI 视频有 472 MB 的，
# 为几个参数把几百 MB 读进内存既慢又危险；而元数据只可能在两头 ——
# mp4 的 moov 在不 faststart 时位于**尾部**（ffmpeg 默认），开了 faststart 才在头部；
# webm 的 Tags 在 Segment 前部（muxer 先写头再写簇）。两头都探，不用猜。
VIDEO_PROBE_BYTES = 16 << 20

# EBML 的 Tags 元素 ID（含 marker bit，固定 4 字节）。
# 尾部探测靠搜它来定位 —— EBML 是顺序容器、没有全局索引，没法像 mp4 那样按偏移找。
_TAGS_ID = b"\x12\x54\xc3\x67"
_VIDEO_META_KIND = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mp4",
                    ".webm": "webm", ".mkv": "webm"}


def _mp4_meta_base(data: bytes, start: int, end: int):
    """算出 meta 载荷的起点，返回偏移；两种风格都不成立时返回 None。

    ⚠️ `meta` 带不带 version+flags 是 mp4 解析的经典分歧：ISO 说带（它是 FullBox），
    QuickTime 写出来的却不带。判据用「从这个起点能不能解析出子 box」，两种都试 ——
    只按一种来，换一个来源的视频就读不出东西。

    ⚠️ 另外注意：**keys 表和 ilst 是兄弟**，都在 meta 这一层。必须回到这一层去找它们；
    在 ilst 内部找 keys 永远找不到（自检时就这么错了一轮，现象是「keys 表是空的」）。
    """
    for base in (start + 4, start):
        if base >= end:
            continue
        try:
            # 只为判空：曾经写成 list(...) ⇒ 物化整个生成器。16MB 窗口在畸形数据下
            # 能产出上百万个五元组（≈200MB 峰值），而这里只需要「有没有第一个 box」。
            if next(_mp4_boxes(data, base, end), None) is not None:
                return base
        except Exception:
            continue
    return None


def _mp4_key_table(data: bytes, start: int, end: int) -> dict:
    """读 mdta 风格 meta 的 keys 表，返回 {1-based 索引: 键名}。

    🔴 这是 mp4 元数据最容易踩的一个坑，必须写清楚：
    `meta` box 的 handler 有两种，ilst 条目名的含义**完全不同** ——
      · handler = `mdta`（ffmpeg / iTunes 风格）→ 条目名是 **keys 表里的 1-based 索引**
      · handler = `mdir`（QuickTime 风格）→ 条目名才是 `©nam` 这种四字符码
    把索引当字符串解码出来会得到一串控制字符，现象是「ffprobe 明明读得到、
    我们的程序读不出来」—— 实测就是这么被绊住的（样本由 ffmpeg 写入，走的正是 mdta）。
    """
    out = {}
    try:
        for off, head, btype, ps, pe in _mp4_boxes(data, start, end):
            if btype != b"keys" or pe - ps < 8:
                continue
            count = int.from_bytes(data[ps + 4:ps + 8], "big")   # 前 4 字节是 version+flags
            p = ps + 8
            for i in range(1, count + 1):
                if p + 4 > pe:
                    break
                ksize = int.from_bytes(data[p:p + 4], "big")
                if ksize < 8 or p + ksize > pe:
                    break
                # 键名 = 命名空间(4 字节，如 "mdta") + 实际名字
                name = data[p + 8:p + ksize].decode("utf-8", "replace").strip()
                if name:
                    out[i] = name
                p += ksize
    except Exception:
        return out
    return out


def _mp4_ilst_value(data: bytes, start: int, end: int) -> str:
    """取一个 ilst 条目的文本值。

    条目下挂 data box：前 4 字节是类型（1 = UTF-8 文本）+ 4 字节 locale，之后才是内容。
    `----` 形式（mean / name / data）同样从 data 取，所以这里只认 data 就够。
    """
    try:
        for off, head, btype, ps, pe in _mp4_boxes(data, start, end):
            if btype != b"data" or pe - ps <= 8:
                continue
            return data[ps + 8:pe].decode("utf-8", "replace")
    except Exception:
        pass
    return ""


def _mp4_video_meta(data: bytes) -> dict:
    """mp4/mov/m4v：把 moov/udta/meta/ilst 下所有文本条目都取出来。

    **遍历而不是按名字取**：不同工具写进来的键名并不一致（ffmpeg 的自定义键叫
    `prompt`，标准键可能落在 `©cmt`），按名字取必然漏。统一捞出来，
    「哪个像 ComfyUI 的工作流」留给上层判断。

    条目名的解析要**分两种风格**（见 _mp4_key_table 的说明）：mdta 走 keys 索引表，
    QuickTime 的 mdir 才是四字符码。判断办法是「先当索引查表，查得到就用表里的名字」——
    索引合理时它必然落在 keys 表范围内，落不到就说明这是四字符码。
    """
    out = {}
    try:
        for off, head, btype, ps, pe in _mp4_boxes(data, 0, len(data)):
            if btype != b"moov":
                continue
            for o2, h2, t2, s2, e2 in _mp4_boxes(data, ps, pe):
                if t2 != b"udta":
                    continue
                for o3, h3, t3, s3, e3 in _mp4_boxes(data, s2, e2):
                    if t3 != b"meta":
                        continue
                    base = _mp4_meta_base(data, s3, e3)
                    if base is None:
                        continue
                    # keys 与 ilst 是兄弟、都在 meta 这一层：先把键名表建好，
                    # 才能把 ilst 条目名（mdta 风格下是索引）翻译成真正的键名
                    keytab = _mp4_key_table(data, base, e3)
                    for o4, h4, t4, s4, e4 in _mp4_boxes(data, base, e3):
                        if t4 != b"ilst":
                            continue
                        for o5, h5, t5, s5, e5 in _mp4_boxes(data, s4, e4):
                            val = _mp4_ilst_value(data, s5, e5)
                            if not val:
                                continue
                            key = keytab.get(int.from_bytes(t5, "big"))
                            if not key:
                                key = t5.decode("latin-1", "replace").strip()
                            # 键名可能带 mdta 命名空间前缀，去掉只留本体
                            if key.lower().startswith("mdta"):
                                key = key[4:].strip()
                            if key:
                                out[key] = val
    except Exception:
        return out
    return out


def _ebml_id(data: bytes, pos: int):
    """读 EBML 元素 ID。ID 是变长整数，**值含 marker bit**（0x45A3 就是 0x45A3）。
    返回 (id, 占几字节)；读不了返回 (None, 0)。"""
    if pos >= len(data):
        return None, 0
    first = data[pos]
    if first == 0:
        return None, 0
    n, mask = 1, 0x80
    while n <= 4 and not (first & mask):
        mask >>= 1
        n += 1
    if n > 4 or pos + n > len(data):
        return None, 0
    return int.from_bytes(data[pos:pos + n], "big"), n


def _ebml_size(data: bytes, pos: int):
    """读 EBML 长度字段。与 ID 相反，长度字段的**值不含** marker bit。
    返回 (size, 占几字节)；读不了返回 (None, 0)。"""
    if pos >= len(data):
        return None, 0
    first = data[pos]
    if first == 0:
        return None, 0
    n, mask = 1, 0x80
    while n <= 8 and not (first & mask):
        mask >>= 1
        n += 1
    if n > 8 or pos + n > len(data):
        return None, 0
    val = first & (mask - 1)
    for i in range(1, n):
        val = (val << 8) | data[pos + i]
    return val, n


def _ebml_children(data: bytes, start: int, end: int):
    """遍历 [start,end) 里的 EBML 子元素，产出 (id, 载荷起, 载荷止)。"""
    pos = start
    while pos < end:
        eid, n1 = _ebml_id(data, pos)
        if eid is None:
            return
        size, n2 = _ebml_size(data, pos + n1)
        if size is None:
            return
        ps = pos + n1 + n2
        if ps > end:
            return
        # 全 1 的长度 = 「未知长度，延伸到父元素末尾」（流式写入常见）
        unknown = size == (1 << (7 * n2)) - 1
        pe = end if unknown else min(ps + size, end)
        yield eid, ps, pe
        if unknown:
            return                      # 未知长度后面不会再有序的兄弟元素
        pos = pe


def _webm_tags_from(data: bytes, start: int, end: int) -> dict:
    """从 [start,end) 范围内的 Tags 元素里取 TagName → TagString。

    单独抽出来是因为有**两条入口**：
      · 常规路径：在 Segment 里找到 Tags（Segment 长度已知）；
      · 尾部探测路径：直接在文件尾部片段里搜到 Tags 的 ID —— 此时数据里没有 Segment，
        因为 Segment 的长度常常是「未知」（流式写入），我们只能跳到 Tags 处起解析。
    """
    out = {}
    try:
        for eid, ps, pe in _ebml_children(data, start, end):
            if eid != 0x1254C367:                # Tags
                continue
            for e2, p2, q2 in _ebml_children(data, ps, pe):
                if e2 != 0x7373:                 # Tag
                    continue
                for e3, p3, q3 in _ebml_children(data, p2, q2):
                    if e3 != 0x67C8:             # SimpleTag
                        continue
                    name = value = ""
                    for e4, p4, q4 in _ebml_children(data, p3, q3):
                        if e4 == 0x45A3:         # TagName
                            name = data[p4:q4].decode("utf-8", "replace").strip()
                        elif e4 == 0x4487:       # TagString
                            value = data[p4:q4].decode("utf-8", "replace")
                    if name and value:
                        out[name] = value
    except Exception:
        return out
    return out


def _webm_video_meta(data: bytes) -> dict:
    """webm/mkv：走 EBML Segment → Tags → Tag → SimpleTag。"""
    try:
        for eid, ps, pe in _ebml_children(data, 0, len(data)):
            if eid != 0x18538067:                    # Segment
                continue
            got = _webm_tags_from(data, ps, pe)
            if got:
                return got
    except Exception:
        pass
    return {}


def _read_at(path: Path, offset: int, n: int) -> bytes:
    """从文件指定位置读一段。offset 为负时按「距文件尾」处理。

    n <= 0 直接返回空 —— 因为 `f.read(-1)` 会**读整个文件**，那是几 GB 的意外内存占用，
    调用方少传或传错一个参数就会踩到（实测：50 字节的文件上 n=-1 读出了 50 字节 = 全部）。
    挡在入口比依赖调用方自觉更可靠。
    """
    if n <= 0:
        return b""
    try:
        size = path.stat().st_size
    except OSError:
        return b""
    if offset < 0:
        offset = max(0, size + offset)
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            return f.read(n)
    except OSError as ex:
        log(f"[meta] 读取失败：{path.name} {ex}")
        return b""


def _moov_candidates(data: bytes, limit: int = 8):
    """在片段里找 moov 的**可能起点**。

    读的是文件尾部片段时，片段起点会落在某个 box 中间，直接当 box 解析必然失败。
    所以先搜 `moov` 这 4 个字节，再回退 4 字节（box size 的位置）当候选起点，
    逐个试着解析 —— 谁解得出来算谁的。
    """
    out, i = [], data.find(b"moov")
    while i >= 4 and len(out) < limit:
        out.append(i - 4)
        i = data.find(b"moov", i + 1)
    return out


def video_meta(path: Path) -> dict:
    """读视频容器里的生成参数，返回与 png_meta 同构的 dict（prompt / workflow）。

    ⚠️ 键名统一转小写再匹配：**ffmpeg 写 webm 时会把自定义 tag 名转成大写**
    （实测得到 PROMPT / WORKFLOW，而 mp4 里是小写）。不统一大小写的话，
    webm 一个都匹配不上，而且看起来会像「这个格式本来就不带元数据」。

    不整体读文件，只探头、尾两段（原因见 VIDEO_PROBE_BYTES 的说明）。
    """
    kind = _VIDEO_META_KIND.get(path.suffix.lower())
    if not kind:
        return {}
    try:
        size = path.stat().st_size
    except OSError as ex:
        log(f"[meta] 读取视频失败：{path.name} {ex}")
        return {}
    if size <= 0:
        return {}

    if kind == "webm":
        # 通常 Tags 在 Segment 前部（muxer 先写头再写簇），读头就够。
        # 但它**也可以写在所有 Cluster 之后**（分段/流式写入时很常见），那时就落在头部
        # 窗口之外 —— 所以头部没找到时同样要探尾部，与 mp4 对称。
        # （审查实测：20MB 的 webm 把 Tags 放末尾，修这条之前读不到，且是静默失败。）
        raw = _webm_video_meta(_read_at(path, 0, VIDEO_PROBE_BYTES))
        if not raw and size > VIDEO_PROBE_BYTES:
            tail = _read_at(path, size - VIDEO_PROBE_BYTES, VIDEO_PROBE_BYTES)
            # EBML 是顺序容器、没有全局索引，没法像 mp4 那样按偏移定位；
            # 但 Tags 的 ID 是固定 4 字节，直接在尾部片段里搜它，找到就从那里起解析。
            # 尾部片段起点落在某个元素中间，所以搜到的位置可能不是真正的元素边界 ——
            # 这里只取第一个命中，_webm_tags_from 内部结构不对时会安全返回空。
            idx = tail.find(_TAGS_ID)
            if idx >= 0:
                raw = _webm_tags_from(tail, idx, len(tail))
    else:
        # moov 可能在头（开了 faststart）也可能在尾（ffmpeg 默认）—— 两头都探。
        # 只探一头必然漏掉一半来源，而且现象是「有些视频读不到」，很难归因。
        raw = _mp4_video_meta(_read_at(path, 0, VIDEO_PROBE_BYTES))
        if not raw and size > VIDEO_PROBE_BYTES:
            tail = _read_at(path, size - VIDEO_PROBE_BYTES, VIDEO_PROBE_BYTES)
            for st in _moov_candidates(tail):
                raw = _mp4_video_meta(tail[st:])
                if raw:
                    break
    if not raw:
        return {}
    low = {}
    for k, v in raw.items():
        low[str(k).strip().lower()] = v
    out = {}
    for key in ("prompt", "workflow", "parameters"):
        if low.get(key):
            out[key] = low[key]
    return out


def send_to_comfy(prompt: dict, base=None) -> dict:
    """把 API 格式 prompt POST 到本地 ComfyUI 的 /prompt 重新生成。"""
    base = (base or COMFY_BASE).rstrip("/")
    try:
        data = json.dumps({"prompt": prompt}).encode("utf-8")
        req = urllib.request.Request(base + "/prompt", data=data, headers={
            "Content-Type": "application/json"})
        with _OPENER_NO_PROXY.open(req, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8"))
        if j.get("prompt_id"):
            return {"ok": True, "prompt_id": j["prompt_id"]}
        return {"ok": False, "error": "ComfyUI 未返回 prompt_id: " + str(j)[:200]}
    except urllib.error.HTTPError as ex:
        try:
            detail = json.loads(ex.read().decode("utf-8", "replace"))
        except Exception:
            detail = {}
        err = detail.get("error") or str(detail)[:200] or str(ex)
        return {"ok": False, "error": f"ComfyUI 拒绝提交: {err}"}
    except Exception as ex:
        return {"ok": False,
                "error": f"无法连接 ComfyUI({base}): {type(ex).__name__}: {ex}"}


# ------------------------------------------------- 工作流模板合成（导入 UI 摆好参数，不自动跑）

_COMFY_ROOT_CACHE = [None]     # 全盘探测缓存（None=未探测，False=探测失败）


def comfy_root():
    """定位 ComfyUI 安装目录：COMFYUI_ROOT 环境变量 → 当前根目录(…/output)推导 → 全盘探测。"""
    env = os.environ.get("COMFYUI_ROOT")
    if env:
        p = Path(env)
        if p.is_dir():
            return p
    root = STATE.get("root")
    if root:
        pr = Path(root)
        if pr.name.lower() == "output":
            return pr.parent
    if _COMFY_ROOT_CACHE[0] is None:
        d = _detect_comfyui_output()
        _COMFY_ROOT_CACHE[0] = Path(d).parent if d else False
    return _COMFY_ROOT_CACHE[0] or None


def comfy_workflows_dir() -> Path:
    """ComfyUI 的 UI 工作流模板目录。"""
    r = comfy_root()
    if r:
        return r / "user" / "default" / "workflows"
    return Path(os.devnull)      # 占位：glob / is_file 均安全落空


# 批次 F · F6：模板目录指纹缓存。
# 现状：每次 `/api/templates` 都要把 52 个模板（合计 3.3 MB）全部 json.loads
# 只为了判断「含不含 KSampler + CLIPTextEncode」—— 冷 1181 ms、稳态 97 ms，
# 而这个列表在一次会话里几乎不变（改模板是低频操作）。
#
# 指纹取「文件名 + size + mtime_ns」的整表元组，而不是只看目录 mtime：
# Windows 上**覆盖写同一文件不会改目录 mtime**，只看目录的话
# 「改了模板内容，CMV 里 compatible 还是旧的」。52 次 stat 约 0.2 ms，可以忽略。
#
# ⚠️ 不要改成子串匹配代替 JSON 解析 —— 实测 38 ms vs 30 ms，**子串更慢**，
#    而且 `"KSampler"` 出现在注释/标题里会误判（方案文档 §F6 已记）。
_TPL_CACHE = {"key": None, "val": []}
_TPL_LOCK = threading.Lock()


def _tpl_dir_key(d: Path):
    """模板目录指纹；目录读不了返回 None（表示「别用缓存」）。"""
    parts = []
    try:
        for f in sorted(d.glob("*.json")):
            if f.name.startswith("."):
                continue
            try:
                st = f.stat()
                parts.append((f.name, st.st_size, st.st_mtime_ns))
            except OSError:
                parts.append((f.name, -1, -1))
    except OSError:
        return None
    return tuple(parts)


def list_templates() -> list:
    """ComfyUI 已保存的 UI 工作流模板 [{id, name, compatible}]，按名称排序。

    compatible=模板含 KSampler+CLIPTextEncode，可承载生成参数（Txt2Img 类）。
    批次 F（F6）：按目录指纹缓存（见 `_tpl_dir_key`），命中时直接返回。
    """
    d = comfy_workflows_dir()
    key = _tpl_dir_key(d)
    if key is not None:
        with _TPL_LOCK:
            if _TPL_CACHE["key"] == key:
                return [dict(x) for x in _TPL_CACHE["val"]]
    try:
        files = sorted(f for f in d.glob("*.json") if not f.name.startswith("."))
    except OSError:
        return []
    out = []
    for f in files:
        has_s = has_e = False
        try:
            obj = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            obj = None
        # B2-P1-2：模板目录是用户可写的，里面什么 .json 都可能有 —— 手工丢进去的
        # 配置文件、别的工具导出的元数据、null、顶层数组、"nodes" 是对象/数字…
        # 旧代码直接 obj.get("nodes") 再对每个元素 n.get("type")：撞上 list / int 就抛
        # AttributeError（不在 except 里）⇒ 整个 /api/templates 500，**一个坏文件拖垮
        # 全部模板**。现在结构不对就当「不兼容」，只影响它自己那一条。
        nodes = obj.get("nodes") if isinstance(obj, dict) else None
        if isinstance(nodes, list):
            has_s = any(isinstance(n, dict)
                        and n.get("type") in ("KSampler", "KSamplerAdvanced") for n in nodes)
            has_e = any(isinstance(n, dict) and n.get("type") == "CLIPTextEncode"
                        for n in nodes)
        out.append({"id": f.name, "name": f.stem, "compatible": has_s and has_e})
    if key is not None:
        with _TPL_LOCK:
            _TPL_CACHE["key"], _TPL_CACHE["val"] = key, out
    return [dict(x) for x in out]


_SIZES_MIN, _SIZES_MAX = 1, 8192


def _parse_size(v):
    """'512 x 512' / '512x512' / '512×512' → (512, 512)；格式不对或越界返回 None。

    B2-P2-6：`0 x 0`、负数、十万级尺寸旧版原样写进 EmptyLatentImage —— ComfyUI 侧
    要么报空张量错误、要么在超大尺寸下直接吃满内存；`512x` 这种格式错误则是静默跳过。
    写入前统一走这里：合法才写，不合法进回执。
    """
    parts = str(v or "").replace("×", "x").split("x")
    if len(parts) != 2:
        return None
    try:
        w, h = int(parts[0].strip()), int(parts[1].strip())
    except (TypeError, ValueError):
        return None
    if not (_SIZES_MIN <= w <= _SIZES_MAX and _SIZES_MIN <= h <= _SIZES_MAX):
        return None
    return w, h


def _apply_parsed(tpl: dict, parsed: dict) -> dict:
    """把解析出的参数写进 UI 格式模板（就地修改 nodes 的 widgets_values）。

    采样器 / 大模型 / 出图尺寸只写第一个节点，避免多段采样（hires 等）模板被
    同一组参数无差别覆盖；LoRA 按模板里的 LoRA 节点顺序逐个填入，装不下的丢弃。
    A1111 来源的参数在此做类型与名称转换。

    返回回执 {"applied": [中文名…], "skipped": [{"name","why"}…]}。
    B2-P2-3 / P2-6 / P2-7：旧版「写不进去就当没这回事」—— `_num` 转不动、模板里缺
    对应节点、尺寸非法，全都静默保留模板原值，接口照样 ok:True。用户拿到的工作流
    一半是本图参数、一半是模板默认值，而界面上看不出来。回执就是那个出口。
    """
    rep = {"applied": [], "skipped": []}

    def ok(name):
        if name not in rep["applied"]:
            rep["applied"].append(name)

    def skip(name, why):
        rep["skipped"].append({"name": name, "why": why})

    def recorded(name):
        return name in rep["applied"] or any(s["name"] == name for s in rep["skipped"])

    nodes = tpl.get("nodes") or []
    clipse = [n for n in nodes if isinstance(n, dict) and n.get("type") == "CLIPTextEncode"]
    pos_n = neg_n = None
    for n in clipse:
        t = (n.get("title") or "").lower()
        if not pos_n and ("positive" in t or "正" in t) and "neg" not in t:
            pos_n = n
        elif not neg_n and ("negative" in t or "负" in t or "反向" in t):
            neg_n = n
    # 7-4：兜底配对只允许「双侧都没识别出」时整对猜（clipse[0]/clipse[-1]）；
    #      只识别出一侧时，仅当全图恰好两个编码器才取另一个——多编码器模板里
    #      乱猜会把负向提示词写进第二个正向节点（覆盖原正向内容且无任何报错）。
    if not pos_n and not neg_n and len(clipse) >= 2:
        pos_n, neg_n = clipse[0], clipse[-1]
    elif neg_n is None and pos_n is not None and len(clipse) == 2:
        neg_n = clipse[0] if clipse[1] is pos_n else clipse[1]
    elif pos_n is None and neg_n is not None and len(clipse) == 2:
        pos_n = clipse[0] if clipse[1] is neg_n else clipse[1]

    def set_text(n, val, label):
        """写提示词 + 记账。n 为 None 表示模板里根本没有该侧的节点。"""
        if n is None:
            skip(label, "模板里没有对应的提示词节点")
            return
        wv = n.get("widgets_values")
        if isinstance(wv, list) and len(wv) > 0:
            wv[0] = val
            ok(label)
        else:
            skip(label, "模板里该节点的参数位不够")

    # B2-P2-4：「确认空」写成空串（否则模板作者写的负向会留下来冒充本图参数）；
    # 「解析不到」才保留模板原值 —— 两者在 parse_comfy_prompt 里已分别落到
    # parsed[side] 与 parsed["_empty"]。
    _empty = set(parsed.get("_empty") or [])
    for _side, _node, _label in (("positive", pos_n, "正向提示词"),
                                 ("negative", neg_n, "负向提示词")):
        _val = parsed.get(_side)
        if _val is None and _side in _empty:
            _val = ""
        if _val is None:
            continue
        set_text(_node, _val, _label)

    seed = _num(parsed.get("seed"), as_int=True)
    steps = _num(parsed.get("steps"), as_int=True)
    cfg = _num(parsed.get("cfg"))
    # B2-P2-3：有值但转不动（"20 步" / "7,5" / "N/A"）⇒ 模板原值会被保留，
    # 必须让用户知道那不是本图的参数。
    for _raw, _conv, _label in ((parsed.get("seed"), seed, "Seed"),
                                (parsed.get("steps"), steps, "步数"),
                                (parsed.get("cfg"), cfg, "CFG")):
        if _raw is not None and _raw != "" and _conv is None:
            skip(_label, "数值无法识别：%s" % _raw)

    sampler = sch_from_sampler = None
    if parsed.get("sampler"):
        sampler, sch_from_sampler = _a1111_sampler(str(parsed["sampler"]))
    scheduler = parsed.get("scheduler")
    if scheduler:
        scheduler = _A1111_SCHEDULERS.get(str(scheduler).strip().lower(),
                                          str(scheduler).strip().lower())
    elif sch_from_sampler:
        scheduler = sch_from_sampler          # "DPM++ 2M Karras" 这类名字自带的调度器

    _size = parsed.get("size")
    _wh = _parse_size(_size) if _size else None
    if _size and _wh is None:
        skip("出图尺寸", "数值不可用：%s" % _size)

    pending_loras = list(parsed.get("loras") or [])
    done = set()
    for n in nodes:
        if not isinstance(n, dict):
            continue
        ty = n.get("type") or ""
        tyl = ty.lower()
        wv = n.get("widgets_values")
        if not isinstance(wv, list):
            continue
        if ty == "KSampler":        # [seed, ctrl, steps, cfg, sampler, scheduler, denoise]
            if "sampler" in done:
                continue
            done.add("sampler")
            if seed is not None and len(wv) > 0:
                wv[0] = seed
                ok("Seed")
            if steps is not None and len(wv) > 2:
                wv[2] = steps
                ok("步数")
            if cfg is not None and len(wv) > 3:
                wv[3] = cfg
                ok("CFG")
            if sampler and len(wv) > 4:
                wv[4] = sampler
                ok("采样器")
            if scheduler and len(wv) > 5:
                wv[5] = scheduler
                ok("调度器")
        elif ty == "KSamplerAdvanced":
            # [add_noise, noise_seed, ctrl, steps, cfg, sampler, scheduler, ...]
            if "sampler" in done:
                continue
            done.add("sampler")
            if seed is not None and len(wv) > 1:
                wv[1] = seed
                ok("Seed")
            if steps is not None and len(wv) > 3:
                wv[3] = steps
                ok("步数")
            if cfg is not None and len(wv) > 4:
                wv[4] = cfg
                ok("CFG")
            if sampler and len(wv) > 5:
                wv[5] = sampler
                ok("采样器")
            if scheduler and len(wv) > 6:
                wv[6] = scheduler
                ok("调度器")
        elif "lora" in tyl and "trigger" not in tyl and pending_loras:
            first = pending_loras.pop(0)
            # 7-3：强度允许负数（概念抹除/反向 LoRA，如 <lora:xxx:-1> 会生成 "xxx (-1)"）；
            #      旧正则 [\d.]+ 不吃负号 ⇒ 整串「名字 (-1)」被当文件名写进 widgets_values[0]，
            #      ComfyUI 节点拿到不存在的文件名直接报错。负号是唯一缺口，加一个字符即闭合。
            m = re.match(r"(.+?)\s*\((-?[\d.]+)\)\s*$", first)
            nm, st = (m.group(1), float(m.group(2))) if m else (first, None)
            if len(wv) > 0:
                wv[0] = nm
                if st is not None and len(wv) > 1:
                    wv[1] = st
                ok("LoRA")
            else:
                pending_loras.insert(0, first)      # 写不进去就还回去，统一在循环后报
        elif ("checkpoint" in tyl and "loader" in tyl) or "unetloader" in tyl:
            if "ckpt" in done:
                continue
            done.add("ckpt")
            if parsed.get("model") and len(wv) > 0:
                wv[0] = parsed["model"]
                ok("模型")
        elif "empty" in tyl and "latent" in tyl and _wh:
            if "latent" in done:
                continue
            done.add("latent")
            if len(wv) > 1:
                wv[0], wv[1] = _wh
                ok("出图尺寸")
            else:
                skip("出图尺寸", "模板里该节点的参数位不够")

    # —— 循环后统一结算「本图有、但没写进去」的参数 ——
    if "sampler" not in done:
        for _k, _label in (("seed", "Seed"), ("steps", "步数"), ("cfg", "CFG"),
                           ("sampler", "采样器"), ("scheduler", "调度器")):
            if parsed.get(_k) in (None, "") or recorded(_label):
                continue
            skip(_label, "模板里没有可写入的采样节点")
    if parsed.get("model") and "ckpt" not in done and not recorded("模型"):
        skip("模型", "模板里没有可写入的模型加载节点")
    if pending_loras:
        skip("LoRA", "模板里的 LoRA 节点不够，未写入 %d 个" % len(pending_loras))
    return rep


def _save_filter_string(pairs) -> str:
    """[(显示名, 通配)] → Win32 OPENFILENAME 的过滤器多字符串。

    形态是「显示名\\0通配\\0显示名\\0通配\\0」再补一个 NUL 收尾（双 NUL 表示结束）；
    create_unicode_buffer 还会自己补一个结尾 NUL，所以这里显式给到 "\\0\\0"。
    """
    return "".join("%s\0%s\0" % (label, pattern) for label, pattern in pairs) + "\0"


def _ask_save_path(suggested_name: str, initial_dir: str = "", filter_pairs=None,
                   title: str = "保存工作流 JSON", def_ext: str = "json"):
    """弹 Windows 原生「另存为」对话框，返回用户选定的完整路径。

    返回三态：
      - 非空字符串：用户选定的完整路径（文件可能已存在，覆盖由调用方负责）
      - ""        ：用户取消
      - None      ：当前环境弹不出框（非 Windows / ctypes 调用失败）⇒ 调用方回落自动落盘

    只用 comdlg32!GetSaveFileNameW（系统自带，无第三方依赖、不增打包体积），
    与 recycle_bin_delete 同款路子。请求跑在 ThreadingHTTPServer 子线程上，
    模态框只阻塞这一个请求线程，其它接口照常响应。

    hwndOwner 取前台窗口：否则弹框没有归属，很容易被应用窗口盖住，
    用户会以为「点了导出没反应」。

    filter_pairs / title / def_ext 是给「另存为原件」复用的（2026-11-08）：
    默认值仍是「工作流 JSON」，导出那条链路的行为一个字都没变。
    """
    if str(os.environ.get("CMV_NO_SAVE_DIALOG") or "").strip() not in ("", "0"):
        # 测试 / 无 GUI 环境：跳过对话框，让调用方走「自动落盘回落」分支
        # （回落 = 写到原图所在目录 + _unique_path 去重，调用方仍如实回传路径）
        return None
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes as W
        user32 = ctypes.windll.user32
        comdlg32 = ctypes.windll.comdlg32
        ole32 = ctypes.windll.ole32
    except (OSError, AttributeError):
        return None

    OFN_OVERWRITEPROMPT = 0x00000002
    OFN_NOCHANGEDIR     = 0x00000008
    OFN_PATHMUSTEXIST   = 0x00000800
    OFN_EXPLORER        = 0x00080000

    class OPENFILENAMEW(ctypes.Structure):
        # 字段顺序/类型须与 Win32 OPENFILENAMEW 严格一致，否则 GetSaveFileNameW
        # 以 CDERR_STRUCTSIZE 直接失败（hwnd 与指针在 x64 上各占 8 字节）
        _fields_ = [
            ("lStructSize",       W.DWORD),
            ("hwndOwner",         W.HWND),
            ("hInstance",         W.HINSTANCE),
            ("lpstrFilter",       W.LPCWSTR),
            ("lpstrCustomFilter", W.LPWSTR),
            ("nMaxCustFilter",    W.DWORD),
            ("nFilterIndex",      W.DWORD),
            ("lpstrFile",         W.LPWSTR),
            ("nMaxFile",          W.DWORD),
            ("lpstrFileTitle",    W.LPWSTR),
            ("nMaxFileTitle",     W.DWORD),
            ("lpstrInitialDir",   W.LPCWSTR),
            ("lpstrTitle",        W.LPCWSTR),
            ("Flags",             W.DWORD),
            ("nFileOffset",       W.WORD),
            ("nFileExtension",    W.WORD),
            ("lpstrDefExt",       W.LPCWSTR),
            ("lCustData",         ctypes.c_ssize_t),   # LPARAM = LONG_PTR，指针宽
            ("lpfnHook",          ctypes.c_void_p),
            ("lpTemplateName",    W.LPCWSTR),
            ("pvReserved",        ctypes.c_void_p),
            ("dwReserved",        W.DWORD),
            ("FlagsEx",           W.DWORD),
        ]

    hr = ole32.CoInitializeEx(None, 0x2)          # COINIT_APARTMENTTHREADED
    own_com = hr in (0, 1)                        # S_OK / S_FALSE 才是本线程自己初始化的
    try:
        buf = ctypes.create_unicode_buffer(suggested_name, 4096)
        # 过滤器是「显示名\0通配\0……\0\0」的双 NUL 结尾多字符串
        filt = ctypes.create_unicode_buffer(_save_filter_string(
            filter_pairs or [("工作流 JSON (*.json)", "*.json"),
                             ("所有文件 (*.*)", "*.*")]))
        ofn = OPENFILENAMEW()
        ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
        try:
            user32.GetForegroundWindow.restype = W.HWND     # 默认 int 会截断 64 位句柄
            ofn.hwndOwner = user32.GetForegroundWindow()
        except Exception:
            ofn.hwndOwner = None
        ofn.lpstrFilter = ctypes.cast(filt, W.LPCWSTR)
        ofn.nFilterIndex = 1
        ofn.lpstrFile = ctypes.cast(buf, W.LPWSTR)
        ofn.nMaxFile = 4096
        ofn.lpstrInitialDir = initial_dir or None
        ofn.lpstrTitle = title
        ofn.lpstrDefExt = def_ext
        ofn.Flags = (OFN_OVERWRITEPROMPT | OFN_PATHMUSTEXIST
                     | OFN_EXPLORER | OFN_NOCHANGEDIR)
        comdlg32.GetSaveFileNameW.restype = W.BOOL
        comdlg32.GetSaveFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
        if not comdlg32.GetSaveFileNameW(ctypes.byref(ofn)):
            return ""                             # 用户取消
        return buf.value
    except Exception as ex:
        log("[导出] 保存对话框不可用，回落自动落盘：%s: %s" % (type(ex).__name__, ex))
        return None
    finally:
        if own_com:
            try:
                ole32.CoUninitialize()
            except Exception:
                pass


# ==================== 混淆副本落盘（小番茄口径 · 2026-11-09 改口径） ====================
# 2026-11-08 这里有一整套后端算法（Gilbert 曲线 + 位移，约 250 行，PIL 现算）。
# 2026-11-09 与第三方 V2.13.2 StartupFixed 合版时定为**只保留前端 Worker 一套**：
#   · 算法在 frontend/js/scramble-worker.js（OffscreenCanvas，PNG 无损、保留 alpha）；
#   · 后端只负责把前端算好的 PNG 按既有命名规则写到 `_混淆\` —— 桌面壳里
#     WebView 的 <a download> 不保证弹保存框，由后端落盘才可靠；
#     两边各算一套则一旦漂移没人会发现（算法无密钥，漂移了照样「看着像」）。
# 正确性由 tests/hunxiao_20261108_check.py 的黄金样张比对兜底（像素级，MAE 必须为 0）。
#
# 刻意保留的三条语义（与 11-08 版一致）：
#   ① 副本落在原图同级的 `_混淆` 子目录 —— 非递归浏览时不会混进图库网格；
#   ② 来回多趟不叠后缀（xxx_解混淆_混淆_解混淆… 这种名字越点越长，也会把「哪张是原件」搞丢）；
#   ③ 输出 PNG（无损）且**不带任何元数据** —— 混淆图若还挂着原始提示词，混淆就白做了。
SAVE_COPY_MAX_BYTES = 60 << 20   # 32M 像素 PNG 的极端体积也能过；再大明确报错，不静默截断
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _copy_out_path(src: Path, mode: str) -> Path:
    """副本路径：原图同级的 `_混淆` 子目录，扩展名固定 .png（前端 Worker 产出的就是 PNG）。

    源本身就在 `_混淆` 里（对副本再做一次还原）时不再套一层，否则会生成
    `_混淆/_混淆/`，越点越深；同名不覆盖，第二份起 `-2` / `-3`。
    """
    d = src.parent
    if d.name != "_混淆":
        d = d / "_混淆"
    tag = "_混淆" if mode == "encrypt" else "_解混淆"
    base = src.stem
    while base.endswith("_混淆") or base.endswith("_解混淆"):
        base = base[:-len("_混淆")] if base.endswith("_混淆") else base[:-len("_解混淆")]
    for k in range(1, 1000):
        name = base + tag + ("" if k == 1 else "-%d" % k) + ".png"
        p = d / name
        if not p.exists():
            return p
    return d / ("%s%s-%s.png" % (base, tag, _tmp_tag()))


def save_copy_bytes(src: Path, mode: str, data: bytes) -> dict:
    """把前端 Worker 算好的 PNG 字节写成副本；原图一个字节都不动。

    返回 {"ok": True, out/name/dir/mode/bytes} 或 {"error": …, "code": 4xx/5xx}。
    校验只做「是不是图片 / 有没有数据 / 是不是 PNG / 体积」—— 像素正确性不在这里判，
    由门禁拿黄金样张比对（服务端没有算法，也就没法自证）。
    """
    if mode not in ("encrypt", "decrypt"):
        return {"error": "mode 只能是 encrypt 或 decrypt", "code": 400}
    if src.suffix.lower() not in IMAGE_EXTS:
        return {"error": "只支持图片（%s 不是图片格式）"
                         % (src.suffix or "无扩展名"), "code": 400}
    if not data:
        return {"error": "没有收到图像数据", "code": 400}
    if not data.startswith(_PNG_MAGIC):
        return {"error": "只接受 PNG（前端 Worker 的输出格式）", "code": 400}
    if len(data) > SAVE_COPY_MAX_BYTES:
        return {"error": "图像数据过大（%.1f MB，上限 %d MB）"
                         % (len(data) / 1048576.0, SAVE_COPY_MAX_BYTES >> 20), "code": 413}

    dst = _copy_out_path(src, mode)
    tmp = dst.with_name("%s.%s.part" % (dst.name, _tmp_tag()))
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)          # 先写 .part 再原子换名：中途失败不留半截文件
        os.replace(str(tmp), str(dst))
    except Exception as ex:
        return {"error": "写盘失败：%s" % ex, "code": 500}
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    return {"ok": True, "out": str(dst), "name": dst.name, "dir": str(dst.parent),
            "mode": mode, "bytes": len(data)}

def save_image_as(src: Path, dest=None) -> dict:
    """大图右键「另存为原件（保留元数据）」：把磁盘上那一份**原样**存到用户选定的位置。

    与顶栏「下载」是两件不同的事，别合并：
      · 下载走 downloadClean —— 落到设置里的下载目录，且**默认抹掉**内嵌提示词；
      · 这里字节不动（PNG 的 tEXt / 工作流都还在），路径由用户在原生保存框里选，
        存进共享盘 / U 盘 / 聊天附件目录时用得上。

    dest=None ⇒ 弹原生保存框；结果三态与 `_ask_save_path` 一致：
      · {"ok": True, …}           已写盘（或原路径 = 目标路径，见 same）
      · {"ok": False, "canceled": …}  用户点了取消
      · {"ok": False, "no_dialog": …} 环境弹不出框 ⇒ 前端回落「浏览器原样下载」

    覆盖由保存框的 OFN_OVERWRITEPROMPT 确认过；写入先落 .part 再原子换名，
    中途失败不留半截文件（与导出 / 下载同款）。
    """
    if dest is None:
        suf = src.suffix or ".png"
        dest = _ask_save_path(
            src.name, str(src.parent),
            filter_pairs=[("%s 图片 (*%s)" % (suf.lstrip(".").upper(), suf), "*" + suf),
                          ("所有文件 (*.*)", "*.*")],
            title="另存为原件（保留内嵌元数据）",
            def_ext=suf.lstrip("."))
        if dest is None:
            return {"ok": False, "no_dialog": True}
    if dest == "":
        return {"ok": False, "canceled": True}
    dp = Path(dest)
    try:
        # 选回原文件本身：不做复制（复制到自身会截断），如实告诉前端「就是这个文件」
        if dp.resolve() == src.resolve():
            return {"ok": True, "path": str(dp), "name": dp.name, "same": True}
        tmp = dp.with_name("%s.%s.part" % (dp.name, _tmp_tag()))
        try:
            shutil.copy2(str(src), str(tmp))     # copy2 连 mtime 一起带过去
            os.replace(tmp, dp)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    except OSError as ex:
        return {"ok": False, "error": "写入失败：%s" % ex}
    return {"ok": True, "path": str(dp), "name": dp.name}


def export_workflow(path: Path, template_id: str = None) -> dict:
    """提取 PNG 内嵌 UI 工作流；缺失时用模板 + 本图参数合成。只产出，不提交生成。"""
    suf = path.suffix.lower()
    if suf == ".png":
        meta = png_meta(path)
    elif suf in (".jpg", ".jpeg", ".webp"):
        meta = parse_jpeg_meta(path)
    else:
        meta = {}
    raw = meta.get("workflow")
    if raw:
        try:
            json.loads(raw)
            return {"ok": True, "json": raw, "source": "png"}
        except ValueError:
            pass
    # —— API 格式兜底（2026-11-02）——
    # ComfyUI 的「临时保存 / API 导出」路径只写 `prompt`（API 图），不写 `workflow`（前端图）。
    # 此前这种图一律回「未内嵌 UI 工作流（API 生成）」并逼用户选模板，可它**拖进 ComfyUI
    # 画布是能载入的**（前端 drop 处理读 `json.prompt` 后调 `loadApiJson` 重建布局）。
    # 现在按同样口径兜底：包成 ComfyUI「导出 (API)」的标准形状 `{"prompt": {...}}`，
    # 由消费方（桥接脚本 / 注入表达式）识别后交给 `loadApiJson`。
    # 只认「每个值都是带 class_type 的节点对象」的字典 —— 防止把别家插件的 prompt 字段当工作流。
    _api_raw = meta.get("prompt")
    if isinstance(_api_raw, str) and _api_raw.strip():
        try:
            _api = json.loads(_api_raw)
        except ValueError:
            _api = None
        if (isinstance(_api, dict) and _api
                and all(isinstance(v, dict) and isinstance(v.get("class_type"), str)
                        for v in _api.values())):
            return {"ok": True,
                    "json": json.dumps({"prompt": _api}, ensure_ascii=False),
                    "source": "api"}
    if not template_id:
        return {"ok": False,
                "error": "该图未内嵌 UI 工作流（API 生成），请选择 ComfyUI 模板来合成参数"}
    # B2-P1-3：只禁「路径语法」本身 —— 冒号（盘符）、斜杠/反斜杠、`..` 上跳、
    # Windows 非法文件名字符、控制字符。**不能**像审计建议那样只放行
    # `[A-Za-z0-9._\- ]+`：ComfyUI 模板名大量是中文（「我的放大流程.json」），
    # 英文白名单会把一整类正常模板拒掉 —— 为了堵一条越权读而废掉正常功能。
    _bad_ch = any(c in template_id for c in '\\/:*?"<>|') or \
        any(ord(c) < 32 for c in template_id)
    if (not template_id or len(template_id) > 200 or _bad_ch
            or ".." in template_id or not template_id.lower().endswith(".json")):
        # 旧正则只排了 / 和 \：`C:secret.json` 能通过，而 ComfyUI 装在非 C 盘时
        # `Path("G:\...\workflows") / "C:secret.json"` 的盘符会胜出 ⇒ 读到进程启动
        # 目录下的任意 .json（路径穿越型越权读，且无任何告警）。
        return {"ok": False, "error": "非法模板名: " + template_id}
    _tpl_dir = comfy_workflows_dir()
    tpl_path = _tpl_dir / template_id
    if not tpl_path.is_file():
        return {"ok": False, "error": "模板不存在: " + template_id}
    try:
        # 归属校验：正则已挡盘符，这一道覆盖符号链接 / junction / 其它拼法
        if tpl_path.resolve().parent != _tpl_dir.resolve():
            return {"ok": False, "error": "非法模板名: " + template_id}
    except OSError as ex:
        return {"ok": False, "error": f"模板读取失败: {ex}"}
    try:
        tpl = json.loads(tpl_path.read_text("utf-8"))
    except (OSError, ValueError) as ex:
        return {"ok": False, "error": f"模板读取失败: {ex}"}
    if not isinstance(tpl, dict) or not tpl.get("nodes"):
        return {"ok": False, "error": "模板不是合法的 UI 工作流"}
    nodes = [n for n in (tpl.get("nodes") or []) if isinstance(n, dict)]
    has_sampler = any(n.get("type") in ("KSampler", "KSamplerAdvanced") for n in nodes)
    if not has_sampler:
        return {"ok": False,
                "error": "模板「" + template_id + "」不含采样节点（可能是放大/后处理工作流），请选带 KSampler 的 Txt2Img 模板"}
    # B2-P2-7：与 list_templates 的 compatible 口径对齐。没有编码器节点时
    # _apply_parsed 的 pos_n/neg_n 全为 None ⇒ 提示词一个也写不进去，而旧版仍返回
    # ok:True，用户拿到的是「参数对、提示词是模板作者写的」工作流。
    if not any(n.get("type") == "CLIPTextEncode" for n in nodes):
        return {"ok": False,
                "error": "模板「" + template_id + "」不含提示词编码节点（CLIPTextEncode），"
                         "提示词无法写入，请换一个带文本编码器的模板"}
    _diag = {}
    parsed = parse_meta(meta, _diag) or {}
    if not parsed:
        if _diag.get("parse_error"):
            # B2-P1-4：把「解析器崩了」如实报出来，别和「本来没有参数」混为一谈
            return {"ok": False,
                    "error": "该图的生成参数解析失败（%s）" % _diag["parse_error"]}
        return {"ok": False, "error": "该图没有可解析的生成参数，无法合成"}
    try:
        rep = _apply_parsed(tpl, parsed)
    except Exception as ex:
        return {"ok": False, "error": f"参数合成失败: {type(ex).__name__}: {ex}"}
    if not isinstance(rep, dict):
        rep = {"applied": [], "skipped": []}
    warns = list(parsed.pop("_warn", None) or [])
    parsed.pop("_empty", None)
    for _s in (rep.get("skipped") or []):
        warns.append("%s未写入：%s" % (_s.get("name") or "参数", _s.get("why") or ""))
    try:
        # B2-P1-1 的第二道闸：allow_nan=False 让残留的非有限值**在服务端**暴露
        # （否则 json.dumps 会写出裸 NaN，前端 JSON.parse 与 ComfyUI 都直接失败，
        #  而服务端仍回 ok:True）。
        body = json.dumps(_finite_clean(tpl), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as ex:
        return {"ok": False,
                "error": "工作流含无法序列化的数值（%s），已中止导出" % type(ex).__name__}
    return {"ok": True, "json": body, "source": "template",
            "parsed": _finite_clean(parsed),
            "applied": rep.get("applied") or [],
            "warnings": warns}


# ------------------------------------------------- ComfyUI 画布注入（打开窗口摆好工作流，不自动跑）

EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
CDP_PORT = 9333                            # 首选调试端口（P1-1 后仅作固定端口的兜底值）
CDP_PORT_ENV = "CMV_CDP_PORT"              # 排障时可显式指定固定端口
BROWSER_USER_DIR = DATA_DIR / "edge-profile"
# port 是**本次实例实际用的调试端口**（P1-1：不再写死 9333，spawn 前向系统要空闲口）
COMFY_BROWSER = {"proc": None, "ws": None, "port": 0}
OPEN_EDGE_BUDGET = 15.0                    # 拉起调试 Edge 的整体预算（秒），见 P2-4


def _edge_exe() -> str:
    for p in EDGE_PATHS:
        if Path(p).is_file():
            return p
    return None


def _free_port() -> int:
    """向系统要一个空闲的本机端口（P1-1）。"""
    import socket as _sock
    s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def _cdp_port() -> int:
    """本次实例正在用的调试端口。未拉起时抛异常（调用方据此判「端口上没人」）。"""
    p = int(COMFY_BROWSER.get("port") or 0)
    if not p:
        raise RuntimeError("调试浏览器尚未拉起")
    return p


def _cdp_http(path: str, timeout: float = 8) -> dict:
    with _OPENER_NO_PROXY.open(
            f"http://127.0.0.1:{_cdp_port()}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _edge_cdp_ok(timeout: float = 1.2) -> bool:
    """调试端口上应答的是不是 Microsoft Edge —— P1-1 的**归属校验**。

    只看「端口有人应答」远不够：任意 HTTP 服务占住端口都能满足，
    而接下来的 Page.navigate / Runtime.evaluate 会把**整份工作流 JSON**
    发给那个未知目标（轻则注入失败，重则工作流内容被第三方调试端点拿走）。
    CDP 的 /json/version 里 Browser 形如 "Edg/131.0.2903.86"；普通 HTTP 服务
    给不出这个字段，Chrome 给的是 "Chrome/…"。
    """
    try:
        ver = _cdp_http("/json/version", timeout=timeout)
    except Exception:
        return False
    blob = " ".join(str((ver or {}).get(k) or "")
                    for k in ("Browser", "User-Agent"))
    return "Edg/" in blob


def _stop_proc(proc):
    """终止一个进程句柄（Windows 上 terminate 不响应则 kill）。"""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
    except Exception:
        pass


def _close_comfy_browser(reason: str = ""):
    """回收独立调试 Edge 与 CDP 连接（P1-3）。

    此前全文件没有任何退出回收点：带 --remote-debugging-port 的 Edge 会一直
    常驻后台，调试端口对**本机任意进程**开放（可在里面导航、执行脚本），而且
    长期占着端口，反过来让下一次 _ensure_edge 误判（P1-1 / P1-2 的成因之一）。
    调用点：main() 的 finally、restart_self 的退出路径。
    """
    ws = COMFY_BROWSER.get("ws")
    COMFY_BROWSER["ws"] = None
    if ws is not None:
        try:
            import ws_client as _wsc
            _wsc.ws_close(ws)
        except Exception:
            pass
    proc = COMFY_BROWSER.get("proc")
    port = int(COMFY_BROWSER.get("port") or 0)
    if proc is None and port:
        # A13（2026-11-05 审计复核）：`_ensure_edge` 的复用分支（端口上已有 Edge、
        # 但本进程没有句柄）会把 proc 置 None。而这里见 None 就直接 return ⇒
        # 那个复用来的调试 Edge **永远不会被回收**：重启/退出后仍常驻，占着端口，
        # 且调试口对本机任意进程开放（能在里面导航、执行脚本）。
        # 没句柄就用 CDP 让它自己关 —— Browser.close 关的是整个浏览器实例。
        # 必须在 `COMFY_BROWSER["port"]=0` **之前**做：`_cdp_http` 靠它找端口。
        if _edge_cdp_ok():
            try:
                ver = _cdp_http("/json/version", timeout=3)
                u = (ver or {}).get("webSocketDebuggerUrl")
                if u:
                    import ws_client as _wsc
                    _w = _wsc.ws_connect(u, timeout=4)
                    try:
                        _wsc.cdp_call(_w, "Browser.close", timeout=4)
                    finally:
                        _wsc.ws_close(_w)
                    log("[comfy] 复用来的调试 Edge 已通过 CDP Browser.close 关闭")
            except Exception as ex:
                log(f"[warn] 关闭复用的调试 Edge 失败（忽略）：{type(ex).__name__}: {ex}")
    COMFY_BROWSER["proc"] = None
    COMFY_BROWSER["port"] = 0
    if proc is None:
        # 没句柄时仍要清 singleton 锁：留着它，下次 spawn 的实例会附着到
        # 残留进程后立刻退出，句柄永远对不上（P1-2 的窗口堆积就是这么来的）。
        for nm in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
            try:
                (BROWSER_USER_DIR / nm).unlink()
            except OSError:
                pass
        if reason:
            log(f"[comfy] 已回收调试浏览器（{reason}）")
        return
    _stop_proc(proc)
    # 清 singleton 锁：留着它，下次 spawn 的实例会附着到残留进程后立刻退出，
    # 句柄永远对不上（P1-2 的窗口堆积就是这么来的）。
    for nm in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            (BROWSER_USER_DIR / nm).unlink()
        except OSError:
            pass
    if reason:
        log(f"[comfy] 已回收调试浏览器（{reason}）")


def _ensure_edge(budget: float = OPEN_EDGE_BUDGET):
    """确保带调试端口的独立 Edge 实例在跑（独立 profile，不影响日常浏览器）。

    ε-13（§6.6）：此前旧 Edge 的进程句柄被静默丢弃 —— 句柄还在但不响应 CDP
    （浏览器假死 / 调试通道卡住）时，直接再 spawn 一个新 Edge：同一个
    --user-data-dir + 同一个调试端口，新实例要么附着到旧进程上要么起不来，
    而旧的那个永远没人回收。现在 spawn 前先把旧进程 terminate/kill 干净。

    P1-2：复用判据从「我们手上的句柄还活着」改成「调试端口上是 Edge」。
    句柄已退出但端口仍有 Edge 应答（singleton 附着后父进程先退、或上一进程
    留下的实例）时，旧实现一律清掉重新 spawn —— 而新实例又附着到已有实例后
    立刻退出，句柄永远对不上 ⇒ 每次「打开画布」都新起一个短命进程、并借
    singleton 多开一个 about:blank 窗口。现在这种情形直接复用。

    P2-4：就绪轮询原来是「50 次 × 单次 8s」= 最坏约 400s 卡住请求线程；
    现在按整体预算 budget 走、单次探测 1s。
    """
    old = COMFY_BROWSER.get("proc")
    alive = old is not None and old.poll() is None
    if _edge_cdp_ok():
        if alive:
            return                          # 我们拉起的那个还活着：复用
        log("[comfy] 调试端口上已有 Edge 实例（本进程句柄已退出），直接复用")
        COMFY_BROWSER["proc"] = None
        return
    if old is not None:
        # 走到这里 = 端口上没人应答：旧句柄要么已死，要么假死。清干净重来。
        _stop_proc(old)
        COMFY_BROWSER["proc"] = None
    exe = _edge_exe()
    if not exe:
        raise RuntimeError("未找到 Microsoft Edge")
    BROWSER_USER_DIR.mkdir(parents=True, exist_ok=True)
    # P1-1：不再写死 9333 —— 那个端口可能被上次残留 / 别的自动化工具占着，
    # 而旧实现只要「有人应答」就当成自己的 Edge，随后把工作流发给未知目标。
    port = int(os.environ.get(CDP_PORT_ENV) or 0) or _free_port()
    COMFY_BROWSER["port"] = port
    proc = subprocess.Popen([
        exe, f"--remote-debugging-port={port}",
        f"--user-data-dir={str(BROWSER_USER_DIR)}",
        "--no-first-run", "--no-default-browser-check", "--new-window", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    COMFY_BROWSER["proc"] = proc
    deadline = time.time() + max(3.0, float(budget))
    while time.time() < deadline:
        if proc.poll() is not None:
            # spawn 后立刻退出 = 起不来（profile 被锁 / 实例已存在），别再空等
            rc = proc.returncode
            COMFY_BROWSER["proc"] = None
            COMFY_BROWSER["port"] = 0
            raise RuntimeError(f"Edge 进程启动即退出（code={rc}）")
        if _edge_cdp_ok(timeout=1.0):
            return
        time.sleep(0.2)
    _stop_proc(proc)
    COMFY_BROWSER["proc"] = None
    COMFY_BROWSER["port"] = 0
    raise RuntimeError("Edge 调试端口启动超时")


def _open_comfy_tab() -> str:
    """确保调试浏览器里有一个 ComfyUI 标签页，返回其 webSocketDebuggerUrl。"""
    hostport = urllib.parse.urlparse(COMFY_BASE).netloc
    try:
        tabs = _cdp_http("/json")
    except Exception:
        return None
    for t in tabs:
        if t.get("type") == "page" and hostport in (t.get("url") or ""):
            return t.get("webSocketDebuggerUrl")
    for method in ("PUT", "GET"):   # 新版内核要求 PUT，老内核只认 GET
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{_cdp_port()}/json/new?url=" +
                urllib.parse.quote(COMFY_BASE + "/"),
                method=method)
            t = json.loads(_OPENER_NO_PROXY.open(req, timeout=8).read().decode("utf-8"))
            return t.get("webSocketDebuggerUrl")
        except urllib.error.HTTPError as ex:
            if ex.code == 405:
                continue
            return None
        except Exception:
            return None
    return None


def _comfy_reachable(timeout: float = 1.5) -> bool:
    """ComfyUI 是否可达（P2-5）。任何 <500 的 HTTP 响应都算「有服务在听」。

    只发一次 GET /，与 bridge_active() 一样是只读、无副作用。
    """
    try:
        with _OPENER_NO_PROXY.open(COMFY_BASE + "/", timeout=timeout) as r:
            return int(getattr(r, "status", 200)) < 500
    except Exception:
        return False


def open_comfy_page(workflow: dict, name: str = "") -> dict:
    """在独立 Edge 窗口中把工作流加载到 ComfyUI 画布（不执行，用户手动 Queue）。

    `name` 只在 API 图（`{"prompt":{…}}`）时用得上：`loadApiJson(data, filename)` 的第二个
    参数是文件名，**不能省** —— 内部 afterLoadNewGraph → activateLoadedWorkflow 会把
    `undefined` 当工作流对象去取 `.path`，实测抛
    `Cannot read properties of undefined (reading 'path')`。
    """
    # P2-5：先探活再拉浏览器。此前 ComfyUI 在不在都会先把 Edge 拉起来、再导航
    # 到 COMFY_BASE —— 用户先看到一个弹出窗口里的「无法访问此网站」，之后 CMV
    # 才回「无法打开 ComfyUI 页面」。现在不可达直接给明确错误 + 降级提示。
    if not _comfy_reachable():
        return {"ok": False,
                "error": f"ComfyUI 未启动或不可达（{COMFY_BASE}）",
                "hint": "可改用「拉取模式」：先安装桥接扩展，再从已打开的 ComfyUI 页面接收工作流",
                "fallback": "pull"}
    fname = (name or "").strip() or "CMV_API工作流.json"   # loadApiJson 的 filename 参数
    # P2-4：_ensure_edge 原本在 try 之外 —— RuntimeError 直接穿透到 do_POST 的
    # 兜底 except，用户拿到的是 500 + 英文异常串。现在并进同一条错误通道。
    try:
        _ensure_edge()
    except RuntimeError as ex:
        return {"ok": False, "error": f"无法启动调试浏览器：{ex}", "fallback": "pull"}
    try:
        import sys as _sys
        _d = str(Path(__file__).resolve().parent)
        if _d not in _sys.path:
            _sys.path.insert(0, _d)      # python_embeded 隔离模式：_pth 不含脚本目录
        import ws_client
        ws_url = _open_comfy_tab()
        if not ws_url:
            return {"ok": False, "error": f"无法打开 ComfyUI 页面({COMFY_BASE})"}
        ws = ws_client.ws_connect(ws_url, timeout=15)
    except Exception as ex:
        return {"ok": False, "error": f"{type(ex).__name__}: {ex}"}
    old = COMFY_BROWSER.get("ws")
    if old is not None and old is not ws:
        try:
            ws_client.ws_close(old)      # 换掉旧连接前先关，防句柄泄漏
        except Exception:
            pass
    COMFY_BROWSER["ws"] = ws
    try:
        # 确保标签页指向 ComfyUI（/json/new 的 url 参数在部分版本被忽略）
        tgt = ws_client.cdp_call(ws, "Page.navigate",
                                 {"url": COMFY_BASE + "/"}, timeout=30)
        if "error" in tgt:
            return {"ok": False, "error": "导航失败: " + str(tgt["error"])}
        # 等待前端 app 就绪并注入工作流（60s 上限，含页面加载）
        # 三种形态都要认，且**认不出来就明确失败**，绝不把畸形数据喂给 loadGraphData：
        #   · UI 图（有 nodes 数组）                 → loadGraphData（保留原坐标/分组）
        #   · 纯 API 图（顶层全是 class_type/inputs）→ loadApiJson
        #   · API 包装 `{"prompt":{…}}`              → loadApiJson(wf.prompt)
        # ⚠️ ComfyUI 的 isApiJson() 只认「顶层就是节点表」：把 `{"prompt":…}` 交给
        #    loadGraphData 会被判成 UI 图，随后按 nodes 遍历抛
        #    `TypeError: e is not iterable`（弹窗「由于重新加载工作流数据出错」，
        #    报错脚本却是 ComfyUI-Manager —— 它的钩子在遍历这份畸形数据）。2026-11-02 实机踩到。
        expr = ("(async () => {"
                " const isApiObj=(o)=>{if(!o||typeof o!=='object'||Array.isArray(o))return false;"
                " const ks=Object.keys(o); if(!ks.length)return false;"
                " return ks.every(k=>{const n=o[k];"
                " return !!n&&typeof n==='object'&&!Array.isArray(n)"
                " &&typeof n.class_type==='string'&&!!n.inputs&&typeof n.inputs==='object';});};"
                " let wf = " + json.dumps(workflow, ensure_ascii=False) + ";"
                " if (typeof wf === 'string') { try { wf = JSON.parse(wf); } catch (e) { return 'BAD_WF'; } }"
                " for (let i = 0; i < 300; i++) { if (window.app && window.app.loadGraphData) break;"
                " await new Promise(r => setTimeout(r, 200)); }"
                " if (!(window.app && window.app.loadGraphData)) return 'NO_APP';"
                " let api = null;"
                " if (wf && wf.prompt && !wf.nodes) { if (!isApiObj(wf.prompt)) return 'BAD_WF'; api = wf.prompt; }"
                " else if (isApiObj(wf)) { api = wf; }"
                " else if (!(wf && Array.isArray(wf.nodes))) return 'BAD_WF';"
                " if (api) { if (!window.app.loadApiJson) return 'NO_API';"
                "   await window.app.loadApiJson(api, " + json.dumps(fname, ensure_ascii=False) + ");"
                "   return 'LOADED_API'; }"
                " await window.app.loadGraphData(wf); return 'LOADED'; })()")
        msg = ws_client.cdp_eval(ws, expr, timeout=90)
    except Exception as ex:
        COMFY_BROWSER["ws"] = None
        try:
            ws_client.ws_close(ws)       # 注入失败即回收连接
        except Exception:
            pass
        return {"ok": False, "error": f"注入失败: {type(ex).__name__}: {ex}"}
    res = ((msg.get("result") or {}).get("result") or {}).get("value")
    if res in ("LOADED", "LOADED_API"):
        return {"ok": True, "url": COMFY_BASE + "/",
                "via": "api" if res == "LOADED_API" else "ui"}
    if res == "NO_API":
        return {"ok": False,
                "error": "这张图只有 API 工作流，但 ComfyUI 前端没有 loadApiJson（版本过旧），"
                         "可改用「⬇ 工作流」导出 json 手动载入"}
    if res == "BAD_WF":
        return {"ok": False,
                "error": "工作流数据格式无法识别（既不是 UI 图也不是 API 图），已阻止注入，"
                         "避免 ComfyUI 抛 `e is not iterable`"}
    if res == "NO_APP":
        return {"ok": False, "error": "ComfyUI 前端未就绪（无法注入 loadGraphData）"}
    return {"ok": False, "error": "注入异常: " + str(msg)[:160]}


# ------------------------------------------------- 回传·拉取模式（ComfyUI 页面内脚本取件）
#
# 上面 open_comfy_page() 是「推」：自己拉一个带调试端口的 Edge 去注入画布，
# 代价是必然弹出一个独立空 profile 的浏览器窗口。
#
# 这里是「拉」：ComfyUI 页面里跑一个极小的脚本（frontend/bridge/ 下那份），
# 它天然持有 window.app，直接 app.loadGraphData()。于是
#   · 不需要调试端口——浏览器自 136 起已禁止用默认 profile 开 CDP；
#   · 不需要新窗口——用户平时开着的那个 ComfyUI 标签页直接就收到了。
# 本层只做三件事：收件（入队）、发件（GET + CORS）、记账（谁在线）。
# 之所以取件走 GET：do_POST 有 _same_origin() CSRF 校验，跨源必 403。

PULL_QUEUE = []                             # 待取件（内存队列，进程退出即清空）
PULL_LOCK = threading.Lock()
# 取件序号。基数取「启动时刻」而不是 0：页面内脚本拿它给「已载入的件」去重，
# 若每次启动都从 1 开始，重启后新件会与上一进程的旧序号撞号，被误判成「已载入」而丢弃。
PULL_SEQ = [int(time.time())]
PULL_KEEP = 20                              # 无人取件时的队列上限，防无限堆积
# P2-3：不带 port 的取件只警告一次（否则 1.5s 轮询会把日志刷满）
PULL_NOPORT_WARN = [False]
# 两阶段取件（V2.8.4）：件被页面取走后先挂在这里，等页面回执才真正销毁。
# 此前是「取走即出队」——页面一旦载入失败（ComfyUI 前端没起来、工作流损坏），
# 件就凭空消失：用户看到「已回传」却什么都没发生，也无从重试。
# P0-9/P0-10（审计二批次·第 2 级）：**投递代次**。
# 键仍是 seq（一件同时只会挂一次），但记录里多一个 claim —— 它是「这一次投递」的身份。
# 为什么必须有它：同一件被重投时，上一轮的回执（ack/nack/drop）可能**迟到**，
# 而此前 ack 只按 seq 弹记录 ⇒ 迟到的 ack 会把「新一轮投递」的记录抹掉，
# 件就此从 in-flight 消失、队列里也没有 ⇒ 永久丢失（用户看到「已回传」但画布里什么都没有）。
# 现在：回执必须携带 claim 且与当前记录一致才生效；不一致 = 迟到回执 ⇒ 忽略、不弹记录、不销毁。
# 取件方拿不到 claim 时（abort / 老版脚本）传空 —— 服务端回落到「按 seq 匹配」的旧行为，
# 向后兼容不破（老版脚本因此仍能正常工作）。
PULL_INFLIGHT = {}                          # seq -> {"item","t","port","claim"}
PULL_CLAIM = [0]                            # 投递代次计数器（单调递增，只增不减）
BRIDGE_ACK_TTL = 120.0                      # 等回执的上限（秒）；载入大工作流也够用
# ε-7（§6.7 P2-2）：nack 之后的重投**退避**（按「第几次尝试失败」取，秒）。
# 此前 nack 一律把件插回队首 + 前端 1.5s 轮询 ⇒ 同一件 3 次尝试挤在 3 秒内，
# 3 条 error toast 连发，用户来不及读第一条，也来不及去修 ComfyUI（缺节点 / 前端没起）。
# 退避放在**服务端**（给件打 not_before）：前端的 tick 是全页面共享的一个循环，
# 没有「只为某一件延迟」的位置；而队列天然是逐件的。
PULL_BACKOFF = (0.0, 0.0, 4.0, 12.0)        # [tries] → 延迟；tries 从 1 起（0 位留空）
# ε-7（§6.7 P2-2）：**「放弃」账本**。
# 放弃时前端原本发的是 ack —— 而 ack 在服务端语义是「已成功载入」，于是这件事
# 被记成「送达成功」：件销毁、无记录、CMV 侧无从得知，用户以为工作流已经进去了。
# 现在改成 drop：件同样不再重投（语义不变），但**留一条账**，由 /api/bridge/status
# 交给 CMV，让用户下次推送时看到「上次有件被放弃了，可重新发送」。
PULL_DROPPED = []                           # [{"seq","name","reason","t"}]，有界
PULL_DROPPED_KEEP = 20
# P1-32：取件超时（abort）之后的兜底回收窗口（秒）。
# 桥接脚本单次取件超时 = 10s（comfy-cmv-bridge.js 的 PULL_TIMEOUT），abort 后它拿不到 seq
# ⇒ 既不能 ack 也不能 nack，件只能干等 BRIDGE_ACK_TTL（120s），期间页面每轮都取不到件、
# 用户零提示 —— 「CMV 已报成功但工作流永远到不了」里最难自查的那一条。
# 判据：**同一个端口又来取件** ⇒ 页面侧 busy 串行，说明上一次请求已经结束；
# 若它挂起已超过本窗口，就认定它是被 abort 掉的那一件，立刻放回队列。
# 窗口比前端超时略大，是为了容忍「同端口开了两个标签页」这种并发 —— 那时不急着抢。
BRIDGE_CLAIM_GRACE = 12.0
# P0-10（审计二批次·第 2 级）：**载入期心跳**。
# 上面那条兜底的判据是「同一个端口又来取件 + 挂起够久」，但它有个致命前提：
# 「又来取件」就说明「上一次请求已经结束」。这个前提在**载入很慢**时不成立 ——
# 页面取件是串行的（tick 里 busy），可「取到件」之后是**另一条链路**在载入
# （apply/doLoad 不占 busy，最长等 ComfyUI 前端 30s，见 bridge 脚本的 waitApp），
# 这期间取件循环照常跑、照常发 /api/pull。于是：页面正在载入 A 件（已超 12s），
# 取件循环又发来一次空轮询 ⇒ 服务端把 A 认成「被 abort 丢掉的那一件」⇒ 放回队列 ⇒ 重投
# ⇒ **同一个工作流进画布两次**（用户拿到两份）。
# 判据修正：只有在「这一轮页面明确取走了新件」或「取件方回话说明自己不在载入」时才回收。
# 让「页面侧」把这个信息交给服务端的最省事方式 —— 见 bridge_take 的 reclaim 参数。
BRIDGE_SEEN = {}                            # {端口: 最近一次取件时刻}
# 4.3-7（2026-11-05 审计）：页面**明确**报告「正在载入」的端口。
# 桥接脚本取件时带 busy=1（P0-10 加的），此前只用来跳过 abort 兜底回收；
# 但 BRIDGE_ACK_TTL(120s) 的 stale 重投同样会打断大工作流 —— 载入 3 分钟很常见，
# 件被放回队列后页面还会再取一次 ⇒ 同一份画布进两遍。
# 记录「最后一次 busy 上报」的时刻，_requeue_stale 据此跳过正在载入的端口。
BRIDGE_BUSY = {}                            # {端口: 最近一次报 busy 的时刻}
BRIDGE_BUSY_TTL = 90.0                      # busy 挂号时效：页面崩了也会在 90s 后照常回收
# ε-7（§6.7 P2-3）：**标签页维度**的账。
# BRIDGE_SEEN 的 key 是端口 —— 而 ComfyUI 开两个标签页时**端口相同**，
# 于是在服务端看来「只有一个页面」。两件事因此无解：
#   ① CMV 无法知道同一端口上有几个页面（推送后落在哪个标签页，由谁先轮询到决定）；
#   ② 出问题时无从自查（只看得到一个端口）。
# 这里另记一份按**标签页**的账（前端生成的 tabId，存 sessionStorage ⇒ 页面刷新不变、
# 新标签页新号）。⚠️ BRIDGE_SEEN 按端口的语义**保持不变** —— 存活判定（BRIDGE_TTL）
# 与 bridge_take 的单实例兜底都依赖它，本次不动那条路径。
BRIDGE_TABS = {}                            # {tabId: {"port": …, "t": 最近活跃}}
BRIDGE_TAB_KEEP = 24                        # 记账上限，防标签页开开关关把内存撑大
BRIDGE_TTL = 25.0                           # 心跳有效期（秒）：超时视为页面已关闭
# 为什么是 25 秒而不是更短：页面每 1.5 秒取一次件（= 心跳），但
#   · 后台标签页的定时器会被浏览器降频到约 1 次/分钟；
#   · 页面若被某个原生对话框挡住，JS 会整段冻结。
# 窗口开太小会出现「我明明开着 ComfyUI，回传却另开了一个 Edge 窗口」这种误判；
# 开太大则页面关掉后还会在队列里空等一会儿。25 秒是两者的折中。
# 注意这**不是**判断送达的依据：件入队后会一直留着（PULL_KEEP 上限），
# 页面恢复轮询就会取走，所以这里放宽只会让「静默失败」更少见，不会丢件。
#
# 方案③（2026-11-05）：把「取件新鲜度」与「窗口可用性」拆成两个口径。
# 25s 只回答「件现在该不该投递 / 多久算 stale」；回答「要不要另开一个 ComfyUI
# 窗口」必须用下面的 BRIDGE_WIN_TTL —— 后台标签页的轮询本身就会被降到约 1 次/分，
# 拿 25s 判窗口死活必然误杀，结果是画布变成两份。
# 已知并接受的代价：页面**真的**关掉后，10 分钟内推送会入队而不开新窗口；
# 件最多等 BRIDGE_ACK_TTL(120s) 被放回队列，用户看到的是「已发送，稍候」。
BRIDGE_WIN_TTL = 600.0                      # 窗口可用性（秒）：只在决定「另开窗口」时用
BRIDGE_NODE = "ComfyMediaViewer-Bridge"     # 装到 ComfyUI custom_nodes 下的目录名
BRIDGE_INIT = '''# ComfyMediaViewer 桥接（纯前端扩展，不注册任何节点）
#
# 让【已打开】的 ComfyUI 页面直接接收 ComfyMediaViewer 送来的工作流。
# 本文件除声明 WEB_DIRECTORY 外不做任何事，实际逻辑在 web/comfy-cmv-bridge.js。
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
WEB_DIRECTORY = "./web"
__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
'''


def _comfy_port() -> str:
    """本机 ComfyUI 的端口，作为队列分发目标（多实例时按端口送到「对应的页面」）。"""
    netloc = urllib.parse.urlparse(COMFY_BASE).netloc
    return netloc.rsplit(":", 1)[1] if ":" in netloc else "80"


def _mark_retried(it: dict):
    """标记「这件已经被投递过一轮」（P2-1）。供 _trim_queue 优先保留。

    必须持有 PULL_LOCK 时调用（改动的是队列里的同一份 dict）。
    """
    try:
        it["retried"] = int(it.get("retried") or 0) + 1
    except (TypeError, ValueError):
        it["retried"] = 1


def _trim_queue() -> list:
    """队列超限时裁件 —— **唯一裁剪出口**（P2-1）。必须持有 PULL_LOCK 时调用。

    旧实现是 `del PULL_QUEUE[:-PULL_KEEP]`：它把**队首**当「最老的」删掉，而
    nack 重投的件恰恰被插在队首 —— 于是「刚失败、正要重投」的那一件被当成最老的
    静默丢弃：队列里查不到、没有日志、也不进放弃账本，与
    「跳过 ≠ 丢弃，件仍留在队列里」的承诺直接相悖。
    现在按「重投过的优先保留」排裁剪顺序，且每个被裁的件都记进 PULL_DROPPED
    并打日志 —— 裁剪可以丢件，但不能**无声**丢件。
    """
    over = len(PULL_QUEUE) - PULL_KEEP
    if over <= 0:
        return []
    # 先裁没重投过的（索引小的 = 更老的先裁）；重投过的排在后面，尽量留下
    order = [i for i, x in enumerate(PULL_QUEUE) if not x.get("retried")]
    order += [i for i, x in enumerate(PULL_QUEUE) if x.get("retried")]
    got = []
    for i in sorted(order[:over], reverse=True):
        it = PULL_QUEUE.pop(i)
        got.append(it)
        PULL_DROPPED.append({"seq": it.get("seq"), "name": it.get("name") or "",
                             "reason": "queue-overflow", "t": time.time()})
    if got:
        del PULL_DROPPED[:-PULL_DROPPED_KEEP]
        log(f"[bridge] 队列超过 {PULL_KEEP} 件，裁掉最老的 "
            f"{[x.get('seq') for x in got]}（已记入放弃账本）")
        got.sort(key=lambda x: x.get("seq") or 0)
    return got


def bridge_push(path: Path, tpl=None, port="") -> dict:
    """导出工作流并入队，等 ComfyUI 页面内的桥接脚本取走。"""
    r = export_workflow(path, tpl)
    if not r.get("ok"):
        return r
    try:
        wf = json.loads(r["json"])
    except ValueError:
        return {"ok": False, "error": "工作流 JSON 解析失败"}
    if r.get("source") == "png":
        name = path.stem + "_内嵌.json"
    elif r.get("source") == "api":
        # 没有 UI 工作流、只有 API 图：文件名点明形态，用户拖进 ComfyUI 时心里有数。
        # stem 常以 "_" 结尾（ComfyUI_temp_xekeh_00020_）⇒ 先去掉，别生成双下划线。
        name = (path.stem.rstrip("_") or path.stem) + "_API工作流.json"
    else:
        t = str(tpl)
        name = f"{path.stem}_{t[:-5] if t.endswith('.json') else t}.json"
    with PULL_LOCK:
        PULL_SEQ[0] += 1
        item = {"seq": PULL_SEQ[0], "kind": "workflow",
                "target": str(port or _comfy_port()),
                "name": name, "source": path.name, "workflow": wf}
        PULL_QUEUE.append(item)
        _trim_queue()                       # P2-1：唯一裁剪出口（会记账，不静默丢）
        n = len(PULL_QUEUE)
    log(f"[bridge] 入队 #{item['seq']} {name} → 端口 {item['target']}（队列 {n}）")
    res = {"ok": True, "seq": item["seq"], "queued": n,
           "name": name, "target": item["target"]}
    if r.get("warnings"):
        # B2 族：这条链路也要把「有参数没写进去」带回前端，否则只是在 HTTP 导出
        # 那一头有回执、桥接那头没有 —— 同一个工作流换个入口就变成静默。
        res["warnings"] = r["warnings"]
    return res


def _requeue_stale(now: float) -> list:
    """把「已投递但迟迟没有回执」的件放回队列（页面在载入途中被关掉 / 崩了）。

    必须在持有 PULL_LOCK 时调用。放回队首：它们是最老的件，理应优先重投。
    """
    back = []
    for seq, rec in list(PULL_INFLIGHT.items()):
        if now - rec["t"] > BRIDGE_ACK_TTL:
            _port = str(rec.get("port") or "")
            # 4.3-7（2026-11-05 审计）：页面还在报 busy = 正在载入大工作流。
            # 此时按 120s 一刀切会把件抢回来重投，页面随后再取一次 ⇒ 画布进两遍。
            # 页面若真的崩了，busy 不再更新，过 BRIDGE_BUSY_TTL 仍会照常回收。
            if _port and now - BRIDGE_BUSY.get(_port, 0.0) <= BRIDGE_BUSY_TTL:
                continue
            _it = PULL_INFLIGHT.pop(seq)["item"]
            _it.pop("not_before", None)   # ε-7：兜底放回的件应**立刻**可重投，留着退避会白等
            back.append(_it)
    if back:
        back.sort(key=lambda x: x.get("seq") or 0)
        for it in reversed(back):
            _mark_retried(it)               # P2-1：重投过的件裁剪时优先保留
            PULL_QUEUE.insert(0, it)
        _trim_queue()
        log(f"[bridge] 超过 {BRIDGE_ACK_TTL:.0f}s 无回执，放回队列 "
            f"{[x.get('seq') for x in back]}")
    return back


def _reclaim_abandoned(want: str, now: float) -> list:
    """P1-32 兜底回收：把「挂在同一个端口上过久」的件放回队列。

    与 bridge_release 的关系：那条是前端**明确告知**放弃；这条是前端没来得及说话时的兜底
    （它未必需要——前端 abort 后会立刻发 release，但 release 本身也可能发不出去，
    因为 CMV 正是卡住了才会让取件超时）。

    必须持有 PULL_LOCK 时调用。放回队首：它们是最老的件，理应优先重投。
    """
    if not want:
        # P2-3：不带 port 的取件（老脚本 / 手工请求）把件记成 port=""，这类
        # 「无主件」没有任何端口能认领它。这里**刻意不跑 12s 激进判定** ——
        # 无 port 的页面同时也带不了 busy 参数，服务端分不清它是「abort 了」
        # 还是「正在载入」，抢走就会重复载入。精确回收改由
        # bridge_release("")（页面主动放弃）负责，没说话的那部分由
        # _requeue_stale 的 BRIDGE_ACK_TTL 兜底。
        return []
    back = []
    for seq, rec in list(PULL_INFLIGHT.items()):
        if str(rec.get("port") or "") == want and now - rec["t"] > BRIDGE_CLAIM_GRACE:
            _it = PULL_INFLIGHT.pop(seq)["item"]
            _it.pop("not_before", None)   # ε-7：同上（abort 兜底回收）
            back.append(_it)
    if back:
        back.sort(key=lambda x: x.get("seq") or 0)
        for it in reversed(back):
            _mark_retried(it)
            PULL_QUEUE.insert(0, it)
        _trim_queue()
        log(f"[bridge] 端口 {want} 的取件挂起超过 {BRIDGE_CLAIM_GRACE:.0f}s（疑似 abort），"
            f"放回队列 {[x.get('seq') for x in back]}")
    return back


def _pull_ready(x, now: float) -> bool:
    """件是否**已到可重投时刻**（ε-7 退避，见 PULL_BACKOFF）。

    ⚠️ 未到时刻的件**仍留在队列里**，只是这一轮不摘走 —— 它既不是「丢弃」，
    也不是「出队失败」。必须持有 PULL_LOCK 时调用（只读，但读的是共享结构）。
    没有 not_before 字段（首次入队 / 超时兜底放回 / 主动放弃后收回）⇒ 恒为可取。
    """
    nb = x.get("not_before")
    return (nb is None) or (now >= nb)


def bridge_take(want: str, reclaim: bool = True):
    """取一件发给 want 端口的工作流，并挂起等回执（见 bridge_ack / bridge_nack）。

    优先级：端口严格匹配 > 未指定目标的件 > 单实例兜底（只有一个活着的桥接页面时才允许取任意件）。
    此前 want 为空时会命中任意端口的件，多页面同时在线时 A 页面可能取走发给 B 页面的工作流。

    reclaim（P0-10）：要不要跑「abort 兜底回收」。页面侧**正在载入**时不能跑 ——
    那时它取不到件，却又符合「同端口又来人 + 挂起够久」的旧判据，会把正在载入的件抢走重投。
    老版脚本不带这个参数 ⇒ 保持 True（行为与改动前一致）。
    """
    now = time.time()
    with PULL_LOCK:
        _requeue_stale(now)
        if reclaim:
            _reclaim_abandoned(want, now)      # P1-32：前端 abort 后的兜底回收
        it = None
        if want:
            for i, x in enumerate(PULL_QUEUE):
                # ε-7：退避期内的件跳过（continue 而非 break —— 后面可能还有别的件可取）
                if str(x.get("target") or "") == want and _pull_ready(x, now):
                    it = PULL_QUEUE.pop(i)
                    break
        if it is None:
            for i, x in enumerate(PULL_QUEUE):     # 入队时没指定目标的件：任何页面都能取
                if not str(x.get("target") or "") and _pull_ready(x, now):
                    it = PULL_QUEUE.pop(i)
                    break
        if it is None and not want and PULL_QUEUE:  # 单实例宽松兜底（不重入 PULL_LOCK，直接读 BRIDGE_SEEN）
            live = [p for p, t in BRIDGE_SEEN.items() if now - t <= BRIDGE_TTL]
            if len(live) <= 1:
                for i, x in enumerate(PULL_QUEUE):
                    if _pull_ready(x, now):
                        it = PULL_QUEUE.pop(i)
                        break
        if it is not None:
            # 挂起：只有页面回执 ack 才销毁；nack 或超时都会让它回到队列。
            # claim 是「这一次投递」的身份（见 PULL_INFLIGHT 的说明），随件交给页面、
            # 回执时带回来核对 —— 迟到的旧回执因此不会再抹掉新一轮投递。
            PULL_CLAIM[0] += 1
            claim = PULL_CLAIM[0]
            PULL_INFLIGHT[it["seq"]] = {"item": it, "t": now, "port": want or "",
                                        "claim": claim}
            it["claim"] = claim
        return it


def _claim_ok(rec, claim) -> bool:
    """这次回执是不是**当前这一轮投递**的回执（P0-9）。

    False = 迟到回执（页面超时重试后，上一轮的 ack/nack 才姗姗来迟）⇒ 必须忽略：
    它若被当成本轮的回执，会把新一轮的 in-flight 记录弹掉 ⇒ 件永久丢失
    （队列里没有、in-flight 里也没有，而用户已经看到「已回传」）。
    取件方拿不到 claim 时传空（abort 拿不到 seq 的自然延伸 / 老版脚本不带）⇒
    回落到「只按 seq 匹配」的旧行为，不破坏兼容。
    """
    if rec is None:
        return False
    want = str(claim or "").strip()
    if not want:
        return True
    return str(rec.get("claim") or "") == want


def bridge_ack(seq, claim="") -> dict:
    """页面回执：这件已经成功载入，可以销毁了。

    claim（P0-9）：这一次投递的身份。与当前 in-flight 记录不符 = 迟到回执
    ⇒ 忽略（不弹记录、不销毁）。改前只按 seq 弹 ⇒ 迟到 ack 会把新一轮投递的记录抹掉，
    件从 in-flight 消失、队列里也没有 ⇒ 永久丢失。
    """
    try:
        s = int(seq)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad seq"}
    rec = None
    stale = False
    with PULL_LOCK:
        cur = PULL_INFLIGHT.get(s)
        if _claim_ok(cur, claim):
            rec = PULL_INFLIGHT.pop(s, None)
        elif cur is not None:
            stale = True
    if rec:
        log(f"[bridge] 已确认送达 #{s}")
    elif stale:
        log(f"[bridge] 忽略迟到的 ack #{s}（不是当前这一轮投递）")
    else:
        # P2-2：这个 seq 压根不在 in-flight —— 已 ack 过、nack 回队了、或从没取过。
        # 此前一律回 ok:true，前端无法区分「服务端处理了」与「白跑一趟」。
        log(f"[bridge] 回执无效：ack #{s} 不在 in-flight")
        return {"ok": False, "error": "not inflight", "seq": s, "dropped": False}
    # 4.3-8（2026-11-05 审计）：迟到 ack 仍回 ok（语义是「服务端收到了、别再重试」），
    # 但必须带 stale 标记 —— 否则调用方分不清「这一轮真的确认了」与「这是上一轮的
    # 迟到回执」。可观测性缺口，不影响数据。
    return {"ok": True, "seq": s, "dropped": bool(rec), "stale": stale}


def bridge_nack(seq, tries=0, claim="") -> dict:
    """页面回执：这件载入失败，放回队列等重投（带退避，见 PULL_BACKOFF）。

    tries = 前端报来的「这是第几次尝试失败」（从 1 起）。ε-7 之前不带这个参数，
    件一律立刻插回队首 ⇒ 3 次尝试挤在 3 秒内砸完。现在按次数给件打 not_before，
    bridge_take 会跳过还没到时刻的件（跳过 ≠ 丢弃，件仍留在队列里）。
    claim（P0-9）：同 bridge_ack —— 迟到回执必须忽略，否则会把新一轮投递的件
    又插回队列一次（同一个工作流进画布两遍）。
    """
    try:
        s = int(seq)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad seq"}
    try:
        n = int(tries) if tries else 0
    except (TypeError, ValueError):
        n = 0
    if n < 0:
        n = 0
    delay = PULL_BACKOFF[min(n, len(PULL_BACKOFF) - 1)]
    rec = None
    stale = False
    with PULL_LOCK:
        cur = PULL_INFLIGHT.get(s)
        if _claim_ok(cur, claim):
            rec = PULL_INFLIGHT.pop(s, None)
            if rec:
                rec["item"].pop("not_before", None)
                if delay > 0:
                    rec["item"]["not_before"] = time.time() + delay
                _mark_retried(rec["item"])     # P2-1：重投件裁剪时优先保留
                PULL_QUEUE.insert(0, rec["item"])
                _trim_queue()
        elif cur is not None:
            stale = True
    if rec:
        log(f"[bridge] 载入失败，放回队列重投 #{s}"
            + (f"（第 {n} 次失败，退避 {delay:.0f}s）" if delay > 0 else ""))
    elif stale:
        log(f"[bridge] 忽略迟到的 nack #{s}（不是当前这一轮投递）")
    else:
        # P2-2：不在 in-flight ⇒ 这件已经不在「等回执」状态，回队会是凭空多一份
        log(f"[bridge] 回执无效：nack #{s} 不在 in-flight")
        return {"ok": False, "error": "not inflight", "seq": s,
                "requeued": False, "backoff": delay}
    return {"ok": True, "seq": s, "requeued": bool(rec), "backoff": delay}


def bridge_drop(seq, reason="", name="", claim="") -> dict:
    """页面回执：**已放弃**这件（连试到上限仍失败），不再要它了 —— 但请留档。

    ε-7（§6.7 P2-2）：此前这种情况前端发的是 ack，而 ack 的语义是「已成功载入」，
    于是「放弃」被系统记成「送达成功」：件销毁、无记录、CMV 侧无从得知。
    现在件不再重投（与 ack 一样终结该件），但会进 PULL_DROPPED 账本，由
    /api/bridge/status 交给 CMV ⇒ 用户**能知道**有件没进去，也就能重新发送。

    与 ack 的区别只在「留不留账」——刻意保留 ack 不动：它仍是「成功」的唯一语义。
    claim（P0-9）：迟到回执不得留下「已放弃」的假账（那会让用户以为件没进去，
    而实际上新一轮投递已经成功载入了）。
    """
    try:
        s = int(seq)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad seq"}
    rec = None
    stale = False
    nm = name or ""
    with PULL_LOCK:
        cur = PULL_INFLIGHT.get(s)
        if _claim_ok(cur, claim):
            rec = PULL_INFLIGHT.pop(s, None)
        elif cur is not None:
            stale = True
        if rec is not None:
            # P2-2：只有真的从 in-flight 摘下记录才留账。此前 rec 为 None 也照写，
            # 于是「手工请求一个不存在的 seq」会在 /api/bridge/status 里造出一条
            # 并不存在的「被放弃」记录，污染排查。
            nm = name or (rec["item"].get("name") if rec else "") or ""
            PULL_DROPPED.append({"seq": s, "name": nm,
                                 "reason": (reason or "max-tries")[:120],
                                 "t": time.time()})
            del PULL_DROPPED[:-PULL_DROPPED_KEEP]
    if stale:
        log(f"[bridge] 忽略迟到的 drop #{s}（不是当前这一轮投递，不留账）")
        return {"ok": True, "seq": s, "dropped": False, "name": name or ""}
    if rec is None:
        log(f"[bridge] 回执无效：drop #{s} 不在 in-flight（不留账）")
        return {"ok": False, "error": "not inflight", "seq": s,
                "dropped": False, "name": name or ""}
    log(f"[bridge] 已放弃 #{s} {nm}（{reason or 'max-tries'}）—— 记入放弃账本")
    return {"ok": True, "seq": s, "dropped": bool(rec), "name": nm}


def bridge_dropped(limit=PULL_DROPPED_KEEP) -> list:
    """最近的「放弃」记录（新的在后），供前端提示用户「有件没进去」。"""
    now = time.time()
    with PULL_LOCK:
        out = list(PULL_DROPPED[-limit:])
    return [{"seq": d["seq"], "name": d["name"], "reason": d["reason"],
             "ago": round(max(0.0, now - d["t"]), 1)} for d in out]


def bridge_tabs() -> list:
    """最近 BRIDGE_TTL 秒内活跃的**标签页**（ε-7 / §6.7 P2-3）。

    与 bridge_pages() 的区别：那个按端口（用于「有没有活页面」的判定），
    这个按标签页（用于「同一端口上有几个页面」）。同一端口的两个标签页在
    bridge_pages() 里只有一项，在这里是两项。
    """
    now = time.time()
    with PULL_LOCK:
        return [{"tab": t, "port": r.get("port") or "",
                 "ago": round(max(0.0, now - r.get("t", 0.0)), 1)}
                for t, r in sorted(BRIDGE_TABS.items()) if now - r.get("t", 0.0) <= BRIDGE_TTL]


def bridge_release(want: str) -> dict:
    """页面**主动放弃**：把挂在这个端口上的件全部放回队列。

    P1-32：桥接脚本单次取件有 10s 超时，超时会 abort —— 那一刻它还没拿到响应体，
    也就**拿不到 seq**，既不能 ack 也不能 nack。这是这套两阶段取件的唯一缺口：
    缺的不是「记得回执」，而是「回执需要的信息在请求失败时必然没有」。
    所以改成按端口放弃：件挂在谁名下，谁的页面就能一句话收回。

    P2-3：want 为空（不带 port 的取件 / 老脚本）时，旧实现直接 `{"error":
    "no port"}` —— 而这类件恰恰被记成 port=""，于是**没有任何 release 能
    认领它**：前端 abort 后发的 releaseClaim 打在这条路径上完全失效，件只能
    干等 BRIDGE_ACK_TTL(120s)，用户看到「推送成功但几十秒没反应」。
    现在空端口 = 「无主件」通道，把 port 为空的件一并放回队列。
    """
    back = []
    with PULL_LOCK:
        for seq, rec in list(PULL_INFLIGHT.items()):
            if str(rec.get("port") or "") == want:
                _it = PULL_INFLIGHT.pop(seq)["item"]
                _it.pop("not_before", None)   # ε-7：页面主动放弃取件 ⇒ 立刻回队列
                back.append(_it)
        if back:
            back.sort(key=lambda x: x.get("seq") or 0)
            for it in reversed(back):
                _mark_retried(it)
                PULL_QUEUE.insert(0, it)
            _trim_queue()
            log(f"[bridge] 页面 {want or '(未报端口)'} 主动放弃取件，"
                f"放回队列 {[x.get('seq') for x in back]}")
    return {"ok": True, "port": want, "released": len(back),
            "seqs": [x.get("seq") for x in back]}


def bridge_touch(port: str, tab: str = ""):
    """记录一次页面心跳（页面每次取件都算），用于前端判断「有没有活着的桥接页面」。

    ε-7：额外按 tabId 记一份（见 BRIDGE_TABS）。tab 为空时只更新端口账 —— 老版本
    脚本不带 tab，行为与改动前完全一致（向后兼容）。
    """
    now = time.time()
    with PULL_LOCK:
        BRIDGE_SEEN[port] = now
        if tab:
            BRIDGE_TABS[tab] = {"port": port, "t": now}
            if len(BRIDGE_TABS) > BRIDGE_TAB_KEEP:
                # 按最近活跃时刻裁掉最老的，而不是按插入顺序（开开关关也不失控）
                old = sorted(BRIDGE_TABS.items(), key=lambda kv: kv[1].get("t", 0.0))
                for k, _r in old[:len(BRIDGE_TABS) - BRIDGE_TAB_KEEP]:
                    BRIDGE_TABS.pop(k, None)


def bridge_busy(port: str):
    """页面报告「正在载入」（取件带 busy=1）—— 给 _requeue_stale 一个不抢件的依据。

    4.3-7（2026-11-05 审计）：原先 busy 只影响 abort 兜底回收，120s 的 stale 重投
    照旧执行 ⇒ 载入大工作流超过两分钟就会被重投，同一份画布进两遍。
    """
    if not port:
        return
    now = time.time()
    with PULL_LOCK:
        BRIDGE_BUSY[port] = now
        if len(BRIDGE_BUSY) > 64:          # 端口数量有限，但换端口会累积
            old = sorted(BRIDGE_BUSY.items(), key=lambda kv: kv[1])[:len(BRIDGE_BUSY) - 64]
            for k, _t in old:
                BRIDGE_BUSY.pop(k, None)


def bridge_pages(ttl: float = BRIDGE_TTL) -> list:
    """最近 ttl 秒内活跃的桥接页面端口。

    两个口径别混用（方案③）：
      · ttl=BRIDGE_TTL(25s)    —— 取件新鲜度：件该不该马上投递、多久算 stale；
      · ttl=BRIDGE_WIN_TTL(600s) —— 窗口可用性：要不要另开 ComfyUI 窗口（bridge_windows）。
    老调用点不传参，语义与改动前完全一致。
    """
    now = time.time()
    with PULL_LOCK:
        return sorted(p for p, t in BRIDGE_SEEN.items() if now - t <= ttl)


def bridge_windows() -> list:
    """最近 BRIDGE_WIN_TTL 秒内活跃的桥接页面端口 —— **只**用于「要不要另开窗口」。

    与 bridge_pages() 的差别就是时间窗：后台标签页 1 次/分的轮询在 25s 口径下会被
    判成「页面已关闭」，导致 CMV 另开窗口、同一份画布出现两次。放宽到 10 分钟
    能覆盖浏览器的后台节流档位。
    """
    return bridge_pages(BRIDGE_WIN_TTL)


def bridge_script_src() -> Path:
    """桥接脚本源文件（随前端静态资源一起分发，打包后位于 _MEIPASS）。"""
    return app_dir() / "frontend" / "bridge" / "comfy-cmv-bridge.js"


def bridge_installed():
    """已安装到 ComfyUI 时返回目标路径，否则 None。"""
    root = comfy_root()
    if not root:
        return None
    dst = root / "custom_nodes" / BRIDGE_NODE / "web" / bridge_script_src().name
    return str(dst) if dst.is_file() else None


def bridge_stale() -> bool:
    """已装的桥接脚本与当前版本内容不一致 = 有更新没重装。

    为什么必须检测：ComfyUI 只在启动时扫描一次 custom_nodes，装过的脚本不会自动跟随
    ComfyMediaViewer 升级。改了桥接行为却让用户继续跑旧脚本，等于修复没送达。
    """
    dst = bridge_installed()
    if not dst:
        return False
    try:
        src = bridge_script_src()
        return (not src.is_file()) or (Path(dst).read_bytes() != src.read_bytes())
    except OSError:
        return False


def bridge_active():
    """ComfyUI 是否**已经加载**了桥接扩展（= 页面打开就会执行它）。

    判据是 ComfyUI 启动时生成的 /extensions 清单。扩展写进 custom_nodes 后
    **必须重启 ComfyUI** 才会进这份清单，因此三种状态要分清：

        True  —— 已生效：用户页面一打开/刷新就会上报心跳
        False —— 已写盘但没重启，页面根本拿不到脚本（最容易误解的状态）
        None  —— ComfyUI 不可达，无法判定

    这里只发只读的 GET /extensions。注意：**绝不能去碰带副作用的端点**
    （ComfyUI-Manager 的 /api/manager/reboot 就是 GET 即生效的，探一下
    就会把用户的 ComfyUI 关掉）。
    """
    if bridge_installed() is None:          # 没装 → 谈不上“已加载”，也不必打扰 ComfyUI
        return False
    try:
        with _OPENER_NO_PROXY.open(COMFY_BASE + "/extensions", timeout=1.5) as r:
            names = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(names, list):
        return None
    tag = "/extensions/" + BRIDGE_NODE + "/"
    return any(tag.lower() in str(n).lower() for n in names)


def bridge_install() -> dict:
    """把桥接脚本装进 ComfyUI 的 custom_nodes（重启 ComfyUI 后自动加载）。"""
    root = comfy_root()
    if not root:
        return {"ok": False,
                "error": "未找到 ComfyUI 安装目录（可设 COMFYUI_ROOT 环境变量指定）"}
    src = bridge_script_src()
    if not src.is_file():
        return {"ok": False, "error": "桥接脚本资源缺失: " + str(src)}
    pkg = root / "custom_nodes" / BRIDGE_NODE
    try:
        (pkg / "web").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(src), str(pkg / "web" / src.name))
        (pkg / "__init__.py").write_text(BRIDGE_INIT, encoding="utf-8")
    except OSError as ex:
        return {"ok": False, "error": f"写入 ComfyUI 目录失败: {ex}"}
    log(f"[bridge] 已安装桥接脚本 → {pkg}")
    return {"ok": True, "path": str(pkg),
            "url": f"/extensions/{BRIDGE_NODE}/{src.name}"}


def app_dir() -> Path:
    """源码运行 = 脚本目录；PyInstaller 运行 = 解包资源目录(_MEIPASS)。"""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).parent


STATIC_TYPES = {".css": "text/css; charset=utf-8",
                ".js": "text/javascript; charset=utf-8",
                ".svg": "image/svg+xml", ".png": "image/png",
                ".ico": "image/x-icon", ".woff2": "font/woff2",
                ".map": "application/json; charset=utf-8"}


def page_file() -> Path:
    """页面入口：模块化后是 frontend/index.html；保留旧的根目录 index.html 兜底。"""
    p = app_dir() / "frontend" / "index.html"
    return p if p.is_file() else (app_dir() / "index.html")


def frontend_file(rel: str):
    """把 /css/x.css、/js/x.js 映射到 app_dir()/frontend/ 下的文件；越界或不存在返回 None。

    /bridge/ 是回传·拉取模式的桥接脚本（给 ComfyUI 页面取用，不在本页面加载）。
    """
    rel = urllib.parse.unquote(rel).lstrip("/")
    if not re.fullmatch(r"(?:css|js|assets|bridge)/[A-Za-z0-9_.\-]+", rel):
        return None
    base = (app_dir() / "frontend").resolve()
    try:
        p = (base / rel).resolve()
    except OSError:
        return None
    if p != base and base not in p.parents:          # 防目录穿越
        return None
    return p if p.is_file() else None


_THUMB_WARNED = set()          # §6.1：同一张图只记一次，滚屏/批量时别把日志刷满


def thumb_for(p: Path, size: int = 320):
    """生成/命中磁盘缓存缩略图，失败返回 None（前端回退到原图）。"""
    if not HAS_PIL or p.suffix.lower() not in IMAGE_EXTS:
        return None
    try:
        key = hashlib.sha1(f"{p}|{p.stat().st_mtime_ns}|{size}".encode("utf-8")).hexdigest()[:20] + ".jpg"
        out = THUMB_DIR / key
        if out.exists():
            return out
        from PIL import ImageOps
        with Image.open(p) as im:
            # 2026-11-05：JPEG 走 DCT 缩解码。1024x1536 缩到 320 时只需解 1/4 像素，
            # 实测单张 19.5ms → 12.4ms（1.6x）；首屏 120 张是启动后最大的等待。
            # 必须在 exif_transpose **之前**：它一 load 图像就再没有降采样机会了。
            # draft 只会选「结果仍不小于目标」的比例，不会把缩略图做糊。
            if im.format == "JPEG":
                try:
                    im.draft("RGB", (size, size))
                except Exception:
                    pass
            im = ImageOps.exif_transpose(im)   # 按 EXIF 方向转正，竖拍缩略图不再躺倒
            # 2026-10-22：先缩再转 RGB。原先对 4K 原图整幅 convert("RGB")，
            # 实测 RGBA 一档白花 8ms（RGB 图是 no-op）；缩到 320 后再转，成本≈0。
            # 2026-11-05：reducing_gap=2.0 让大图走「先 reduce 再 resize」两段缩放，
            # PNG/WEBP 实测快 5~13%（这两种格式没有 draft 可用）。
            try:
                im.thumbnail((size, size), reducing_gap=2.0)
            except TypeError:                    # 老 Pillow 不认这个参数
                im.thumbnail((size, size))
            if im.mode != "RGB":
                im = im.convert("RGB")         # RGBA/LA/P 统一转 RGB：JPEG 不支持 alpha
            THUMB_DIR.mkdir(parents=True, exist_ok=True)
            tmp = out.with_suffix(f".{_tmp_tag()}.tmp")
            im.save(tmp, "JPEG", quality=82)
            tmp.replace(out)
        return out
    except Exception as ex:
        # §6.1：以前这里是「吞掉一切、零痕迹」—— 缩略图**生成失败**与**本来就没有缩略图**
        # 在日志、界面、API 三处都不可区分，用户只看到「这张图显示得慢/是糊的」。
        # 返回值语义保持 None（前端照旧回退原图），但必须留痕，且按名字限流一次。
        key = "%s@%d" % (p.name, size)
        if key not in _THUMB_WARNED and len(_THUMB_WARNED) < 500:
            _THUMB_WARNED.add(key)
            log(f"[warn] 缩略图生成失败 {p.name}: {type(ex).__name__}: {ex}")
        return None


# ---------------------------------------------------------------- prompt index

IDX_FILE = DATA_DIR / "prompt_index.json"
IDX_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")   # 参与提示词索引的格式
PARSER_VER = 22         # 解析逻辑变更时 +1，使旧缓存整体失效重建
                        # 22（2026-11-15）：显示类节点（TE_text_display）不再用 API 里的
                        #   `inputs.text`（那是**上一次**的显示串，带 POSITIVE/NEGATIVE/
                        #   SETTINGS 模板），改为优先取 workflow properties 里的本次副本。
                        #   索引里存的 `pos` 是旧口径 ⇒ 不升版则已索引的老图仍显示
                        #   「POSITIVE: …」+ 上一张图的正文。
                        # 21（2026-11-11）：原生 CLIPTextEncode 里的 <lora:…> 标签不再算 LoRA
                        #   （ComfyUI 核心编码器不解析 lora 语法，那串只是文字）。索引里存的
                        #   `loras` / `pos` 都是旧口径 ⇒ 不升版的话，老图在搜索与列表里永远
                        #   带着那批幻觉条目（全库 540 张 / 1228 条）。
                        # 20（2026-11-09）：合入第三方包后仍为本工程口径（LAN 白名单 / 启动日志），
                        #   解析结果无变化。
                        # 19（2026-11-05）：提示词纯净度 —— 反推模板不再进正向、同一段正文去重；
                        #   索引里存的 e["pos"] 是旧逻辑的脏文本（模板词会被提示词搜索命中），
                        #   不升版的话已索引的老图永远吃不到修复结果。
# 13（2026-11-02）：A1111 的 <lora:...> 标签与 Lora hashes 接入 loras（旧版恒为空）；
#                   ANIMA 的 AnimaArtistPack.base_prompt 接入正向正文（旧版正向整段消失）。
#                   不升版的话老图仍走索引里的旧解析结果，等于没修。
# 14（2026-11-02）：WeiLinPromptUI 的 <wlr:名字:模型强度:文本编码强度:触发词强度> 也算 LoRA，
#                   并从正向正文里剥掉（旧版把整排标签原样留在提示词开头、LoRA 列表里没有）。
#                   索引里存着 pos ⇒ 不升版的话，老图的搜索正文永远带着标签。
# 15（2026-11-03）：WeiLinPromptUI 的另一种形态 —— LoRA 存在 `lora_str` 字段（整张槽位表
#                   json.dumps），节点不在模型链上 ⇒ 旧版 loras 恒为空。示意：F1317CF4….png，
#                   10 条 LoRA 一条都不显示；不升版则这张图永远是旧结果。
# 16（2026-11-03）：LoRA 槽位被摊平成点号输入（`loras.0.lora_name` / `.strength` / `.enabled`）
#                   —— 动态输入节点（示意：Krea2-2ST-….png 的 LoadLoraModel）既不写数组也不写
#                   `lora_name`，三条常规分支全落空；且该节点挂在第二段 KSamplerAdvanced 的链上，
#                   主采样链 visit_m 到不了 ⇒ 必须全图扫。不升版则这张图仍是 0 条。
# 17（2026-11-03）：多段采样流新增 `models = [{name, stage}]`（每段真正作用的模型）。
#                   旧版只记第一段；不升版则老图永远只显示一个模型（前端新逻辑无从显示）。
#                   `model` 字段语义不变，索引里多存的是列表，不影响既有统计口径。
# 18（2026-11-03）：`models` 每条补上该段自己的 `sampler/scheduler/steps/cfg/seed` 与
#                   `w/h`（该段采样时的 latent 尺寸）。旧版这四项只在顶层存**主采样器**那一份，
#                   两段用不同采样器/尺寸时界面上是错的信息（照着复现必然出不了同一张图）。
#                   不升版则已索引的老图永远是「一个采样器打天下」。
                        # 10：提示词不再混入 <lora:...>；不跨正负侧；ZeroOut 侧为空；新增工作流类型 wfk
                        # 12（2026-10-29）：LoRA 槽位表被 JSON.stringify 进字符串（VisualLoRALoader
                        #    的 lora_data）也认得出来 —— 不升版的话，已索引过的老图永远是空 loras
                        #    （索引按 pver 命中缓存，只有「重建索引」才能纠正）。
REINDEX_INTERVAL = 30   # 索引完成后距下次允许增量刷新的最小秒数（出图后 30s 内可搜到新图）
# P0-1：单次索引扫描允许的目录读取失败上限。超过即认定「盘读不到」而不是「图没了」，
# 丢弃本代扫描结果、保留旧索引。30 是个宽松值：正常库偶尔一两个子目录被独占不算事，
# 而「外接盘掉线」会让**所有**子目录一起失败，远超此数。
INDEX_SCAN_FAIL_MAX = 30
IDX = {"root": None, "map": {}, "done": 0, "total": 0, "building": False,
       "built_at": 0.0, "stale": False, "stale_reason": ""}
IDX_LOCK = threading.Lock()
IDX_THREAD = [None]
# P1-15：索引「代际」令牌。force_reindex 会把 IDX_THREAD[0] 置 None 好让
# ensure_index 起新线程，但**旧线程并不会因此停下** —— 于是两个 _index_builder
# 同时在跑，各自把 IDX["map"] 整体替换，后收工的那个用更旧的扫描结果盖掉新的，
# 「重建」看起来点了没反应（索引停在旧结果）。做法：每次重建/换根都给 gen+1，
# 线程只在「自己这一代仍是最新」时才允许写回 IDX / 落盘，否则静默丢弃结果。
IDX_GEN = [0]


def _wf_class(meta: dict, parsed: dict) -> str:
    """工作流分类：full=有完整 UI 工作流 / prompt=只有提示词参数 / none=无生成参数。"""
    raw = meta.get("workflow")
    if isinstance(raw, str) and raw.strip():
        return "full"
    return "prompt" if parsed else "none"


def _hay_of(pos: str, neg: str, model: str, loras: str) -> str:
    """搜索用的大字符串：四段提示词字段按固定顺序拼接（**保留原始大小写**）。

    抽成函数是为了让「构造条目时预算小写版」与「搜索时兜底重算」用的是
    同一份拼法 —— 两处各写一遍，迟早会有一处顺序不一致，搜索结果就会
    与索引内容对不上，而且极难发现。
    """
    return "\n".join((pos, neg, model, loras))


def _index_entry(p: Path, st: os.stat_result) -> dict:
    """解析单个图片的提示词摘要，用于搜索。"""
    if p.suffix.lower() == ".png":
        meta = png_meta(p)
    elif HAS_PIL:
        meta = parse_jpeg_meta(p)
    else:
        meta = {}
    parsed = parse_meta(meta) or {}

    def _s(v):                      # 索引字段统一为字符串，防异常值污染搜索与统计聚合
        return v if isinstance(v, str) else ""

    _pos, _neg = _s(parsed.get("positive")), _s(parsed.get("negative"))
    _model = _s(parsed.get("model"))
    _loras = "; ".join(str(x) for x in (parsed.get("loras") or []))

    return {
        # B4-P2-1（2026-10-19）：`mtime` 仍是秒级（前端/统计在用），另存一份
        # **纳秒**戳专供增量判重。秒级会漏检两类真实改写：① 同一秒内同尺寸重写；
        # ② `copy2` / `os.utime` 还原 mtime 的覆盖导入。两者都会让这张图在索引里
        # **永久**保留旧提示词，只有「重建索引」才能纠正。
        "mtime": int(st.st_mtime), "mtime_ns": st.st_mtime_ns, "size": st.st_size,
        "pos": _pos,
        "neg": _neg,
        "model": _model,
        "loras": _loras,
        "sampler": _s(parsed.get("sampler")),
        "dim": _s((meta.get("IHDR") or "").replace(" ", "")),
        "wfc": _wf_class(meta, parsed),     # 工作流分类（full/prompt/none）
        "wfk": _s(parsed.get("wfk")),       # 工作流类型（t2i/i2i/cn）
        # P3（2026-11-15）：**派生字段，不落盘**（见 _strip_derived）。
        # 搜索每次请求都要对全库做「拼 4 字段 + lower」；11942 条实测 56 ms，
        # 而真正干活的部分不到 10 ms —— 大头是给几千条根本不会命中的图反复
        # 复制整段提示词。这里在解析时（每条只发生一次）就算好，搜索直接读。
        "_low": _hay_of(_pos, _neg, _model, _loras).lower(),
    }


def _idx_entry_ok(v) -> bool:
    """索引缓存条目的有效性判定（P1，2026-10-09）。

    必须是 dict，且搜索/统计消费的必填键都在、类型对得上：`search_index`
    拼 hay 时直接取 `e["pos"]`，`stats_view` 取 `e.get("wfc")`，坏条目会让
    **每秒轮询**的 `/api/stats` 持续 500。缺一条即判无效 —— 丢掉的条目重扫
    会补回，留着的坏条目会经 old → newmap → 落盘**永久留档**。
    """
    if not isinstance(v, dict):
        return False
    for k in ("pos", "neg", "model", "loras"):
        if not isinstance(v.get(k), str):
            return False
    return isinstance(v.get("mtime"), int) and isinstance(v.get("size"), int)


def _load_idx_map(raw_map) -> dict:
    """索引缓存装载口：过滤坏条目 + 补齐派生字段（P3 起包括 `_low`）。

    P1（2026-10-09）：缓存是「用户能改的文件」，唯一的装载口必须过滤。一条非 dict /
    缺键的条目一旦进了 old → newmap → 又被写回磁盘，就**永久留档**：搜索侧
    `e["pos"]` 抛 KeyError/TypeError，统计侧 `e.get("wfc")` 对 str 抛
    AttributeError ⇒ 前端每秒轮询的 /api/stats 持续 500。丢弃即触发重扫补回，
    无副作用。（NSFW 侧同款装载口 `_load_nsfw_cache` 早已这么做，这里是漏掉的那一半。）

    P3（2026-11-15）：`_low` 不落盘（见 `_strip_derived`），所以每份缓存装进来都要
    补一次 —— 本机 11942 条 60 ms，只在后台构建线程启动时发生。被改坏的 `_low`
    （非字符串）同样视为缺失：留着它搜索侧 `.find()` 会抛 AttributeError，
    整次搜索直接 500，比慢 50 ms 严重得多。
    """
    out = {}
    for k, v in (raw_map or {}).items():
        if not _idx_entry_ok(v):
            continue
        if not isinstance(v.get("_low"), str):
            v["_low"] = _hay_of(v["pos"], v["neg"], v["model"], v["loras"]).lower()
        out[k] = v
    return out


def _strip_derived(m: dict) -> dict:
    """落盘前剥掉键名以 `_` 开头的**派生字段**（目前只有搜索用的 `_low`）。

    索引缓存是磁盘文件（本机 11942 条 = 23.7 MB）。`_low` 是整段提示词的小写副本，
    量级与原字段相同 —— 原样写进去等于让索引文件原地翻倍，而它纯粹是
    「避免每次搜索重算」的内存缓存，装载时 60 ms 就能重建。约定：
    **`_` 前缀 = 只活在内存里，不进磁盘**。
    """
    out = {}
    for k, v in m.items():
        if isinstance(v, dict) and any(kk[:1] == "_" for kk in v):
            out[k] = {kk: vv for kk, vv in v.items() if kk[:1] != "_"}
        else:
            out[k] = v
    return out


def _save_idx_cache(map_obj=None, root=None):
    """落盘索引缓存；map_obj 传入时用它（构建中的新表），否则用当前已发布的表。

    B4-P2-5（2026-10-19）：`root` 由调用方显式传本线程的 `myroot`。原来写的是全局
    `IDX["root"]` —— 代际校验通过之后、真正写盘之前仍有窗口，此时换根会让新线程把
    `IDX["root"]` 改成新根，于是落出「root=新根、map=旧根内容」的自相矛盾缓存；
    下次装载按 root 判等命中，这些路径 stat 全失败，每张刷一条 `[warn] 索引跳过`
    （大库能刷几千行）。不传时退回全局值（兼容单独调用/调试）。
    """
    try:
        IDX_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = IDX_FILE.with_suffix(f".{_tmp_tag()}.tmp")
        tmp.write_text(json.dumps({"root": root if root else IDX["root"],
                                   "pver": PARSER_VER,
                                   "map": _strip_derived(IDX["map"] if map_obj is None else map_obj)},
                                  ensure_ascii=False), "utf-8")
        tmp.replace(IDX_FILE)
    except OSError as ex:
        # §6.1：此前完全静默。索引缓存丢了本身不致命（下次会重建），
        # 但「磁盘写不进去」这件事必须能在日志里看见 —— 它往往先于其它写失败出现。
        # B4-P2-3：失败时必须清掉自己那份 tmp，否则 20MB 的半截文件永久堆在数据目录
        # （`_save_nsfw_cache` 早就会 unlink，这里是漏网的那一处）。
        try:
            tmp.unlink()
        except (OSError, UnboundLocalError):
            pass
        log(f"[warn] 索引缓存落盘失败: {type(ex).__name__}: {ex}")


def _idx_entry_fresh(e, st: os.stat_result) -> bool:
    """索引里的旧条目能否直接复用（= 这张图自上次索引以来没变过）。

    B4-P2-1（2026-10-19）：判重必须比**纳秒**戳。原来是
    `e.get("mtime") == int(st.st_mtime)` —— 同一秒内同尺寸重写、或 copy2/utime
    还原 mtime 的覆盖导入，int 值一模一样 ⇒ 判「未变更」⇒ 这张图**永久**保留旧的
    正/负向提示词、模型、LoRA 分类，只有「重建索引」才能纠正。
    旧缓存里没有 `mtime_ns` 字段 ⇒ `None != int` ⇒ 当作变了，重算一次即可。

    单独抽成函数是为了**可测**：门禁直接拿两个 stat 打这个谓词，
    而不是靠「代码里出现了某个字符串」来间接推断。
    """
    return bool(e) and e.get("mtime_ns") == st.st_mtime_ns and e.get("size") == st.st_size


def _index_builder(root: str):
    """后台构建/增量校验索引；根目录切换或重建时自行中止。

    P1-15：进来先认领一个**代际号** mygen。之后每次要写回 IDX / 落盘前都比一次
    `IDX_GEN[0] == mygen` —— 只要期间有人发起过重建或换根（代际+1），这一代线程
    的全部产出即作废，避免旧扫描覆盖新结果。
    """
    myroot = root
    with IDX_LOCK:
        mygen = IDX_GEN[0]
    try:
        rootp = Path(root)
        with IDX_LOCK:
            if IDX_GEN[0] != mygen:              # 还没开工就被更新的一代取代了
                return
            same_root = IDX["root"] == root
            old = dict(IDX["map"]) if same_root else {}
        # P3（2026-11-15）：把「读缓存文件 + 逐条校验 + 预算 `_low`」整段搬到锁外。
        #   这几步跟索引状态无关，却在锁内占着几百毫秒（本机 23.7 MB / 11942 条：
        #   读 156 ms + 预算 60 ms），期间所有搜索与每秒一次的 /api/stats 都排队。
        #   搬出去要多验一次代际号，代价可忽略。
        cached = {}
        if IDX_FILE.is_file():
            try:
                cache = json.loads(IDX_FILE.read_text("utf-8"))
                if (cache.get("root") == root
                        and cache.get("pver") == PARSER_VER):
                    cached = _load_idx_map(cache.get("map"))
            except (OSError, ValueError, AttributeError, TypeError):
                cached = {}
        with IDX_LOCK:
            if IDX_GEN[0] != mygen:              # 读盘期间被重建/换根取代
                return
            old.update(cached)
            # 增量校验期间继续沿用旧索引（新表建好再整体替换）：
            # 否则每 30 秒一次的增量校验会短暂清空索引，期间提示词搜索返回 0 条、
            # 左栏「工作流」数量也会掉到 0。
            IDX.update({"root": root, "map": old if same_root else {},
                        "done": 0, "total": 0, "building": True})
        imgs = []
        # 2026-11-08：隐藏名单在**扫描阶段**就生效，不是只在列表里过滤 ——
        # 索引里没有它们 ⇒ 提示词搜索、工作流统计、左栏「全部」数字自然都不含。
        # 这是「彻底不扫」与「假隐藏」的分界线。
        _hid = hidden_snapshot(root)
        _stack = [(str(rootp), 0)]
        _visited = {os.path.realpath(str(rootp))}
        # P0-1（2026-10-08 全代码审计）：遍历失败必须计数，不能一律 `continue` 吞掉。
        # 外接盘（G:）休眠 / 掉线 / USB 瞬断时 root 的 scandir 会抛 OSError，旧代码
        # 静默跳过 ⇒ imgs 保持为空 ⇒ 下面把**空表发布并落盘**，真实库 20.6MB 的
        # prompt_index.json 当场被替换成空表，界面还显示「已建好」，用户完全无法
        # 与「图片被删光」区分，且需重新全库解析才能恢复。
        # 现在：root 自身读不到 = 硬失败（直接丢弃本代结果）；子目录失败也计数，
        # 超阈值同样丢弃 —— 宁可保留旧索引（stale），绝不把「读不到」当成「没有」。
        _scan_fail = 0
        _root_fail = False
        while _stack:
            _cur, _depth = _stack.pop()
            try:
                with os.scandir(_cur) as it:
                    for e in it:
                        try:
                            if e.is_dir():
                                if _depth < 10 and not _hid_hit(_hid, e.path):
                                    rp = os.path.realpath(e.path)
                                    if rp not in _visited:
                                        _visited.add(rp)
                                        _stack.append((e.path, _depth + 1))
                            elif os.path.splitext(e.name)[1].lower() in IDX_IMG_EXTS:
                                imgs.append(Path(e.path))
                        except OSError:
                            _scan_fail += 1
                            continue
            except OSError as ex:
                _scan_fail += 1
                if os.path.normcase(os.path.realpath(_cur)) == os.path.normcase(os.path.realpath(str(rootp))):
                    _root_fail = True
                log(f"[warn] 索引遍历失败 {_cur}: {type(ex).__name__}: {ex}")
                continue
        if _root_fail or _scan_fail > INDEX_SCAN_FAIL_MAX:
            # 保留旧索引（含 IDX["map"] 与磁盘缓存），只落 building 旗标并留痕。
            # 不发布、不落盘 —— 这是本函数唯一「不产出」的出口。
            with IDX_LOCK:
                if IDX_GEN[0] != mygen:          # 已被新代取代：什么都别动
                    return
                IDX["building"] = False
                IDX["built_at"] = time.time()    # 算是「刚校验过」，避免 30 秒后再撞一次
                IDX["stale"] = True
                IDX["stale_reason"] = ("根目录不可读" if _root_fail
                                       else f"{_scan_fail} 个子目录读取失败")
                IDX["total"] = len(IDX["map"])   # 进度条别停在半路
                IDX["done"] = len(IDX["map"])
            log(f"[error] 索引构建中止：{'根目录不可读' if _root_fail else f'遍历失败 {_scan_fail} 次'}"
                f" —— 保留旧索引（{len(old)} 条），未覆盖磁盘缓存")
            return
        with IDX_LOCK:
            IDX["total"] = len(imgs)
            IDX["stale"] = False
            IDX["stale_reason"] = ""
        newmap = {}
        changed = False                           # 本轮是否有新增/重解析（收尾落盘判据）
        # 2026-10-27（代码梳理 O1）：检查点需要一个**可复位**的脏标志。
        # 原先检查点直接判 `changed`，而它一旦置 True 就不再复位 ⇒ 一次全量建库
        # 或一次「只多了一张新图」的增量校验，之后每个 300 张边界都会重写整份表
        # （7484 张 ≈ 24 次全表序列化 + 24 次整文件落盘）。复位 changed 又会让
        # 「长度没变但有改动」的收尾落盘被跳过，所以拆成两个标志：
        # changed 保底收尾，dirty 只管「上次落盘之后又改了没有」。
        dirty = False
        for i, p in enumerate(imgs):
            if STATE.get("root") != myroot:      # 用户已切换目录：清场后放弃
                with IDX_LOCK:
                    if IDX_GEN[0] == mygen:      # 只由「仍是最新一代」的线程清场，别把新线程的地盘掀了
                        IDX.update({"root": None, "map": {}, "done": 0,
                                    "total": 0, "building": False})
                return
            with IDX_LOCK:
                if IDX_GEN[0] != mygen:          # P1-15：已被重建/换根取代 → 放弃，不写回
                    return
            try:
                st = p.stat()
                key = str(p)
                e = old.get(key)
                # B4-P2-1（2026-10-19）：判重走 `_idx_entry_fresh`（比纳秒戳）。
                # 原来这里是比较 `e.get("mtime") == int(st.st_mtime)` —— 同一秒内
                # 同尺寸重写、或 copy2/utime 还原 mtime 的覆盖导入，int 值一模一样
                # ⇒ 判「未变更」⇒ 这张图永久保留旧提示词。谓词抽出去是为了可测。
                if _idx_entry_fresh(e, st):
                    newmap[key] = e              # 未变更，复用旧条目
                else:
                    newmap[key] = _index_entry(p, st)
                    changed = True
                    dirty = True
            except Exception as ex:            # 单张坏图（畸形 PNG / 权限）不得拖垮整批索引
                log(f"[warn] 索引跳过 {p.name}: {type(ex).__name__}: {ex}")
                continue
            with IDX_LOCK:
                if IDX_GEN[0] != mygen:          # 进度也别再往新版的地盘上写
                    return
                IDX["done"] = i + 1              # 只更新进度；索引表等建完再整体替换
            if dirty and (i + 1) % 300 == 0:
                # 阶段性落盘（无变更不写盘，避免写放大）。
                # ⚠️ 写「旧表 + 已处理部分」而不是当前的半张新表：检查点只在
                # `old` 为空（首次建库）时才是等价于新表；增量校验时直接写 newmap
                # 会把磁盘上完整的旧索引覆盖成「只含前 N 张」的半张表 ——
                # 万一此时进程被杀，下次启动就带着一张残缺索引起来。
                # 合并一次 dict 的代价远小于重新序列化整表，且只在 dirty 时发生。
                _save_idx_cache({**old, **newmap} if old else newmap, myroot)
                dirty = False
        # P0-1：最后一道闸 —— 旧表非空、新表为空 ⇒ 几乎只能是「盘读不到」而不是
        # 「图真的被删光了」。此时**拒绝发布、拒绝落盘**，保留旧索引（上一步已置 stale）。
        # 2026-11-08：「几乎」之外还有一种合法情形 —— 隐藏名单把库里的图全遮住了。
        # 判据补成「名单为空 或 根读不到 或 本轮有扫描失败」才拦；三者都不成立时
        # 空表就是真值，必须发布，否则用户隐藏完整个库、界面还留着满屏的图。
        _hid_empty_ok = (bool(_hid) and not _scan_fail
                         and _root_readable(str(rootp)))
        if not newmap and old and not _hid_empty_ok:
            with IDX_LOCK:
                if IDX_GEN[0] != mygen:
                    return
                IDX["building"] = False
                IDX["built_at"] = time.time()
                IDX["stale"] = True
                IDX["stale_reason"] = "新扫描结果为空，疑似根目录不可读，已保留旧索引"
                IDX["total"] = len(IDX["map"])
                IDX["done"] = len(IDX["map"])
            log(f"[error] 索引重建结果为空（旧索引 {len(old)} 条）—— 拒绝覆盖，保持旧索引")
            return
        with IDX_LOCK:
            if IDX_GEN[0] != mygen:              # P1-15：收工前最后一道闸 —— 旧代结果禁止覆盖新索引
                return
            IDX["map"] = newmap                  # 整体替换：期间旧索引一直可用
            IDX["building"] = False
            IDX["built_at"] = time.time()
        if changed or len(newmap) != len(old):   # 有新增/改动/删除才落盘
            _save_idx_cache(newmap, myroot)
    except Exception:
        with IDX_LOCK:
            if IDX_GEN[0] == mygen:              # 只有当前代才该把 building 落下来
                IDX["building"] = False


def ensure_index():
    """搜索时调用：索引缺失或根目录不匹配则启动后台构建。

    索引已存在时按 REINDEX_INTERVAL 节流做增量刷新——_index_builder
    按 mtime+size 复用旧条目，重跑只会重新 stat 校验并解析新增/变更
    文件，因此出图后不必重启进程也能搜索到新图。
    """
    now = time.time()
    root = STATE.get("root")
    if not root:
        return
    with IDX_LOCK:
        t = IDX_THREAD[0]
        if t and t.is_alive():
            return
        if (IDX["root"] == root and IDX.get("built_at")
                and now - IDX["built_at"] < REINDEX_INTERVAL):
            return                      # 索引已建成（含 0 结果）且在节流窗口内
        # 登记与启动必须在同一临界区。`Thread.is_alive()` 对「已登记但还没
        # start()」的线程返回 **False** —— 若把 start() 放到锁外，并发调用 B
        # 会看到未启动的 T1 而误判「没有线程在跑」，再建一条 T2 并覆盖登记；
        # 两条 _index_builder 领到同一个 mygen（代际闸形同虚设），9700 张的
        # 元数据被解析两遍、外接盘 I/O 与进度计数各走一套。
        #
        # 旗标也必须由**调用方线程同步**置起：`IDX["building"] = True` 原先只写在
        # _index_builder 里 ⇒ 本函数返回的那一刻它还是上一轮的 False。而 /api/search
        # 正是带着这个旗标回话的（它只调 ensure_index() 就立刻 search_index()）——
        # 冷启动后的第一次搜索于是自称「没在建索引」并返回 0 条：前端把「正在建」
        # 显示成「没搜到」，测试侧「等 building 落下来」的循环也会当场跳出。
        # 实测 20/20 轮复现（dev/_idx_building_probe.py），nsfw_check 的 F 段
        # 因此 3/5 轮翻红（2026-10-09）。取值与 _index_builder 起手那句保持一致。
        IDX.update({"done": 0, "total": 0, "building": True})
        t = threading.Thread(target=_index_builder, args=(root,), daemon=True)
        IDX_THREAD[0] = t
        t.start()


# ---------------------------------------------------------------- 图片宽高（2026-11-04）
# 原比例铺满布局（frontend/js/justified.js）需要每张图的 w/h 才能在**首帧**把行切对。
# 首选来源是提示词索引（entry["dim"] = "1024x1536"），这里只做「字典命中 + 正则缓存」，
# 不读文件、不 stat —— 实测全库 11,641 张的 dim 只有几十种不同取值，一条列表请求
# 走缓存后正则总调用次数 ≈ 去重后的尺寸种数，成本可忽略。
#
# 但索引有两个盲区（2026-11-04 用户实测「新生成的图还是方格」）：
#   ① 索引是 REINDEX_INTERVAL(30s) 节流的**增量**刷新 ⇒ 刚出图的那几十秒里，
#      新图在列表里根本没有 dim ⇒ 前排按 1:1 排成方格；
#   ② 索引只解析 png（IHDR）与 jpeg；webp/gif/bmp 永远没有 dim。
# 因此补一条兜底：索引未命中时直接读文件头（几十字节，进 _HEAD_CACHE）。
# **索引命中路径完全不变**（不 open、不 stat），对既有一万张老图零额外开销。
_DIM_CACHE = {}
_DIM_RE = re.compile(r"(\d+)\s*x\s*(\d+)")
_HEAD_CACHE = {}                 # path -> (size, mtime_ns, w, h)
_HEAD_CACHE_MAX = 16384
_HEAD_SOF = frozenset((0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                       0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF))


def _parse_head(b):
    """从文件头解析 (w, h)：PNG / JPEG / GIF / BMP / WebP；认不出返回 (0, 0)。

    只依赖前 64KB，不解码像素 —— 一张 20MB 的图与一张 20KB 的图成本相同。
    """
    try:
        n = len(b)
        # PNG：8 字节签名 + 4 字节块长 + "IHDR" + 宽高（各 4 字节大端）
        if n >= 24 and b[:8] == b"\x89PNG\r\n\x1a\n" and b[12:16] == b"IHDR":
            w, h = struct.unpack(">II", b[16:24])
            return (int(w), int(h))
        # GIF：逻辑屏幕宽高，小端
        if n >= 10 and b[:6] in (b"GIF87a", b"GIF89a"):
            w, h = struct.unpack("<HH", b[6:10])
            return (int(w), int(h))
        # BMP：DIB 头里是 int32（高度为负表示自上而下）
        if n >= 26 and b[:2] == b"BM":
            w, h = struct.unpack("<ii", b[18:26])
            return (abs(int(w)), abs(int(h)))
        # WebP：VP8X / VP8 / VP8L 三种块头，宽高偏移各不相同
        if n >= 30 and b[:4] == b"RIFF" and b[8:12] == b"WEBP":
            cc = b[12:16]
            if cc == b"VP8X":
                return (1 + int.from_bytes(b[24:27], "little"),
                        1 + int.from_bytes(b[27:30], "little"))
            if cc == b"VP8 ":
                return (struct.unpack("<H", b[26:28])[0] & 0x3FFF,
                        struct.unpack("<H", b[28:30])[0] & 0x3FFF)
            if cc == b"VP8L" and n >= 25 and b[20] == 0x2F:
                v = int.from_bytes(b[21:25], "little")
                return ((v & 0x3FFF) + 1, ((v >> 14) & 0x3FFF) + 1)
        # JPEG：扫段找 SOFn（段内第 5~8 字节，高在前）
        if n >= 4 and b[:2] == b"\xFF\xD8":
            i = 2
            while i + 9 < n:
                if b[i] != 0xFF:
                    i += 1
                    continue
                m = b[i + 1]
                if m in (0xD8, 0x01) or 0xD0 <= m <= 0xD7:
                    i += 2
                    continue
                if m in (0xD9, 0xDA):        # EOI / SOS：往后是压缩数据，别扫了
                    break
                seg = struct.unpack(">H", b[i + 2:i + 4])[0]
                if seg < 2:
                    break
                if m in _HEAD_SOF:
                    h, w = struct.unpack(">HH", b[i + 5:i + 9])
                    return (int(w), int(h))
                i += 2 + seg
    except Exception:
        pass
    return (0, 0)


def _head_dims(path, st=None):
    """读文件头取宽高，按 (size, mtime_ns) 缓存；失败返回 (0, 0)。"""
    try:
        if st is None:
            st = os.stat(path)
        key = str(path)
        c = _HEAD_CACHE.get(key)
        if c and c[0] == st.st_size and c[1] == st.st_mtime_ns:
            return (c[2], c[3])
        with open(path, "rb") as f:
            b = f.read(65536)
        w, h = _parse_head(b)
        if len(_HEAD_CACHE) >= _HEAD_CACHE_MAX:
            _HEAD_CACHE.clear()          # 简单粗暴：到顶就清，避免无界增长
        _HEAD_CACHE[key] = (st.st_size, st.st_mtime_ns, w, h)
        return (w, h)
    except Exception:
        return (0, 0)


def dims_of(path, idxmap=None, head=False, st=None):
    """取 (w, h)；未知返回 (None, None) 或 (0, 0)。

    idxmap 由调用方在锁内取一次引用传入（索引是整表替换，引用在整个请求内稳定），
    避免每条 item 都抢一次 IDX_LOCK。
    head=True 时，索引未命中会走 _head_dims 读文件头兜底（新图 / webp）。
    st 可传入调用方已有的 stat 结果，省一次系统调用。
    """
    e = None
    try:
        src = IDX["map"] if idxmap is None else idxmap
        e = src.get(str(path))
    except Exception:
        e = None
    if e:
        s = e.get("dim") or ""
        v = _DIM_CACHE.get(s)
        if v is None:
            m = _DIM_RE.match(s)
            v = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
            if len(_DIM_CACHE) < 4096:      # 上限：异常数据不会把缓存撑爆
                _DIM_CACHE[s] = v
        if v[0] and v[1]:
            return v
    if head:
        return _head_dims(path, st)
    return (0, 0)


def _wait_index(timeout: float = 5.0):
    """筛选面板用：确保提示词索引可用（已就绪立即返回；正在建则最多等 timeout 秒）。

    索引建好后 meta_batch 可以整批命中内存，不必逐张读文件；磁盘有缓存时
    重建通常 1~2 秒内完成（只做 mtime+size 增量校验）。
    """
    root = STATE.get("root")
    if not root:
        return
    ensure_index()
    t0 = time.time()
    while time.time() - t0 < timeout:
        with IDX_LOCK:
            if IDX["root"] == root and not IDX["building"] and IDX["map"]:
                return
        time.sleep(0.1)


# 搜索结果上限（2026-10-18）：默认 2000，可用 CMV_SEARCH_LIMIT 覆盖；
# 另设绝对上限，防止 `?limit=999999` 把内存/带宽打爆（本机回环也不该无条件信任）。
SEARCH_LIMIT_DEFAULT = 2000
SEARCH_LIMIT_MAX = 20000


def _clamp_search_limit(limit) -> int:
    """把请求里的 limit 收敛到 [1, SEARCH_LIMIT_MAX]；缺省/非法走环境变量或默认值。"""
    if limit is None or limit == "":
        raw = str(os.environ.get("CMV_SEARCH_LIMIT", "") or "").strip()
        n = SEARCH_LIMIT_DEFAULT
        if raw:
            try:
                n = int(raw)
            except ValueError:
                n = SEARCH_LIMIT_DEFAULT
    else:
        try:
            n = int(limit)
        except (TypeError, ValueError):
            n = SEARCH_LIMIT_DEFAULT
    return max(1, min(n, SEARCH_LIMIT_MAX))


def search_index(q: str, base_dir: str = "", limit=None) -> dict:
    with IDX_LOCK:
        building = IDX["building"]
        done, total = IDX["done"], IDX["total"]
        # A12（2026-11-05 审计复核）：建库失败时 `IDX["stale"]` 会置真、原因写进
        # `stale_reason`，但搜索响应此前完全不带这两个字段 —— 前端只能看到
        # `building:false` + 一份旧索引的结果，看不出「这是过期数据 / 建库被中止了」。
        # `/api/stats` 早就带这两个字段，搜索这条路径漏了，现在补齐。
        idx_stale, idx_stale_reason = IDX.get("stale", False), IDX.get("stale_reason", "")
        snap = list(IDX["map"].items())
    terms = q.lower().split()              # 多关键词 AND：全部命中才算
    prefix = ""
    if base_dir:
        prefix = base_dir.rstrip("\\/").lower() + os.sep   # 限定当前目录（含子目录）
    favs = load_favs()
    tgs = load_tags()
    _nctx = nsfw_ctx()          # 一份快照整个循环复用：每张都取锁/读盘会明显拖慢搜索
    results = []
    for path, e in snap:
        if prefix and not path.lower().startswith(prefix):
            continue
        # P3（2026-11-15）：小写版 `_low` 在索引条目里预算好（派生字段，不落盘）。
        #   改前每条都 `join` 4 个字段再 `lower()` —— 11942 条实测 56 ms，其中
        #   绝大多数图在第一个关键词就落选，等于把整段提示词白复制了两遍。
        #   `hay`（原始大小写）只在**命中之后**才拼：结果摘要要求保留原始大小写，
        #   但命中集通常只有几百条，成本可以忽略。
        low = e.get("_low")
        hay = None
        if not isinstance(low, str):            # 老条目兜底（装载时会补齐，这里防手改）
            hay = _hay_of(e["pos"], e["neg"], e["model"], e["loras"])
            low = hay.lower()
        pos0, ok = None, True
        for t in terms:
            i = low.find(t)
            if i < 0:
                ok = False
                break
            if pos0 is None:
                pos0 = i
        if not ok:
            continue
        if hay is None:
            hay = _hay_of(e["pos"], e["neg"], e["model"], e["loras"])
        i = pos0 or 0
        snip = hay[max(0, i - 40):i + 80].replace("\n", " ").strip()  # 保留原始大小写
        _ext = os.path.splitext(path)[1].lower()
        _typ = "video" if _ext in VIDEO_EXTS else "image"
        # 结果页同样走原比例布局 ⇒ 也要宽高。索引里的条目本身可能没有 dim
        # （webp/gif 索引不解析），所以这里允许读文件头兜底；
        # 循环在 IDX_LOCK 之外，不会把索引更新/其他搜索堵住。
        _w, _h = dims_of(path, head=True)
        results.append({
            "name": os.path.basename(path), "path": path,
            "type": _typ, "size": e["size"], "mtime": e["mtime"],
            "playable": _typ == "image" or _ext in PLAYABLE_VIDEO,
            "png": _ext == ".png", "fav": path in favs,
            "favcat": favs[path]["cat"] if path in favs else None,
            "tags": sorted(tgs.get(path, [])),      # 搜索语法 tag: 需要（提示词模式同样生效）
            "snippet": snip,
            **({"w": _w, "h": _h} if (_w and _h) else {}),
        })
    # 批次 F · F4：**先排序切片、再算 nsfw**。
    # 排序键是 mtime，与 n/ns 无关 ⇒ 换序不改变结果集与顺序。
    # 改前是「给全部命中算 nsfw → 再切 500」：命中 3244 条时白算 2744 次（≈290 ms）。
    results.sort(key=lambda x: -x["mtime"])
    # P1#5（10-15 全代码审计）：`results[:500]` 是**静默截断**，而返回的 `total` 是
    # **索引总数**（不是命中数）⇒ 前端只能数「服务端给了几条」，命中超过 500 时
    # 底栏「命中 500 项」是假话，用户无从察觉结果被阉割。
    # 这里显式补两个字段：matched = 命中总数（切片前）、truncated = 是否被截断。
    # 语义保持向后兼容（total / results 口径不变），旧前端不受影响。
    #
    # 2026-10-18（第 23 级补充）：上限从写死的 500 提成可配（默认 2000）。
    #   500 对本机这个规模的库太小 —— 单关键词常见命中上千，用户只看到最新 500 张，
    #   而且没有任何「还有更多」的出口。
    #   ⚠️ **刻意不做分页**：筛选是前端本地做的（标签/收藏/日期/标记/NSFW），
    #      分页只会让「筛选只在已加载的子集里生效」，与这条要修的毛病同源。
    #      真要让上万条也可筛，得先把筛选下推到服务端 —— 那是另一个量级的改动。
    matched = len(results)
    cap = _clamp_search_limit(limit)
    top = results[:cap]
    for r in top:
        r["n"], r["ns"] = nsfw_fields(r["path"], r["mtime"], r["size"], _nctx)
    return {"building": building, "done": done, "total": total,
            "stale": bool(idx_stale), "stale_reason": idx_stale_reason,
            "matched": matched, "truncated": matched > len(top),
            "limit": cap, "results": top}


# ---------------------------------------------------------------- NSFW 内容检测（V2.11.0）
# 分数在服务端算、落盘缓存、由列表接口一次性带出 —— 不给每张卡片单开请求。
# 这是整个功能的性能红线：一屏 100 张卡片多 100 个请求，页面直接废掉。
#
# 两张表性质完全不同，别混：
#   nsfw_index/       派生数据。随便删、随便重建，不影响任何用户内容。
#   nsfw_manual.json  用户数据。人工纠正过的判定，重建索引**不能**动它。
#
# 与提示词索引刻意分成两条线程：索引要在 1~2 秒内可用，推理慢一个数量级，
# 混在一条线程里会把提示词搜索一起拖住。

# V2.11.3：缓存目录化 —— 一个根目录一份文件（见 _nsfw_cache_path）。
# 以前是单个 nsfw_index.json 只记得住「最后扫的那个根」，用户在多根目录间
# 来回切就来回作废、切回去就得重扫。人工纠正表不受影响，仍是全局一份。
NSFW_DIR = DATA_DIR / "nsfw_index"
NSFW_LEGACY_FILE = DATA_DIR / "nsfw_index.json"   # V2.11.2 及以前的单文件，迁移后留 .bak
NSFW_MANUAL_FILE = DATA_DIR / "nsfw_manual.json"
NSFW_MODELS_DIR = DATA_DIR / "models" / "nsfw"     # 精确档等额外模型的落点
# 只扫静态图。2026-11-05 明确决定：**视频不参与 NSFW 检测**（抽帧 + 逐帧推理的代价
# 远大于收益，视频仍按原样显示）。所以这里不含 VIDEO_EXTS 不是漏配 —— 要加之前先看这条。
NSFW_IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
NSFW_DEFAULT_THRESHOLD = 0.60                      # score >= 它算 NSFW
NSFW_THRESHOLD_RANGE = (0.05, 0.95)
NSFW_AUTO_INTERVAL = 60.0     # 扫过一遍之后，增量补测的最小间隔（出图后最多 1 分钟能被认出）

NSFW = {"root": None, "map": {}, "done": 0, "total": 0,
        "building": False, "built_at": 0.0, "model": "",
        # 最近一次扫描的失败留痕（空串 = 一切正常）。「跑完了但零结果」是最坏的
        # 形态 —— 进度照走满、界面报「完成」、用户以为保护在生效，却没有 error
        # 可看。nsfw_status 会把它带出去给前端提示。
        "last_error": ""}
# 单张推理失败逐条打日志的上限：坏图批量出现时只留前 N 条 + 汇总数字，防刷屏
_NSFW_SKIP_LOG_MAX = 5
NSFW_LOCK = threading.Lock()
NSFW_THREAD = [None]
# 2026-11-05 P2：扫描代际。此前只靠循环内 `STATE["root"] != myroot` 中止 ——
# 但**收尾**那几条赋值没有代际判据，窗口（循环结束到写 NSFW["map"] 之间）里
# 用户换根 / 又点一次「检测」时，旧根的结果会被当成新根的表写进去。
# 起线程处 +1，收尾处比对；与 _index_builder 的 IDX_GEN 同款。
NSFW_GEN = [0]
_NSFW_MANUAL = {"at": -1.0, "map": {}}      # 按文件 mtime 缓存，别每个请求都读一次盘
_NSFW_MANUAL_LOCK = threading.Lock()
# A11（2026-11-05 审计复核）：人工标注是「读 → 改 → 写盘 → 回填缓存」的复合操作，
# 只靠 `_NSFW_MANUAL_LOCK`（保护缓存字典）串不起来 —— 两个请求可以同时读到同一份
# 旧快照，各自改一条再各自整文件覆盖，后写的把先写的那条**静默吃掉**。
# 写路径用这把独立锁整体串行；它不与 `_NSFW_MANUAL_LOCK` 嵌套（读缓存仍在更里层取锁）。
_NSFW_MANUAL_WLOCK = threading.Lock()

# ---- 根目录变更迁移（V2.12.0）----
# 换根（移动/改名/换盘符）后，所有按绝对路径存的东西集体失配：NSFW 缓存文件名是
# md5(根)（_nsfw_cache_path），map 键与人工纠正/收藏/标签/标记/最近的键全是绝对路径。
# 数据其实都还在盘上，只是键对不上了 —— 这里做一次「指纹验证 + 前缀重写」把它们接回家。
NSFW_MIGRATE_RATIO = 0.60     # 指纹命中率门槛（实测定的默认值）
NSFW_MIGRATE_MIN_HITS = 10    # 命中数下限：防「碰巧相似」的库被误迁；小库按比例放宽
NSFW_MIGRATE_SAMPLE = 500     # 指纹抽样上限：打分只看这么多张，大库不必全比
_MIGRATE_LOCK = threading.Lock()
_MIGRATE_STATE = {"running": False, "done": None, "last_try": 0.0}
# done   : 最近一次迁移的报告 dict（前端经 /api/migrate_report 取走即清，一次迁移只提示一次）
# last_try: 自动检测的节流戳 —— stats 是秒级轮询，没有它「索引没建好等下一拍」会变成
#           每秒一次的全目录扫描

# 把「数据目录下的模型目录」也纳入查找（设置页下载的精确档落在这里）。
# 随包分发的 models/nsfw 由 nsfw.py 自己按程序目录 / PyInstaller 解包目录去找 ——
# 这个项目要给别人用，模型必须跟着包走，不能依赖联网下载。
NSFW_ENGINE.configure([NSFW_MODELS_DIR])


def nsfw_tiers() -> tuple:
    """可用档位 id。从引擎的登记表取，免得两边各写一份枚举、早晚对不上。"""
    m = getattr(NSFW_ENGINE, "MODELS", None)
    return tuple(m.keys()) if m else ("light",)


def nsfw_available() -> bool:
    return bool(NSFW_ENGINE and NSFW_ENGINE.available())


def nsfw_reason() -> str:
    """不可用的原因原文。要能在界面上显示出来，而不是只躺在日志里。"""
    return "" if not NSFW_ENGINE else NSFW_ENGINE.unavailable_reason()


_NSFW_MODEL_REPORT = {"want": "", "got": "", "why": ""}


def load_nsfw_model() -> str:
    """当前档位。非法值或模型文件被删了一律回落默认档 —— 设置里存着 precise
    但文件不在了，也不能让整个功能崩掉。

    B4-P2-4（2026-10-19）：回落**不再静默**。此前设置里存着 `precise`、模型文件却
    没随包，实际全程按 `light` 跑，日志一个字都没有，用户以为在用精确档
    （设置页读的是 settings 里的值，和真正生效的档位可以不一致）。
    现在把「请求的档 / 实际用的档 / 原因」记进 `_NSFW_MODEL_REPORT` 由
    `/api/nsfw/status` 带出去，并且**只在变化时**写一行日志（这个函数调用很频繁）。
    """
    want = str(load_settings().get("nsfw_model") or "").strip()
    tiers = nsfw_tiers()
    why = ""
    try:
        if want in tiers and NSFW_ENGINE.find_model(want)[0]:
            got = want
        elif want and want not in tiers:
            got, why = (tiers[0] if tiers else "light"), f"设置里的档位 {want!r} 不在可用列表"
        elif want:
            got, why = (tiers[0] if tiers else "light"), f"{want!r} 的模型文件找不到"
        else:
            got = tiers[0] if tiers else "light"
    except Exception as ex:                   # find_model 自己炸了：同样要留痕
        got, why = (tiers[0] if tiers else "light"), f"查模型出错：{type(ex).__name__}: {ex}"
    if why and (_NSFW_MODEL_REPORT["want"], _NSFW_MODEL_REPORT["got"]) != (want, got):
        _NSFW_MODEL_REPORT.update({"want": want, "got": got, "why": why})
        log(f"[nsfw] 档位回落：{why} —— 实际使用 {got}（设置里是 {want or '空'}）")
    elif not why:
        _NSFW_MODEL_REPORT.update({"want": want, "got": got, "why": ""})
    return got


def valid_nsfw_model(mid: str) -> bool:
    """档位合法性（与 save_nsfw_model 同口径）。

    B9-P1-2（2026-10-10）：独立出来是为了让 `/api/prefs` 能**先全量校验、后统一写盘** ——
    否则一次请求里排在非法项前面的字段已经落盘了（见该端点的注释）。
    """
    return str(mid) in nsfw_tiers()


def save_nsfw_model(mid: str) -> bool:
    if not valid_nsfw_model(mid):
        return False
    with _store_lock(SETTINGS_LOCK, "NSFW 模型"):
        s = load_settings()
        s["nsfw_model"] = str(mid)
        return save_settings(s)


_NSFW_THR = {"at": -1.0, "v": NSFW_DEFAULT_THRESHOLD}


def load_nsfw_threshold() -> float:
    """阈值判定会在「每一条媒体」上被调用一次（收藏夹一屏就是几百条），
    所以这里按 settings.json 的 mtime 缓存 —— 只 stat 一次，不 read+parse。
    写入方（save_nsfw_threshold）会把 at 置 -1 强制重读，不会读到旧值。"""
    try:
        at = SETTINGS_FILE.stat().st_mtime
    except OSError:
        at = -1.0
    if _NSFW_THR["at"] == at:
        return _NSFW_THR["v"]
    try:
        v = float(load_settings().get("nsfw_threshold", NSFW_DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        v = NSFW_DEFAULT_THRESHOLD
    lo, hi = NSFW_THRESHOLD_RANGE
    v = max(lo, min(hi, v))
    _NSFW_THR["at"], _NSFW_THR["v"] = at, v
    return v


def valid_nsfw_threshold(v) -> bool:
    """阈值合法性（与 save_nsfw_threshold 同口径）。

    B9-P1-2（2026-10-10）：供 `/api/prefs` 两趟校验用 —— 校验必须能独立于写盘调用。
    """
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    lo, hi = NSFW_THRESHOLD_RANGE
    return lo <= f <= hi


def save_nsfw_threshold(v) -> bool:
    """阈值存服务端而不是前端 localStorage：判定发生在服务端，阈值是判定的一部分。
    前端自己记一份的话，切换浏览器 / 桥接脚本读到的就是两个不同的判定标准
    （delete_mode 已经踩过这个坑，注释里还留着）。"""
    if not valid_nsfw_threshold(v):
        return False
    f = float(v)
    with _store_lock(SETTINGS_LOCK, "NSFW 阈值"):
        s = load_settings()
        s["nsfw_threshold"] = round(f, 3)
        ok = save_settings(s)
    _NSFW_THR["at"] = -1.0            # 强制下次重读，避免停在旧值上
    return ok


def nsfw_enabled() -> bool:
    """总开关。关掉后列表不带判定、界面上的三态控件一并停用。"""
    try:
        on = bool(load_settings().get("nsfw_enabled", True))
    except Exception:
        on = True
    return on and nsfw_available()


def save_nsfw_enabled(on: bool) -> bool:
    with _store_lock(SETTINGS_LOCK, "NSFW 开关"):
        s = load_settings()
        s["nsfw_enabled"] = bool(on)
        return save_settings(s)


# ---- 人工纠正表 ----

def load_nsfw_manual() -> dict:
    """{路径: "nsfw"|"sfw"}。按文件 mtime 缓存，避免每个请求都读盘。"""
    try:
        at = NSFW_MANUAL_FILE.stat().st_mtime
    except OSError:
        at = -1.0
    with _NSFW_MANUAL_LOCK:
        if _NSFW_MANUAL["at"] == at:
            return _NSFW_MANUAL["map"]
    m = {}
    if at >= 0:
        try:
            d = json.loads(NSFW_MANUAL_FILE.read_text("utf-8"))
            if isinstance(d, dict):
                for k, v in d.items():
                    if v in ("nsfw", "sfw"):
                        m[str(k)] = v
        except (OSError, ValueError):
            m = {}
    with _NSFW_MANUAL_LOCK:
        _NSFW_MANUAL["at"], _NSFW_MANUAL["map"] = at, m
    return m


def set_nsfw_manual(path: str, label) -> dict:
    """写人工纠正；label 传 None = 撤销纠正、回到模型判定。

    这个文件是用户资产，写坏了没处恢复 —— 先写临时文件再原子换名，全程串行。
    """
    try:
        p = str(Path(path).resolve())
    except Exception:
        return {"ok": False, "error": "路径非法"}
    if label is not None and label not in ("nsfw", "sfw"):
        return {"ok": False, "error": "label 只能是 nsfw / sfw / null"}
    # A11：整段「读 → 改 → 写 → 回填」在写锁内完成。锁外读会丢更新：
    # 两个并发标注各自基于同一份旧快照整文件覆盖，后写的吃掉先写的。
    with _NSFW_MANUAL_WLOCK:
        cur = load_nsfw_manual()           # 它内部取的是缓存锁，与写锁不嵌套
        d = dict(cur)
        if label is None:
            d.pop(p, None)
        else:
            d[p] = label
        try:
            NSFW_MANUAL_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = NSFW_MANUAL_FILE.with_suffix(f".{_tmp_tag()}.tmp")
            tmp.write_text(json.dumps(d, ensure_ascii=False), "utf-8")
            tmp.replace(NSFW_MANUAL_FILE)
        except OSError as ex:
            return {"ok": False, "error": f"写入失败：{type(ex).__name__}: {ex}"}
        with _NSFW_MANUAL_LOCK:
            _NSFW_MANUAL["at"], _NSFW_MANUAL["map"] = -1.0, d   # -1 强制下次重读，拿到新 mtime
    return {"ok": True, "path": p, "label": d.get(p)}


def nsfw_manual_clear() -> dict:
    """清空人工纠正。会提示一句后果：清完这些图回到模型判定，
    下次检测可能被重新标成 NSFW —— 这是用户点按钮时该知道的。"""
    with _NSFW_MANUAL_WLOCK:                # A11：与 set_nsfw_manual 同一把写锁
        n = len(load_nsfw_manual())
        try:
            NSFW_MANUAL_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = NSFW_MANUAL_FILE.with_suffix(f".{_tmp_tag()}.tmp")
            tmp.write_text("{}", "utf-8")
            tmp.replace(NSFW_MANUAL_FILE)
        except OSError as ex:
            return {"ok": False, "error": f"写入失败：{type(ex).__name__}: {ex}"}
        with _NSFW_MANUAL_LOCK:
            _NSFW_MANUAL["at"], _NSFW_MANUAL["map"] = -1.0, {}
    return {"ok": True, "cleared": n}


# ---- 派生缓存（V2.11.3：按根目录分文件共存）----
# 旧版是单个 nsfw_index.json，只记得住「最后扫的那个根目录」——用户在多个根
# 目录之间频繁切换时，每切一次整表作废，切回来就得重扫。现在一个根目录一份
# 文件（文件名 = 规范化路径的 md5 前 16 位），来回切各自的热缓存都还在。

def _nsfw_cache_path(root: str) -> Path:
    """根目录 → 它自己的缓存文件。拿 md5 做文件名而不是直接拼路径：
    Windows 路径里的冒号反斜杠当不了文件名；且同一目录的不同写法（大小写、
    结尾斜杠）应得到同一份缓存，所以哈希前先 normcase+normpath。"""
    norm = os.path.normcase(os.path.normpath(root))
    return NSFW_DIR / (hashlib.md5(norm.encode("utf-8", "replace")).hexdigest()[:16]
                       + ".json")


def _nsfw_migrate_legacy():
    """旧版单文件缓存 → 按根目录分文件。幂等：迁移完旧文件改名 .bak 留底，
    下次进来第一行就返回。任何失败都不抛 —— 迁移失败顶多损失一次重扫，
    不能挡住服务启动（「不能 500、不能崩」同款思路）。"""
    tmp = None                  # 迁移写一半失败时要能清掉半截 tmp（并发输家走的正是这条路）
    try:
        if not NSFW_LEGACY_FILE.is_file():
            return
        d = json.loads(NSFW_LEGACY_FILE.read_text("utf-8"))
        if isinstance(d, dict):
            root = d.get("root")
            if isinstance(root, str) and root and isinstance(d.get("map"), dict):
                dst = _nsfw_cache_path(root)
                if not dst.is_file():        # 新文件已存在（同根重扫过）：以新的为准
                    NSFW_DIR.mkdir(parents=True, exist_ok=True)
                    payload = dict(d)
                    payload["v"] = 1
                    tmp = dst.with_suffix(f".{_tmp_tag()}.tmp")
                    tmp.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
                    tmp.replace(dst)
                    log(f"[nsfw] 旧版单文件缓存已迁移 -> {dst.name}")
        NSFW_LEGACY_FILE.replace(NSFW_LEGACY_FILE.parent / "nsfw_index.json.bak")
    except (OSError, ValueError) as ex:
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        log(f"[warn] 旧 NSFW 缓存迁移跳过: {ex}")


def _load_nsfw_cache(root: str, model: str):
    """返回 (map, built_at)。只读自己这根目录的文件；版本/根/档位对不上就整表
    作废 —— 换了模型档，旧分数和新分数没有可比性，混在一起会得到自相矛盾的判定。"""
    try:
        d = json.loads(_nsfw_cache_path(root).read_text("utf-8"))
    except (OSError, ValueError):
        return {}, 0.0
    if not isinstance(d, dict) or d.get("v") != 1:
        return {}, 0.0
    # 根目录比对走 normcase：Windows 下大小写、斜杠写法差异不该作废整表
    if os.path.normcase(str(d.get("root") or "")) != os.path.normcase(root) \
            or d.get("model") != model:
        return {}, 0.0
    m = d.get("map")
    if isinstance(m, dict):
        # 缓存是"用户能改的文件"，整条坏掉（手改/写崩成字符串、列表等）不能带进内存
        # 炸请求 —— 在唯一的装载口把非 dict 条目丢掉，重扫会补回来。
        # 审查实测：一条 "garbage" 就能让 /api/list 和 /api/nsfw/status 双 500，
        # 而 status 是前端每秒轮询的接口。
        m = {k: v for k, v in m.items() if isinstance(v, dict)}
    else:
        m = {}
    try:
        at = float(d.get("at") or 0.0)
    except (TypeError, ValueError):
        at = 0.0
    return m, at


def _save_nsfw_cache(mp: dict, root: str, model: str):
    """原子换名落盘，只写 root 自己的文件。失败只记日志 —— 派生数据，
    丢了重扫一遍就行，不该让用户看到报错。

    mp/root/model 由调用方显式传入，不从 NSFW[] 现读：扫描线程收尾时用户可能
    已经切走，按 NSFW 现值落盘会把 A 根目录的分数写进 B 的文件里（本版修的竞态）。
    """
    tmp = None                                   # 先占位：失败分支要清 tmp，不能引 NameError
    try:
        NSFW_DIR.mkdir(parents=True, exist_ok=True)
        dst = _nsfw_cache_path(root)
        tmp = dst.with_suffix(f".{_tmp_tag()}.tmp")
        tmp.write_text(json.dumps({"v": 1, "root": root, "model": model,
                                   "at": time.time(), "map": mp},
                                  ensure_ascii=False), "utf-8")
        # Windows 上 `os.replace` = MoveFileEx(REPLACE_EXISTING)：**只要还有别的句柄开在
        # dst 上就会失败（WinError 5 拒绝访问）**，而被开着的概率并不小 ——
        # `/api/nsfw/status` 是前端每秒轮询的，它的 nsfw_prime → _load_nsfw_cache 读的
        # 就是这个文件。实测（dev/_nsfw_flag_order_probe.py Part 5）在一次运行里就撞到
        # 两次：「[warn] NSFW 缓存写入失败: [WinError 5] ... 64be007ba60a0c27.tmp ->
        # ...64be007ba60a0c27.json」—— 整批扫描结果**一条都没落盘**，只留一行日志，
        # 用户第二天打开发现「昨天扫的都白扫了」。派生数据丢了顶多重扫，但这个重扫
        # 本来是能免掉的，所以给 3 次机会、递增退避，等读者先松手。
        last = None
        for _try in range(3):
            try:
                tmp.replace(dst)
                last = None
                break
            except OSError as ex:
                last = ex
                time.sleep(0.05 * (_try + 1))
        if last is not None:
            raise last
    except OSError as ex:
        if tmp is not None:
            try:
                tmp.unlink()                     # 换名输了（并发写/进程被杀）别留半截 tmp 在缓存目录
            except OSError:
                pass
        log(f"[warn] NSFW 缓存写入失败: {ex}")


def nsfw_prime(root: str = None):
    """把该根目录自己的缓存读进内存（切根目录时调）。只读，不触发任何推理。"""
    _nsfw_migrate_legacy()                      # 幂等，首次之后立刻返回
    root = root or STATE.get("root")
    if not root:
        return
    model = load_nsfw_model()
    mp, at = _load_nsfw_cache(root, model)
    with NSFW_LOCK:
        NSFW.update({"root": root, "model": model, "map": mp,
                     "done": len(mp), "total": len(mp),
                     "building": False, "built_at": at})


def nsfw_ctx():
    """一次请求取一份快照，循环里反复用。

    返回 `(entries, model, threshold, manual, enabled)`。

    `enabled`（总开关）一并算进来是**性能红线**（批次 F · F1）：
    `nsfw_fields()` 在逐条循环里会被调用上万次，此前它每次都自己调
    `nsfw_enabled()` → `nsfw_available()` + `load_settings()`，一次 `/api/list`
    就是 9726 次读盘，端到端从 97 ms 涨到 2.6 s。
    总开关在**一次请求内翻转**是不可能场景，所以整个循环共用一份快照是安全的。
    """
    with NSFW_LOCK:
        entries, model = NSFW["map"], NSFW["model"]
    if not model:
        # 首次请求：先把缓存与档位装进来（纯读，不触发推理）。
        # 装完 NSFW["model"] 就有值了，后续请求走上面那条零成本路径。
        nsfw_prime()
        with NSFW_LOCK:
            entries, model = NSFW["map"], NSFW["model"]
    return entries, model, load_nsfw_threshold(), load_nsfw_manual(), nsfw_enabled()


def nsfw_fields(path, mtime, size, ctx=None):
    """返回 (label, score)。

    score == -1 是「这条没有可用分数，请以 label 为准」的信号，涵盖四种情况：
    还没检测 / 检测失败 / 文件被改过 / **人工纠正**。
    前端据此决定能不能自己按阈值重算：有分数 → 阈值滑块改完立刻生效（不必重扫、不必重拉列表）；
    没分数 → 乖乖用服务端给的标签。**人工纠正必须走这条路** ——
    否则前端拿阈值一算，就能把用户明确标注过的判定翻回去。
    """
    if ctx is None:
        ctx = nsfw_ctx()
    # 严格 5 元解包（批次 F · F1）：总开关走 ctx，**不在这里**调 nsfw_enabled()。
    # 不做「兼容 4 元组」的兜底 —— 那会让「ctx 忘了带 enabled」静默退回每张读盘的
    # 慢路径（2.6 s 那个坑），宁可当场炸出来。
    entries, model, thr, manual, enabled = ctx
    if not enabled:
        return "unknown", -1.0
    m = manual.get(str(path))
    if m in ("nsfw", "sfw"):
        return m, -1.0
    e = entries.get(str(path))
    if not isinstance(e, dict) or mtime is None:   # 类型判断而非 if not e：非空 str/list 也拦得住
        return "unknown", -1.0
    try:
        if int(e.get("m", -1)) != int(mtime) or int(e.get("s", -1)) != int(size if size is not None else -1):
            return "unknown", -1.0
        if e.get("md") != model:
            return "unknown", -1.0         # 换了模型档位：旧分数没有可比性
        sc = float(e.get("sc", -1.0))
    except (TypeError, ValueError):
        return "unknown", -1.0
    return ("nsfw" if sc >= thr else "sfw"), round(sc, 6)


def _nsfw_builder(root: str, model: str, mygen: int = None):
    """后台检测线程：遍历 → 增量复用 → 推理 → 落盘。根目录切换时自行中止。"""
    myroot = root
    mygen = NSFW_GEN[0] if mygen is None else mygen

    def _superseded() -> bool:
        """本代是否已作废：换过根，或用户又发起了一次检测。

        2026-11-05 P2：收尾的几次回写原先没有任何判据 —— 循环里查到 root 变了就
        break，但「刚查完这一张、用户就切根」的窗口里，收尾仍把旧根的表写成新根的。
        """
        return NSFW_GEN[0] != mygen or STATE.get("root") != myroot
    # 扫描是低频、用户显式触发的后台任务，起止都留一行：出了「扫了但没结果」
    # 这类问题，没有这两行就只能靠猜（这次就吃过亏——中止分支静默退场，查了半天）。
    log(f"[nsfw] 扫描开始 root={myroot} model={model}")
    try:
        old, _at = _load_nsfw_cache(root, model)
        imgs = []
        stack, seen = [(str(Path(root)), 0)], set()
        try:
            seen.add(os.path.realpath(root))
        except OSError:
            pass
        while stack:
            cur, depth = stack.pop()
            try:
                with os.scandir(cur) as it:
                    for e in it:
                        try:
                            if e.is_dir():
                                if depth < 10:
                                    rp = os.path.realpath(e.path)
                                    if rp not in seen:
                                        seen.add(rp)
                                        stack.append((e.path, depth + 1))
                            elif os.path.splitext(e.name)[1].lower() in NSFW_IMG_EXTS:
                                imgs.append(e.path)
                        except OSError:
                            continue
            except OSError:
                continue
        with NSFW_LOCK:
            NSFW["total"] = len(imgs)
        log(f"[nsfw] 待检 {len(imgs)} 张")
        # 预热：把加载耗时从第一张图里挪出来。返回 False = 会话根本没建起来
        # （模型损坏 / ort 建会话失败 / 依赖缺失）。接着空转 9700 次只会白等
        # 十几分钟，最后交一份空表还报「完成」—— 当场收工，如实回报。
        if not NSFW_ENGINE.warm(model):
            _err = (NSFW_ENGINE.last_load_error(model)
                    or "检测引擎未能加载模型")
            log(f"[warn] NSFW 中止：模型加载失败 —— {_err}")
            with NSFW_LOCK:
                # X2（2026-10-28）：**失败出口不许把已有的内存表打空**。
                # 此前这里无条件写 `{}` —— 磁盘上的缓存其实一个字节没动，但内存被清，
                # 前端立刻把全库读成 unknown，「保护」当场失效，而且看不出是为什么
                # （用户看到的是「扫描中止」，不会想到防护值一起没了）。
                if not _superseded():
                    NSFW.update({"map": dict(old), "done": 0, "total": 0,
                                 "building": False, "last_error": f"模型加载失败：{_err}"})
            return
        newmap, changed = {}, False
        skipped = 0                              # predict 返回 None 的条数（坏图 / 会话半死）
        for i, p in enumerate(imgs):
            if STATE.get("root") != myroot:      # 用户切了根目录：清场放弃
                log(f"[nsfw] 中止：根目录已切换（STATE={STATE.get('root')} myroot={myroot}）")
                if newmap:
                    # V2.11.3：已扫到的部分落进自己根目录的文件，下次接着用。
                    # 表按根分文件之后这一步才安全 —— 不会污染别的根目录。
                    # ⚠️ 同收尾处：先落盘再置 building=False（宣告完成不许抢在落盘前面）。
                    _save_nsfw_cache(newmap, myroot, model)
                with NSFW_LOCK:
                    # 换代之后**别**再清场：nsfw_prime 已经把新根的表装好了，
                    # 这里清一次就等于把新根的评分一起抹掉（旧代码正是这样）。
                    if not _superseded():
                        NSFW.update({"root": None, "map": {}, "done": 0,
                                     "total": 0, "building": False})
                return
            try:
                st = os.stat(p)
                key = str(p)
                e = old.get(key)
                if (e and int(e.get("m", -1)) == int(st.st_mtime)
                        and int(e.get("s", -1)) == st.st_size
                        and e.get("md") == model):
                    newmap[key] = e              # 没变且同档位：直接复用，不重跑推理
                else:
                    r = NSFW_ENGINE.predict(p, model)
                    if r is None:
                        # 坏图/解码失败：跳过（读时自然落成 unknown）。但**必须留痕** ——
                        # 整批零结果时，这里是唯一能事后归因的地方。逐条只在头几张打，
                        # 之后交给收工汇总，避免坏图批量出现时刷屏。
                        skipped += 1
                        if skipped <= _NSFW_SKIP_LOG_MAX:
                            log(f"[warn] NSFW 推理返回空 #{skipped} "
                                f"{p}（{NSFW_ENGINE.last_load_error(model) or '该图解码失败'}）")
                        continue
                    newmap[key] = {"m": int(st.st_mtime), "s": st.st_size, "md": model,
                                   "sc": r["sc"], "tp": r["top"]}
                    changed = True
            except Exception as ex:              # 单张出错不得拖垮整批（_index_builder 同款兜底）
                log(f"[warn] NSFW 跳过 {os.path.basename(p)}: {type(ex).__name__}: {ex}")
                continue
            with NSFW_LOCK:
                NSFW["done"] = i + 1
            # 每 500 张把表露出去一次：进度条、以及「边扫边出结果」都靠它。
            # 公布的是**部分表**（还没轮到的读成 unknown）而不是旧表 —— 诚实一点，
            # 反正单张 2 ms，全库也就十几秒，没必要为这点时间差维护两套表。
            if changed and (i + 1) % 500 == 0 and not _superseded():
                with NSFW_LOCK:
                    NSFW["map"] = dict(newmap)
        # 「跑完但零结果」是这条链路最坏的形态：进度照走满、状态报「完成」，
        # 用户以为保护在生效。按失败率如实留痕，交给 nsfw_status 带出去。
        _lerr = ""
        if skipped and not newmap:
            _lerr = (f"全部 {skipped} 张推理失败（模型不可用或图片解码异常），"
                     f"本次未产出任何结果")
            log(f"[warn] NSFW 扫描结束但零结果：{_lerr}")
        elif skipped:
            _lerr = f"{skipped} 张推理失败已跳过（共成功 {len(newmap)} 张）"
            log(f"[warn] NSFW 扫描完成：{len(newmap)} 张成功，{_lerr}")
        # 2026-11-05 P2：收尾前的最终闸 —— 本代已被换根 / 重新发起取代就整份丢掉。
        # 落盘也不能做：`_save_nsfw_cache` 是按 myroot 分文件的，写的是旧根自己的
        # 缓存（其实无害），但下面「宣告完成」会把新根的 building 改成 False。
        if _superseded():
            log(f"[nsfw] 扫描结果作废：期间已换根或重新发起（myroot={myroot}）")
            return
        # ⚠️ X2（2026-10-28）：**零结果一律不许覆盖非空旧表**（与 _index_builder 同款闸）。
        # 三个触发面，症状不同、结果一样 —— 旧评分被空表顶掉，全库读成 unknown：
        #   ① 一张都没扫到（外接盘休眠 / 无权限 / 根目录失效）⇒ imgs 为空、skipped 恒 0，
        #      上面 `if skipped and not newmap` 根本不进 ⇒ last_error 留空串，
        #      界面显示「扫描完成 0 张」，用户只会以为自己的图丢了；
        #   ② 全部推理失败 ⇒ skipped=N 虽有 last_error，但表已被空表覆盖，下次还得重扫全库；
        #   ③ 模型加载失败（上面那个分支）⇒ 内存表被置空而磁盘没动，两边从此不一致。
        # 这里只管 ①②：磁盘不写、内存回填旧表，让「内存 == 磁盘」这条不变量始终成立。
        # 不拦「部分结果」：用户真的删了一半图是合法操作，少了的 key 本来就该消失。
        if not newmap and old:
            _why = ("一张图都没扫到（盘未挂载 / 无权限 / 根目录失效）" if not imgs
                    else f"全部 {skipped} 张推理失败（模型不可用或图片解码异常）")
            log(f"[error] NSFW 扫描零结果 —— 拒绝覆盖旧评分（{len(old)} 条）。原因：{_why}")
            with NSFW_LOCK:
                NSFW["map"] = dict(old)
                NSFW["built_at"] = time.time()
                NSFW["last_error"] = f"本次扫描零结果，已保留上次结果 — {_why}"
                NSFW["building"] = False
            return
        # ⚠️ 落盘的顺序有讲究：**先落盘、后宣告「收工」**（`building=False`）。
        # 反过来写（本版修的就是这个）会让「宣告完成」与「文件可用」之间留一段窗口 ——
        # 消费者在窗口里读到的是「扫完了，可它的派生缓存还读不到」。两条实测证据：
        #   ① 门禁 tests/nsfw_check.py H 段：轮询到 building=False 立刻去查两个根目录的
        #      缓存文件 ⇒ 命中窗口时 fB=None（2026-10-09 批量回归里翻红一次，重跑难复现）；
        #   ② dev/_nsfw_flag_order_probe.py：把 _save_nsfw_cache 卡住不动，此刻
        #      building 已经是 False（确定性，不是概率）。
        # 注意 `NSFW["map"]` 与旗标一起后移：对外可见的结果与「已完成」保持同一时点。
        # 显式带 myroot/model：此刻用户可能已切走，绝不能按 NSFW 现值落盘
        # 2026-10-27（代码梳理 O2）：**没有变化就不落盘**。
        # 自动补测每 NSFW_AUTO_INTERVAL（60 s）跑一次全树 scandir；此前无论有没有
        # 新图都会把整份表重写一遍（建库后长期每 60 秒一次全表序列化 + 落盘）。
        # 未变条目是直接复用 `old` 里的对象（`newmap[key] = e`），所以整表相等
        # 就等价于「这一轮没有任何产出」，跳过落盘不丢任何信息。
        # 注意：这里只省写盘，不改「先落盘、后宣告 building=False」的顺序。
        if newmap != old:
            _save_nsfw_cache(newmap, myroot, model)
        with NSFW_LOCK:
            NSFW["map"] = newmap
            NSFW["built_at"] = time.time()
            NSFW["last_error"] = _lerr
            NSFW["building"] = False
        log(f"[nsfw] 扫描完成 {len(newmap)} 张")
    except Exception as ex:
        log(f"[warn] NSFW 扫描异常: {type(ex).__name__}: {ex}")
        with NSFW_LOCK:
            if not _superseded():
                NSFW["building"] = False
                NSFW["last_error"] = f"扫描异常：{type(ex).__name__}: {ex}"


def ensure_nsfw(auto_only: bool = True) -> bool:
    """启动后台检测。

    auto_only=True（默认，挂在既有轮询里）只在「这个根目录已经扫过」时做增量补测 ——
    出图后不用手动点，但首次打开软件**绝不会**把 CPU 打满。全库首扫必须由用户点。
    """
    root = STATE.get("root")
    if not root or not nsfw_enabled():
        return False
    now = time.time()
    model = load_nsfw_model()
    with NSFW_LOCK:
        t = NSFW_THREAD[0]
        if t and t.is_alive():
            return False
        if NSFW["root"] != root:
            return False                          # 换过根目录：等 nsfw_status 先 prime
        if not NSFW.get("built_at"):
            return False                          # 从没扫过：等用户点「开始检测」
        if now - NSFW["built_at"] < NSFW_AUTO_INTERVAL:
            return False
        NSFW_GEN[0] += 1                 # 换代：上一轮即使还在收尾也不许写回
        NSFW.update({"model": model, "done": 0, "total": 0, "building": True})
        t = threading.Thread(target=_nsfw_builder,
                             args=(root, model, NSFW_GEN[0]), daemon=True)
        NSFW_THREAD[0] = t
        t.start()          # 同 ensure_index：登记与启动同一临界区，防并发各起一条
    return True


def nsfw_start(force: bool = False) -> dict:
    """用户点「开始检测 / 重新检测」。force=True 先丢掉派生缓存（人工纠正不动）。"""
    if not nsfw_available():
        return {"ok": False, "error": nsfw_reason() or "检测引擎不可用"}
    root = STATE.get("root")
    if not root:
        return {"ok": False, "error": "尚未设置根目录"}
    with NSFW_LOCK:
        if NSFW["building"]:
            return {"ok": True, "building": True, "msg": "已在检测中"}
    cleared = nsfw_clear_cache() if force else 0
    model = load_nsfw_model()
    with NSFW_LOCK:
        # 清缓存与载模型都在锁外，并发点两次可能双双穿过上面那道 building 预检；
        # 这里必须再挡一次，否则同一档位会各起一条推理线程、两条都写 NSFW["map"]。
        t = NSFW_THREAD[0]
        if NSFW["building"] or (t and t.is_alive()):
            return {"ok": True, "building": True, "msg": "已在检测中"}
        NSFW_GEN[0] += 1                 # 换代：上一轮即使还在收尾也不许写回
        NSFW.update({"root": root, "model": model, "done": 0, "total": 0,
                     "building": True, "map": {} if force else dict(NSFW["map"]),
                     "last_error": ""})
        t = threading.Thread(target=_nsfw_builder,
                             args=(root, model, NSFW_GEN[0]), daemon=True)
        NSFW_THREAD[0] = t
        t.start()
    return {"ok": True, "building": True, "cleared": cleared, "model": model}


def nsfw_clear_cache() -> int:
    """丢弃当前根目录的派生缓存（人工纠正表不动，其他根目录的缓存也不动 ——
    V2.11.3 起按根分文件，「重新检测」清的是自己这份）。返回丢掉的条数。"""
    root = STATE.get("root")
    if not root:
        return 0
    f = _nsfw_cache_path(root)
    n = 0
    try:
        n = len(json.loads(f.read_text("utf-8")).get("map") or {})
    except (OSError, ValueError, AttributeError):
        n = 0
    try:
        f.unlink()
    except OSError:
        pass
    return n


# ---- 根目录变更迁移（V2.12.0）----
# 场景：D:\图库 被移动/改名/换了盘符。此时缓存文件名（md5(根)）变了、map 键和
# 人工纠正/收藏/标签/标记/最近的键全对不上 —— 数据都在盘上，只是找不回来。
# 思路：拿新根下的文件清单拼回旧路径查旧表，size 一致算指纹命中；命中够多就
# 认定「只是搬了家」，把六张表的键整体重写到新根。目录结构变过（命中率低）
# 不自动迁，走设置页手动兜底（用户指定旧根，跳过门槛、保留逐条验证）。


def _dir_cmp(p: str) -> str:
    """前缀比较用的规范化形式：末尾补一个分隔符。
    补了才等价于「整段目录名相等」—— 否则 `D:\\lib` 会命中 `D:\\library`；
    先 rstrip 再补则让盘根（`D:\\`）也只留一个分隔符，不会拼成 `D:\\\\`。"""
    return os.path.normcase(os.path.normpath(p)).rstrip("\\/") + os.sep


def _migrate_key_prefix(table: dict, old_root: str, new_root: str):
    """dict 形态路径表（manual/favs/tags/marks 共用）的前缀重写。
    逐条 isfile 验证：验证不过的条目**原样保留**而不是丢弃 —— 键迁错比键失配
    更糟（失配顶多显示不出来，迁错会张冠李戴）。返回 (新表, 迁移条数, 冲突条数)。"""
    old_n, new_n = os.path.normpath(old_root), os.path.normpath(new_root)
    pre_old = _dir_cmp(old_n)
    out, n, conflicts = {}, 0, 0
    for k, v in table.items():
        kn = os.path.normpath(str(k))
        if os.path.normcase(kn).startswith(pre_old):
            nk = new_n + os.sep + kn[len(old_n) + 1:]
            if os.path.isfile(nk):
                # B5-P2-3（2026-10-17）：目标键已经在表里 ⇒ **保留新根下的现值**。
                # 原先 `out[nk] = v` 直接覆盖，谁赢取决于 dict 遍历顺序，现值可能
                # 无声消失、报告还说「成功 N 条」。源键也不写回（它指向已被搬走的
                # 旧路径，留着只是一条永远显示不出来的死键）。
                if nk in out:
                    conflicts += 1
                    continue
                out[nk] = v
                n += 1
                continue
        key_s = str(k)
        if key_s in out:
            # 反过来的遍历顺序：先迁过来的值已占了这位，现在是**现值**后到。
            # 同样「现值优先」—— 换成现值，并记一次冲突（否则结果对但不说）。
            conflicts += 1
        out[key_s] = v
    return out, n, conflicts


def _migrate_recent_prefix(items: list, old_root: str, new_root: str):
    """recent 表是 list（[{path,...}]），键在 value 里，单独一份重写。
    返回 (新表, 迁移条数, 冲突条数)；冲突语义同 `_migrate_key_prefix`。"""
    old_n, new_n = os.path.normpath(old_root), os.path.normpath(new_root)
    pre_old = _dir_cmp(old_n)
    # 现值集合：表里本来就有的 path，迁移结果撞上它就是冲突（保留现值那一条）。
    # 反向遍历（现值的条目后到）会留下两条同路径记录 —— recent 是易失的使用记录，
    # 重复一条只多一次画面，不影响任何功能，故不为它做二次去重（有意取舍）。
    live = {os.path.normcase(os.path.normpath(str((e or {}).get("path") or "")))
            for e in items if isinstance(e, dict) and (e or {}).get("path")}
    out, n, conflicts = [], 0, 0
    for e in items:
        p = str((e or {}).get("path") or "")
        kn = os.path.normpath(p)
        if p and os.path.normcase(kn).startswith(pre_old):
            nk = new_n + os.sep + kn[len(old_n) + 1:]
            if os.path.isfile(nk):
                cnk = os.path.normcase(os.path.normpath(nk))
                if cnk in live:
                    conflicts += 1
                    continue
                live.add(cnk)
                e2 = dict(e)
                e2["path"] = nk
                out.append(e2)
                n += 1
                continue
        out.append(e)
    return out, n, conflicts


def _migrate_write_json(path: Path, obj) -> bool:
    """迁移专用落盘：先把现文件留一份 .mig-bak 再原子覆盖。
    这些表全是用户资产（收藏/标签没有任何重建途径），bak 是最后的保险；
    bak 写不出来不拦迁移 —— 此刻原文件一个字节都还没动，失败可重跑（幂等）。

    返回 True = 有一份**迁移前**的备份可用（或本来就没有文件可备份）；
    False = 备份没写成，调用方据此在报告里带 `bak_failed`，前端能提示。

    B5-P2-4（2026-10-17）：两处修正 ——
    ① 备份只在 .mig-bak **不存在时**写。原先每次迁移都重写 bak，而手动迁移可
       无限次触发 ⇒ 第二次备份的是「已经迁过一次」的表，迁移前的原始状态再也
       找不回来，恰好废掉了 bak 作为「最后保险」的唯一价值。
    ② 备份失败不再 `except OSError: pass` 吞掉：写一条 warn 日志，并把 False
       带回去。静默失败比没有备份更糟 —— 用户以为有退路。
    """
    bak_ok = True
    if path.is_file():
        bak = path.with_suffix(path.suffix + ".mig-bak")
        if bak.exists():
            bak_ok = True          # 更早那份才是原始状态，保留它，别覆盖
        else:
            try:
                bak.write_bytes(path.read_bytes())
            except OSError as ex:
                bak_ok = False
                log(f"[warn] 迁移备份写入失败（{path.name}）：{ex} —— 本次迁移没有可回退的备份")
    if not _save_json(path, obj):
        raise WriteError(f"{path.name}写入失败（磁盘不可写或文件被占用）")
    return bak_ok


def _migrate_save(path: Path, obj, report: dict) -> None:
    """落盘并把「没有备份」记进报告（report["bak_failed"] 是文件名列表）。"""
    if not _migrate_write_json(path, obj):
        report["bak_failed"].append(path.name)


def _migrate_sample(new_root: str) -> list:
    """新根的指纹样本 [(相对路径, mtime, size)]。直接走文件系统而不是等提示词
    索引建好：索引比它慢一个数量级，迁移要赶在用户反应过来之前完成，等不起。"""
    out = []
    base = os.path.normpath(new_root)
    try:
        for cur, dirs, files in os.walk(base):
            dirs[:] = dirs[:64]          # 变态深的树抽样够用了，别为一两个环白烧时间
            for name in files:
                if os.path.splitext(name)[1].lower() in NSFW_IMG_EXTS:
                    p = os.path.join(cur, name)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    out.append((os.path.relpath(p, base), int(st.st_mtime), st.st_size))
                    if len(out) >= NSFW_MIGRATE_SAMPLE:
                        return out
    except OSError:
        pass
    return out


def _migrate_read_cache(root: str):
    """读某根的缓存文件，不管档位。迁移是搬运不是判定 —— 档位匹配是读端
    （_load_nsfw_cache）的职责，这里过滤会把「换根+换档」的用户挡在门外。"""
    try:
        d = json.loads(_nsfw_cache_path(root).read_text("utf-8"))
    except (OSError, ValueError):
        return {}, ""
    if not isinstance(d, dict) or d.get("v") != 1:
        return {}, ""
    m = d.get("map")
    return (m if isinstance(m, dict) else {}), str(d.get("model") or "")


def _migrate_score(sample: list, old_root: str, old_map: dict) -> int:
    """按「拼回旧路径后 size 一致」计命中。mtime 不参与计分：复制、网盘回迁都
    可能动 mtime，把它卡死会把最需要救援的用户挡在门外（迁移时会用 stat 对齐）。"""
    base = os.path.normpath(old_root)
    idx = {}
    for k in old_map:
        idx[os.path.normcase(os.path.normpath(str(k)))] = k
    hits = 0
    for rel, _mt, size in sample:
        e = old_map.get(idx.get(os.path.normcase(os.path.normpath(
            base + os.sep + str(rel))), ""))
        try:
            if isinstance(e, dict) and e.get("s") is not None and int(e["s"]) == size:
                hits += 1
        except (TypeError, ValueError):
            continue
    return hits


def _migrate_candidates(new_root: str):
    """候选旧根 = 数据目录里每个 NSFW 缓存文件的 root 字段（map 顺手带回：
    打分和迁移用同一份材料，不用读两遍）。当前根自己的文件不算候选。"""
    want = os.path.normcase(os.path.normpath(new_root))
    try:
        files = sorted(NSFW_DIR.glob("*.json"))
    except OSError:
        return
    for f in files:
        try:
            d = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict) or d.get("v") != 1:
            continue
        r = str(d.get("root") or "")
        if not r or os.path.normcase(os.path.normpath(r)) == want:
            continue
        m = d.get("map")
        if isinstance(m, dict) and m:
            yield r, m, str(d.get("model") or "")


def _migrate_run(new_root: str, old_root: str = None, manual: bool = False) -> dict:
    """执行一次迁移。old_root 缺省时自动选候选（打分最高且过门槛）；
    manual=True 由设置页显式指定旧根 —— 跳过门槛（用户说迁就迁），验证照做。

    全程 copy 语义：所有旧文件一律不动，迁错了把目录改回去就复原。
    落盘顺序是刻意的：用户数据表先写，NSFW 缓存文件**最后**写 —— 它存在
    就代表迁移完整走完，自动检测靠这个判断「要不要再试」。
    """
    report = {"ok": False, "migrated": False, "new_root": new_root,
              "old_root": old_root or "", "cache": 0, "manual": 0,
              "favs": 0, "tags": 0, "marks": 0, "recent": 0,
              # B5（2026-10-17）：conflicts = 新旧目录下同一张图各有记录、保留了
              # 现值（旧值没并进来）的条数；bak_failed = 迁移前没备份成的表名。
              # 两个都是「办成了但有损失」，必须回传，不能只留在日志里。
              "conflicts": 0, "bak_failed": []}
    try:
        # ---- 自动模式：选候选 + 门槛（old_root 在这里才确定，路径规范化必须放在它后面）----
        old_map, old_model = {}, ""
        if not manual:
            sample = _migrate_sample(new_root)
            if not sample:
                report["error"] = "新目录里没有可核对的图片"
                return report
            best = None
            for cand_root, cand_map, cand_model in _migrate_candidates(new_root):
                h = _migrate_score(sample, cand_root, cand_map)
                if best is None or h > best[0]:
                    best = (h, cand_root, cand_map, cand_model)
            if best is None:
                report["error"] = "没有可用的旧缓存"
                return report
            h, cand_root, cand_map, cand_model = best
            # 小库按比例放宽下限：总共 8 张的库不可能凑够 10 个命中
            need = max(1, min(NSFW_MIGRATE_MIN_HITS,
                              int(len(sample) * NSFW_MIGRATE_RATIO)))
            if h < need or h / len(sample) < NSFW_MIGRATE_RATIO:
                # 明确告诉调用方「不是搬家」——自动入口据此记住不再重试
                report["reason"] = f"指纹命中率不足（{h}/{len(sample)}，需 {need}）"
                report["ok"] = True
                return report
            old_root, old_map, old_model = cand_root, cand_map, cand_model
        else:
            old_map, old_model = _migrate_read_cache(old_root)
        report["old_root"] = old_root

        old_n, new_n = os.path.normpath(old_root), os.path.normpath(new_root)
        old_c, new_c = _dir_cmp(old_n), _dir_cmp(new_n)
        if old_c == new_c:
            report["error"] = "新旧路径相同，无需迁移"
            return report
        # B5-P2-2（2026-10-17）：两者是包含关系时**必须拒绝**。旧根是新根的父目录
        # 时（旧填 D:/lib、当前根 D:/lib/2026），`旧根 + 后缀` 会同时匹配到新根
        # 自己的条目，把它们再拼一层前缀 —— 收藏/标签整批指向不存在的路径，用户
        # 看到的是「收藏全空了」，而报告还是一片成功。反过来（旧根在新根里面）
        # 同理，且还会把新根外的条目卷进来。这种迁移没有正确结果，只能不做。
        if old_c.startswith(new_c) or new_c.startswith(old_c):
            report["error"] = ("新旧目录存在包含关系（一个在另一个里面），迁移会把路径越拼越深，已拒绝。"
                               "请换一个与当前目录互不包含的旧目录。")
            return report

        # ---- P2（V2.12.1）检查点①：切根即中止。用户切走 = 放弃这个根，别再动
        # 他的表。检查点必须压在任何写盘之前；这里过了之后到写缓存之间还有检查点②
        # 兜底。中止不记 skip（用户可能切回来，30s 后轮询会再试）。----
        if STATE.get("root") != new_root:
            report["aborted"] = True
            report["reason"] = "根目录已切换，迁移中止"
            report["ok"] = True
            return report

        # ---- ① 用户数据表：前缀重写（验证不过的条目原样保留）----
        # P1（V2.12.1）：快照→重写→落盘必须整段在同一把表锁里。旧写法快照在锁外、
        # 只给写盘上锁，3000 键的 isfile 循环把窗口拉到数百毫秒 —— 审计探针实测
        # 窗口内 40 次收藏 toggle 被 40/40 整表覆盖。锁内化后与 toggle_fav（同锁
        # 读写）串行，谁也不吞谁；load_* 本身不拿锁，锁内调用不会死锁。
        # manual 例外：load_nsfw_manual 内部自己拿 _NSFW_MANUAL_LOCK，锁内再调会
        # 递归死锁 —— 保持锁外快照（表小、写频极低，毫秒级窗口可接受）。
        man = load_nsfw_manual()
        man2, report["manual"], c_man = _migrate_key_prefix(man, old_root, new_root)
        report["conflicts"] += c_man
        if report["manual"]:
            with _NSFW_MANUAL_LOCK:
                _migrate_save(NSFW_MANUAL_FILE, man2, report)
                _NSFW_MANUAL["at"] = -1.0          # 失效缓存，下个请求拿到新表

        with _store_lock(FAV_LOCK, "数据目录迁移"):
            favs2, report["favs"], c_fav = _migrate_key_prefix(load_favs(), old_root, new_root)
            report["conflicts"] += c_fav
            if report["favs"]:
                _migrate_save(FAV_FILE, favs2, report)

        with _store_lock(TAGS_LOCK, "数据目录迁移"):
            tags2, report["tags"], c_tag = _migrate_key_prefix(load_tags(), old_root, new_root)
            report["conflicts"] += c_tag
            if report["tags"]:
                _migrate_save(TAGS_FILE, tags2, report)

        with _store_lock(MARKS_LOCK, "数据目录迁移"):
            marks2, report["marks"], c_mark = _migrate_key_prefix(load_marks(), old_root, new_root)
            report["conflicts"] += c_mark
            if report["marks"]:
                _migrate_save(MARKS_FILE, marks2, report)

        # recent 是易失性的使用记录（丢了无感），不为它上锁 —— 重写窗口内并发的
        # recent_add 可能被覆盖，可接受（V2.12.1 审计定级：不修）。
        rec, report["recent"], c_rec = _migrate_recent_prefix(load_recent(), old_root, new_root)
        report["conflicts"] += c_rec
        if report["recent"]:
            _migrate_save(RECENT_FILE, rec, report)

        # ---- ② NSFW 派生缓存：键重写 + mtime 对齐（读端 nsfw_fields 卡 mtime）----
        # P2 检查点②：用户表已动、缓存还没写 —— 这中途切根就收手（已写的表不回滚，
        # copy 语义 + 30s 重试自愈）。P16：builder 正在扫就让路，它收尾会落一份
        # 全新的完整缓存，迁移半路插一份旧分数只会跟新结果打架。
        with NSFW_LOCK:
            _building = bool(NSFW["building"])
        if _building or STATE.get("root") != new_root:
            report["aborted"] = True
            report["reason"] = ("检测进行中，迁移只迁了用户数据表" if _building
                                else "根目录已切换，迁移中止")
            report["ok"] = True
            return report
        if old_map:
            pre_old = _dir_cmp(old_n)
            new_map = {}
            for k, e in old_map.items():
                if not isinstance(e, dict):
                    continue
                kn = os.path.normpath(str(k))
                if not os.path.normcase(kn).startswith(pre_old):
                    continue                       # 别的根的条目：不掺和
                nk = new_n + os.sep + kn[len(old_n) + 1:]
                try:
                    st = os.stat(nk)               # isfile + 新 mtime 一次拿到
                except OSError:
                    continue                       # 新根下没这文件：留在旧文件里
                if nk in new_map:                  # B5-P2-3：同一目标只留先到的那条
                    report["conflicts"] += 1
                    continue
                e2 = dict(e)
                e2["m"] = int(st.st_mtime)         # 复制/回迁会动 mtime：对齐新值，
                new_map[nk] = e2                   # 否则全表会被打成 unknown 白迁一趟
                report["cache"] += 1
            if report["cache"]:
                # model 沿用旧文件的档位：行为等同「同一份缓存换了个根」，
                # 之后换档作废走既有逻辑，迁移不掺和档位的事
                _model = old_model or load_nsfw_model()
                # 4.1-5（2026-11-05 审计）：**不能整份覆盖新根已有的缓存**。
                # 新根可能此前已经扫过一份（先扫后迁 / 手工迁移），那份分数是针对
                # 新根文件实测出来的，比「旧根分数 + 对齐 mtime」更可信；直接
                # replace 会把它一起抹掉，用户只能再全库重扫一遍。
                # 合并原则：新根已有条目优先，迁移表只补缺位（update 顺序即优先级）。
                _cur, _ = _load_nsfw_cache(new_root, _model)
                if _cur:
                    _merged = dict(new_map)
                    _merged.update(_cur)
                    new_map = _merged
                _save_nsfw_cache(new_map, new_root, _model)

        if not any(report[k] for k in ("cache", "manual", "favs", "tags", "marks", "recent")):
            report["reason"] = "没有可迁移的条目"
            report["ok"] = True
            return report
        report["ok"] = True
        report["migrated"] = True
        return report
    except Exception as ex:                        # 迁移失败不能 500：报告里如实带回
        report["error"] = f"{type(ex).__name__}: {ex}"
        return report


def _migrate_auto_worker(new_root: str):
    with _MIGRATE_LOCK:
        if _MIGRATE_STATE["running"]:
            return
        _MIGRATE_STATE["running"] = True
    try:
        r = _migrate_run(new_root, manual=False)
        if r.get("migrated"):
            with _MIGRATE_LOCK:
                _MIGRATE_STATE["done"] = r
            log(f"[migrate] 已从旧目录找回数据: {r}")
            # P13（V2.12.1）：迁移只写盘不刷内存 —— 内存那份是切根时 prime 的，
            # 那时候缓存文件还不存在，读到的必然是空表。不在这里补 prime，找回的
            # 分数要等用户再切一次根（或重启）才上屏。扫描进行中不刷：builder 收尾
            # 有自己的落盘节奏，prime 半路改 building/map 会打架（prime 与用户点
            # 扫描的极小竞态窗口最多导致重扫一遍，可接受）。
            with NSFW_LOCK:
                _building = bool(NSFW["building"])
            if not _building and STATE.get("root") == new_root:
                nsfw_prime(new_root)
        elif r.get("error"):
            log(f"[warn] 根目录迁移失败: {r.get('error')}")
        elif r.get("reason"):
            if r.get("aborted"):
                # 切根/检测中是暂态：不记 skip，用户切回来（或扫完）后下一拍就会再试
                log(f"[migrate] 迁移中止: {r.get('reason')}")
            else:
                # 命中率不足 = 真的是个新目录，不是搬家。记住这个根不再重试，
                # 否则 stats 每隔 30 秒就会白扫一遍文件系统。
                _MIGRATE_STATE["skip"] = os.path.normcase(os.path.normpath(new_root))
                log(f"[migrate] 跳过迁移: {r.get('reason')}")
    finally:
        with _MIGRATE_LOCK:
            _MIGRATE_STATE["running"] = False


def root_migrate_maybe():
    """stats 轮询每拍都调。发现「当前根没有缓存文件」才考虑迁移 —— 有缓存说明
    这个根路径没变过（或上次迁移已完整做完），一分钟节流防止把轮询变成扫描器。"""
    root = STATE.get("root")
    if not root or not NSFW_DIR.is_dir():
        return
    try:
        if _nsfw_cache_path(root).is_file():
            return
    except OSError:
        return
    if _MIGRATE_STATE.get("skip") == os.path.normcase(os.path.normpath(root)):
        return
    now = time.time()
    with _MIGRATE_LOCK:
        if _MIGRATE_STATE["running"]:
            return
        if now - _MIGRATE_STATE["last_try"] < 30.0:
            return
        _MIGRATE_STATE["last_try"] = now
    threading.Thread(target=_migrate_auto_worker, args=(root,), daemon=True).start()


def nsfw_status() -> dict:
    """设置面板与前端启动查询用。扫描进行中不统计 counts ——
    进度轮询每隔一秒来一次，全表遍历一次没必要。"""
    root = STATE.get("root")
    with NSFW_LOCK:
        building = bool(NSFW["building"])
        stale = (NSFW["root"] != root)
    if root and stale and not building:
        nsfw_prime(root)                        # 换过根目录：先把这份缓存读进来
    with NSFW_LOCK:
        building = bool(NSFW["building"])
        done, total = int(NSFW["done"]), int(NSFW["total"])
        model = NSFW["model"] or load_nsfw_model()
        entries = NSFW["map"]
        last_error = NSFW.get("last_error", "")
    thr = load_nsfw_threshold()
    counts = {"nsfw": 0, "sfw": 0, "unknown": 0}
    if not building and entries:
        manual = load_nsfw_manual()
        seen = set()
        for k, e in entries.items():
            seen.add(k)
            lb = manual.get(k)
            if lb not in ("nsfw", "sfw"):
                if not isinstance(e, dict):
                    lb = "unknown"      # 坏条目按没分数算：这个接口前端每秒轮询，炸不得
                else:
                    try:
                        lb = "nsfw" if float(e.get("sc", -1.0)) >= thr else "sfw"
                    except (TypeError, ValueError):
                        lb = "unknown"
            counts[lb] = counts.get(lb, 0) + 1
        for k, lb in manual.items():
            if k not in seen:
                counts[lb] = counts.get(lb, 0) + 1
    tiers = []
    if NSFW_ENGINE:
        try:
            tiers = NSFW_ENGINE.available_models()
        except Exception:
            tiers = []
    return {
        "available": nsfw_available(),
        "reason": nsfw_reason(),
        "enabled": nsfw_enabled(),
        "model": model,
        # B4-P2-4：设置里存的档位 vs 实际生效的档位。两者不一致时前端才能提示
        # 「你选的 precise 用不了，正在用 light 跑」，而不是让人以为精确档生效中。
        "requested_model": _NSFW_MODEL_REPORT["want"],
        "model_fallback": _NSFW_MODEL_REPORT["why"],
        "threshold": thr,
        "threshold_range": list(NSFW_THRESHOLD_RANGE),
        "tiers": tiers,
        "models_dir": str(NSFW_MODELS_DIR),
        "building": building,
        "done": done,
        "total": total,
        "counts": counts,
        "last_error": last_error,   # P1：模型加载失败 / 整批推理失败；空串=正常
        "manual": len(load_nsfw_manual()),
    }


def strip_png_meta(data: bytes, warn=None) -> bytes:
    """剔除 PNG 的元数据文本块（tEXt/zTXt/iTXt/eXIf/tIME），像素数据原样保留。

    纯字节级手术不重编码；任何结构异常时原样返回（fail-safe）。

    ⚠️ 「fail-safe」不等于「可以静默」：B5-P2-1（2026-10-17）之前，任意一个块的
    长度字段越界就 `return data` —— 连**前面已经扫到**的 tEXt 也一起放弃，接口却
    照样报「已抹除」。现在改为「剥离已扫到的部分 + 剩余字节原样追加」，并把原因
    写进 warn，让调用方（strip_meta_checked）如实回报「没抹干净」。
    """
    w = warn if warn is not None else []
    try:
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            w.append("不是 PNG（缺 89 PNG 头）")
            return data
        out = bytearray(data[:8])
        i = 8
        ended = False
        while i + 12 <= len(data):
            ln = int.from_bytes(data[i:i + 4], "big")
            ctype = data[i + 4:i + 8]
            if ln > len(data) - i - 12:
                w.append("结构异常：块长度越界，只剥离了已扫到的部分")
                out += data[i:]
                return bytes(out)
            if ctype not in (b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"):
                out += data[i:i + 12 + ln]
            i += 12 + ln
            if ctype == b"IEND":
                ended = True
                break
        if not ended:
            w.append("结构异常：没找到 IEND，只剥离了已扫到的部分")
            out += data[i:]
        return bytes(out)
    except Exception:
        w.append("剥离过程异常")
        return data


# JPEG 里**没有长度字段**的标记：TEM(0x01) 与 RSTn(0xD0~0xD7)
_JPG_NOLEN = frozenset((0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7))
# 要丢弃的元数据段：APP1(0xE1 = EXIF / XMP) · APP13(0xED = IPTC) · COM(0xFE)
_JPG_DROP = frozenset((0xE1, 0xED, 0xFE))
# ⚠️ **APP2(0xE2) 绝不能进这张表** —— 它是 ICC 色彩配置文件，丢了直接改色。
#    （审计建议顺手把 E2/EB/EF 一并丢，已否决：E2=ICC 会改色，属真实回归。）


def strip_jpg_meta(data: bytes, warn=None) -> bytes:
    """剔除 JPEG 的元数据段（APP1=EXIF/XMP、APP13=IPTC、COM 注释），像素原样保留。

    纯字节级手术：熵编码数据逐字节照抄（FF00 字节填充、RSTn 重启动标记都原样
    保留），不重编码。

    B5-P1-1（2026-10-17）：原先一遇 `FFDA`（SOS）就把**剩余全部字节**拷走并
    return —— 可 JPEG 语法允许扫描数据之后、EOI 之前再出现标记段（部分转码器 /
    拼接工具产出的就是这种排布），那些段里的 EXIF/XMP 原封不动留在了「已抹除」的
    产物里。现在改为**沿熵编码数据继续找标记**，遇到标记段照常判。
    B5-P1-2（2026-10-17）：原先一遇 `FFD9`（EOI）就 return —— MPO / 相机双图 JPEG
    的第二张被**整段截断**（丢数据），第二张自带的 EXIF 也一并残留。现在 EOI 之后
    继续找后继的 `FFD8`，找到就递归走同一套剥离再拼接；找不到的尾部字节**原样保留**
    （宁可留着，也不替用户丢字节）。
    结构异常不再「静默返回原字节」：原因写进 warn，调用方据此如实报「未抹干净」。
    """
    w = warn if warn is not None else []
    try:
        if data[:2] != b"\xff\xd8":
            w.append("不是 JPEG（缺 FFD8 头）")
            return data
        out = bytearray(b"\xff\xd8")
        n = len(data)
        i = 2
        ended = False
        while i < n:
            if data[i] != 0xFF:
                out.append(data[i])              # 熵编码数据里的普通字节，照抄
                i += 1
                continue
            k = i
            while k + 1 < n and data[k + 1] == 0xFF:
                k += 1                           # FF 填充字节
            if k + 1 >= n:
                w.append("结构异常：文件在标记中途结束")
                out += data[i:]
                return bytes(out)
            m = data[k + 1]
            if m == 0x00:                        # 字节填充：FF00 是一个字面 FF
                out += data[i:k + 2]
                i = k + 2
                continue
            if m in _JPG_NOLEN:                  # TEM / RSTn：没有长度字段
                out += data[i:k + 2]
                i = k + 2
                continue
            if m == 0xD9:                        # EOI
                out += data[i:k + 2]
                i = k + 2
                ended = True
                break
            if k + 4 > n:
                w.append("结构异常：标记段缺长度字段")
                out += data[i:]
                return bytes(out)
            ln = int.from_bytes(data[k + 2:k + 4], "big")
            if ln < 2 or k + 2 + ln > n:
                w.append("结构异常：标记段长度越界")
                out += data[i:]
                return bytes(out)
            if m not in _JPG_DROP:
                out += data[i:k + 2 + ln]
            i = k + 2 + ln
        if not ended:
            w.append("结构异常：未找到 EOI")
        if i < n:
            rest = data[i:]
            p = rest.find(b"\xff\xd8")
            if p < 0:
                out += rest                      # 无后继图：尾部字节原样保留
            else:
                out += rest[:p]                  # 图间填充
                out += strip_jpg_meta(rest[p:], w)
        return bytes(out)
    except Exception:
        w.append("剥离过程异常")
        return data


def strip_webp_meta(data: bytes, warn=None) -> bytes:
    """剔除 WebP 的 EXIF/XMP 块并清掉 VP8X 对应标志位（RIFF 字节级手术，像素原样保留）。

    ⚠️ B5（2026-10-17）：块长度越界时原先整段 `return data`（一个块都不剥，且
    静默）。现在改为「保留已处理部分 + 尾部字节原样追加」并写 warn。
    """
    w = warn if warn is not None else []
    try:
        if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
            w.append("不是 WebP（缺 RIFF/WEBP 头）")
            return data
        body = bytearray()
        i = 12
        n = len(data)
        while i + 8 <= n:
            ctype = data[i:i + 4]
            ln = int.from_bytes(data[i + 4:i + 8], "little")
            end = i + 8 + ln + (ln & 1)           # RIFF 块按 2 字节对齐
            if end > n:
                w.append("结构异常：块长度越界，尾部字节原样保留")
                body += data[i:]
                i = n
                break
            if ctype in (b"EXIF", b"XMP "):
                i = end                            # 整块丢弃
                continue
            if ctype == b"VP8X" and ln >= 1:
                flags = data[i + 8] & ~(0x08 | 0x04)   # 清 EXIF/XMP 标志位
                body += data[i:i + 8] + bytes([flags]) + data[i + 9:end]
            else:
                body += data[i:end]
            i = end
        if i < n:
            body += data[i:]                       # 尾部不足一个块头的零头
        out = bytearray(b"RIFF\x00\x00\x00\x00WEBP") + body
        out[4:8] = (len(out) - 8).to_bytes(4, "little")
        return bytes(out)
    except Exception:
        w.append("剥离过程异常")
        return data


def _mp4_boxes(data: bytes, start: int, end: int, warn=None):
    """遍历 [start,end) 范围里的 box，产出 (box起, 头长, 类型, 载荷起, 载荷止)。

    warn 非 None 时把结构异常写进去（B5-P1-3，2026-10-17）：原先异常一律静默
    `return`（生成器悄悄终止、调用方毫无察觉），`strip_mp4_meta` 照常
    `return bytes(out)` ⇒ 接口照样报「已抹除」。顶层一个 `size=0` 的 skip/free
    就能把它后面的 moov 整个吞掉，元数据一条不落全留着。

    默认 warn=None ⇒ 对既有 8 个只读调用点（视频首帧 / 元数据提取）零行为变化。
    """
    i = start
    while i + 8 <= end:
        size = int.from_bytes(data[i:i + 4], "big")
        btype = data[i + 4:i + 8]
        head = 8
        if size == 1:                    # 64 位 largesize
            if i + 16 > end:
                if warn is not None:
                    warn.append("mp4 结构异常：largesize 字段不完整")
                return
            size = int.from_bytes(data[i + 8:i + 16], "big")
            head = 16
        elif size == 0:                  # 0 = 一直延伸到文件尾（规范语义）
            size = end - i
        if size < head or i + size > end:
            if warn is not None:
                warn.append("mp4 结构异常：box 长度越界，扫描中止")
            return
        yield i, head, btype, i + head, i + size
        i += size


def _mp4_meta_boxes(data: bytes, start: int, end: int, warn=None):
    """产出 moov 范围内所有的元数据容器 (box起, 载荷起, 载荷止)。

    覆盖 `moov/udta`、`moov/meta`，以及 **`moov/trak/udta`**（有些相机 / 剪辑软件
    把 udta 挂在 trak 下）—— 只清 moov 的第一层会漏掉后者。
    """
    for o, _h, t, ps, pe in _mp4_boxes(data, start, end, warn):
        if t in (b"udta", b"meta"):
            yield o, ps, pe
        elif t == b"trak":
            for o2, _h2, t2, s2, e2 in _mp4_boxes(data, ps, pe, warn):
                if t2 in (b"udta", b"meta"):
                    yield o2, s2, e2


def strip_mp4_meta(data: bytes, warn=None) -> bytes:
    """清空 mp4 的元数据容器：moov 下的 udta / meta（含 trak 下一层），以及顶层 uuid（XMP）。

    为什么是**等长清零**而不是删除：mp4 的 stco/co64 记录的是**绝对文件偏移**，
    删字节会让后面的 mdat 整体前移、偏移表全部失效（视频花屏/不播）。做法是把
    udta/meta 的 box 类型改写成 free 并清零载荷 —— 长度不变、偏移不变（播放零
    风险），元数据字节真实消失（不是隐藏：strings/binwalk 也捞不出来）。
    ComfyUI 视频工作流（VideoHelperSuite 等）经 ffmpeg 把提示词写进
    moov/udta/meta/ilst，这一刀正好覆盖。结构异常原样返回（fail-safe）。

    B5-P1-3（2026-10-17）：原先「结构异常原样返回」是全静默的 —— 顶层出现
    `size=0` 或长度越界的 box 时 `_mp4_boxes` 直接终止，moov 一条都扫不到，
    元数据全留着而接口报成功。现在 ① 异常写进 warn；② 扫完没见到 moov
    容器也算一条 warn（合法 mp4 必有 moov），由调用方如实回报。
    """
    w = warn if warn is not None else []
    try:
        if len(data) < 12 or data[4:8] != b"ftyp":
            w.append("不是 MP4（缺 ftyp 头）")
            return data
        out = bytearray(data)
        saw_moov = False
        for _off, _head, btype, ps, pe in _mp4_boxes(data, 0, len(data), w):
            if btype == b"moov":
                saw_moov = True
                for o2, s2, e2 in _mp4_meta_boxes(data, ps, pe, w):
                    out[o2 + 4:o2 + 8] = b"free"      # 解析器见 free 直接跳过
                    for k in range(s2, e2):
                        out[k] = 0                    # 载荷清零
            elif btype == b"uuid":                    # 顶层 XMP 常见载体
                for k in range(ps, pe):
                    out[k] = 0
        if not saw_moov:
            w.append("mp4 里没找到 moov 容器，元数据是否清除无法确认")
        return bytes(out)
    except Exception:
        w.append("剥离过程异常")
        return data


# ---- B7-P1-2（2026-10-17）：大 MP4 的「结构计划 + 就地清零」----------------
# 症状：batch_export / save_download / 浏览器下载三条路都是 `read_bytes()` 整份进内存，
# 再 `bytearray(data)` 复制一份、`bytes(out)` 又一份 ⇒ 峰值约 3× 文件大小。几分钟的
# 1080p mp4 直接 MemoryError —— 而 MemoryError **不是 OSError**，原先的 `except OSError`
# 抓不到，异常一路穿过 handler ⇒ HTTP 500 / 连接中断，已经导出成功的那些文件连回执
# 都跟着一起丢（failed[] 随异常消失），目标目录留下不知齐不齐的一批文件。
# mp4 的剥离本来就是**等长清零**（不能删字节：stco/co64 记的是绝对偏移），所以完全
# 可以「只读 box 头、按范围就地写零」：内存占用与文件大小无关，长度与偏移一个都不动。

_MP4_ZCHUNK = 1 << 20            # 就地清零 / 流式复制的块大小


def _mp4_boxes_f(fh, start: int, end: int, warn=None):
    """`_mp4_boxes` 的文件版：只 seek+read(8/16) 读盒头，不整份载入。

    语义（含 largesize / size=0 / 越界终止与 warn 通道）与内存版逐条对齐 ——
    两版口径必须一致，否则「导出说干净了、下载却没干净」这种分叉无从解释。
    """
    i = start
    while i + 8 <= end:
        fh.seek(i)
        h8 = fh.read(8)
        if len(h8) < 8:
            if warn is not None:
                warn.append("mp4 结构异常：box 头不完整")
            return
        size = int.from_bytes(h8[:4], "big")
        btype = h8[4:8]
        head = 8
        if size == 1:                    # 64 位 largesize
            if i + 16 > end:
                if warn is not None:
                    warn.append("mp4 结构异常：largesize 字段不完整")
                return
            fh.seek(i + 8)
            size = int.from_bytes(fh.read(8), "big")
            head = 16
        elif size == 0:                  # 0 = 一直延伸到文件尾（规范语义）
            size = end - i
        if size < head or i + size > end:
            if warn is not None:
                warn.append("mp4 结构异常：box 长度越界，扫描中止")
            return
        yield i, head, btype, i + head, i + size
        i += size


def _mp4_meta_boxes_f(fh, start: int, end: int, warn=None):
    """`_mp4_meta_boxes` 的文件版：同样覆盖 moov/udta、moov/meta、moov/trak/udta。"""
    for o, _h, t, ps, pe in _mp4_boxes_f(fh, start, end, warn):
        if t in (b"udta", b"meta"):
            yield o, ps, pe
        elif t == b"trak":
            for o2, _h2, t2, s2, e2 in _mp4_boxes_f(fh, ps, pe, warn):
                if t2 in (b"udta", b"meta"):
                    yield o2, s2, e2


def _mp4_strip_plan(path: Path) -> tuple:
    """只读结构，返回 (操作表, 是否确认, 原因)。操作项 = (偏移, 长度, 内容或 None)。

    None 表示写零。结构异常（越界 / 缺 ftyp / 找不到 moov）⇒ ok=False + 原因 ——
    与内存版 `strip_mp4_meta` 的 fail-safe 口径一致：**原样保留，但必须说出来**。
    """
    ops, w = [], []
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(12)
            if size < 12 or head[4:8] != b"ftyp":
                return [], False, "不是 MP4（缺 ftyp 头）"
            saw_moov = False
            for _off, _h, btype, ps, pe in _mp4_boxes_f(fh, 0, size, w):
                if btype == b"moov":
                    saw_moov = True
                    for o2, s2, e2 in _mp4_meta_boxes_f(fh, ps, pe, w):
                        ops.append((o2 + 4, 4, b"free"))      # 解析器见 free 直接跳过
                        if e2 > s2:
                            ops.append((s2, e2 - s2, None))   # 载荷清零
                elif btype == b"uuid":                        # 顶层 XMP 常见载体
                    if pe > ps:
                        ops.append((ps, pe - ps, None))
            if not saw_moov:
                w.append("mp4 里没找到 moov 容器，元数据是否清除无法确认")
        return ops, (not w), (w[0] if w else "")
    except Exception as ex:
        return [], False, "读取失败：%s: %s" % (type(ex).__name__, ex)


def _mp4_apply_plan(path: Path, ops) -> None:
    """把操作表就地写进文件：等长改写，不动长度、不动偏移（视频播放零风险）。"""
    with open(path, "r+b") as fh:
        for off, ln, payload in ops:
            fh.seek(off)
            if payload is not None:
                fh.write(payload)
                continue
            left = ln
            while left > 0:
                n = min(left, _MP4_ZCHUNK)
                fh.write(b"\0" * n)
                left -= n
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except OSError:
            pass


def _apply_ops_to_buf(buf: bytearray, base: int, ops) -> None:
    """把「清零计划」套到一块**顺序读取**的缓冲区上（流式下载用）。

    base = 这块缓冲在文件里的起始偏移。只处理与它相交的区间，块外的一律不碰。
    """
    end = base + len(buf)
    for off, ln, payload in ops:
        s, e = off, off + ln
        if e <= base or s >= end:
            continue
        lo, hi = max(s, base), min(e, end)
        rel, n = lo - base, hi - lo
        if payload is None:
            buf[rel:rel + n] = b"\0" * n
        else:
            buf[rel:rel + n] = payload[lo - s:lo - s + n]


def _mp4_residue_file(path: Path) -> str:
    """文件版残留扫描：与 `_meta_residue` 的 mp4 分支同口径（走结构，不做子串匹配）。

    刻意**不信**「我刚写过了」：写完重新扫一遍结构，只看还剩下什么。
    """
    ops, ok, why = _mp4_strip_plan(path)
    if not ok:
        return why or "未能确认"
    for off, ln, payload in ops:
        if payload is not None:           # 重新扫到「仍是 udta/meta」⇒ 改型没生效
            return "MP4 的 moov 里仍有 udta/meta"
        with open(path, "rb") as fh:      # 顶层 uuid 只清零载荷，型还在 ⇒ 查载荷
            fh.seek(off)
            left = ln
            while left > 0:
                n = min(left, _MP4_ZCHUNK)
                if any(fh.read(n)):
                    return "MP4 顶层仍有非零 uuid（XMP）"
                left -= n
    return ""


def _copystat(src: Path, dst: Path) -> None:
    """保留时间戳（B7-P2-6：导出件的 mtime 不该变成「导出那一刻」）。失败不影响内容。"""
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass


def _copy_then_strip_mp4(src: Path, dest_dir: Path, name: str) -> tuple:
    """流式复制到独占目标，再**在目标文件上**原地抹除元数据。返回 (路径, ok, 原因)。

    峰值内存 = 一块 1MB 缓冲区，与文件大小无关。
    """
    target, fh = _open_exclusive(dest_dir, name)
    try:
        with fh, open(src, "rb") as sf:
            shutil.copyfileobj(sf, fh, _MP4_ZCHUNK)
        ops, ok, why = _mp4_strip_plan(target)
        if not ok:
            why = why or "未能确认"
        elif ops:
            _mp4_apply_plan(target, ops)
            why = _mp4_residue_file(target)         # 独立一遍复扫
            ok = not why
        _copystat(src, target)
        return target, ok, why
    except BaseException:
        try:
            target.unlink()                          # 别把半截文件留在用户目录里
        except OSError:
            pass
        raise


def _gif_scan(data: bytes, warn=None):
    """GIF 块级扫描：依次产出 (块起, 块止, 块类型, 扩展标签)。

    块类型：0x21=扩展（第 4 个值是扩展标签）、0x2C=图像描述符、0x3B=结束符。
    头部与全局色表不在产出范围内（调用方自己拷）。

    结构不可信时把原因写进 warn 并停止产出 —— 调用方必须把**剩余字节原样保留**，
    绝不能像老代码那样「保守截断」（那等于替用户丢字节）。
    """
    n = len(data)
    if n < 13 or data[:6] not in (b"GIF87a", b"GIF89a"):
        if warn is not None:
            warn.append("不是 GIF（缺 GIF87a/GIF89a 头）")
        return
    i = 13
    if data[10] & 0x80:                               # 全局色表
        i += 3 * (2 ** ((data[10] & 0x07) + 1))
    if i > n:
        if warn is not None:
            warn.append("结构异常：全局色表越界")
        return

    def _sub_end(pos):
        """子块链末尾（含终止 0x00）的下一个位置。"""
        while pos < n and data[pos]:
            pos += 1 + data[pos]
        return min(pos + 1, n)

    while i < n:
        b = data[i]
        if b == 0x3B:                                 # 文件结束
            yield i, i + 1, 0x3B, None
            return
        if b == 0x21:                                 # 扩展块
            if i + 3 > n:
                if warn is not None:
                    warn.append("结构异常：扩展块头不完整")
                return
            label = data[i + 1]
            if label == 0xFF:                         # 应用扩展：标识长度**可变**
                blen = data[i + 2]
                if i + 3 + blen > n:
                    if warn is not None:
                        warn.append("结构异常：应用扩展标识越界")
                    return
                j = _sub_end(i + 3 + blen)
            else:
                j = _sub_end(i + 2)
            yield i, j, 0x21, label
            i = j
            continue
        if b == 0x2C:                                 # 图像块
            if i + 10 > n:
                if warn is not None:
                    warn.append("结构异常：图像描述符不完整")
                return
            j = i + 10                                # 0x2C + 9 字节描述符
            if data[i + 9] & 0x80:                    # 局部色表
                j += 3 * (2 ** ((data[i + 9] & 0x07) + 1))
            if j > n:
                if warn is not None:
                    warn.append("结构异常：局部色表越界")
                return
            j = _sub_end(j + 1)                       # LZW 最小码长 + 数据子块
            yield i, j, 0x2C, None
            i = j
            continue
        if warn is not None:
            warn.append("结构异常：出现无法识别的块")
        return
    if warn is not None:
        warn.append("结构异常：未找到结束符")


def strip_gif_meta(data: bytes, warn=None) -> bytes:
    """删掉 GIF 的注释扩展与 XMP 应用扩展。

    GIF 是流式块结构（没有绝对偏移表），删除扩展块不破坏解码 —— 比等长清零更
    干净（字节真的没了）。解码序列其余部分原样复制。

    B5-P1-4（2026-10-17）：XMP 的判据原先写死「应用标识 + 认证码 = 11 字节」，
    按 `data[i+3:i+14]` 这种**硬编码偏移**取标识 —— 规范里这个长度是可变的，
    块长不是 11 时取到错位的字节，永远判不中，XMP 随文件一起出去而界面报「已抹除」。
    现在按**真实块长**取标识（`data[i+2]` 是块长），只认前缀 `XMP`。
    ⚠️ 另外：结构异常时不再「保守截断」（那是丢用户数据），改为把剩余字节原样
    追加并写 warn，由调用方如实回报「没抹干净」。
    """
    w = warn if warn is not None else []
    try:
        n = len(data)
        if n < 13 or data[:6] not in (b"GIF87a", b"GIF89a"):
            w.append("不是 GIF（缺 GIF87a/GIF89a 头）")
            return data
        head_end = 13
        if data[10] & 0x80:
            g = 3 * (2 ** ((data[10] & 0x07) + 1))
            if 13 + g > n:
                w.append("结构异常：全局色表越界")
                return data
            head_end = 13 + g
        skip, stop = [], n
        for i, j, kind, label in _gif_scan(data, w):
            if kind == 0x3B:
                stop = j
                break
            if kind != 0x21:
                continue
            blen = data[i + 2]
            if label == 0xFE:                          # 注释扩展
                skip.append((i, j))
            elif label == 0xFF and blen >= 3 and \
                    data[i + 3:i + 3 + blen].startswith(b"XMP"):
                skip.append((i, j))                    # XMP 应用扩展
        out = bytearray(data[:head_end])
        cur = head_end
        for a, b2 in skip:
            if a >= stop:
                break
            out += data[cur:a]
            cur = b2
        out += data[cur:stop]                          # 扫描提前结束时剩余字节原样保留
        return bytes(out)
    except Exception:
        w.append("剥离过程异常")
        return data


def _meta_residue(ext: str, data: bytes) -> str:
    """独立的一遍残留结构扫描：返回**还残留着什么**（空串 = 没查到）。

    为什么不信剥离函数自己的结论：B5 那批 P1 的共同点就是「函数正常返回、
    元数据其实还在」。这里是第二道防线 —— 按格式**真走一遍结构**，只认结构里
    确实存在的元数据容器。刻意**不做** `in` 子串硬匹配（IDAT / LZW / mdat 压缩
    流里凑出 4 字节巧合的概率不为零，那种判据会假红）。
    """
    try:
        if ext in (".jpg", ".jpeg"):
            for sig, what in ((b"Exif\x00\x00", "EXIF"),
                              (b"http://ns.adobe.com/xap/1.0/", "XMP"),
                              (b"Photoshop 3.0\x00", "IPTC")):
                if sig in data:
                    return "仍能搜到 %s 特征" % what
        elif ext == ".png":
            i = 8
            while i + 12 <= len(data):
                ln = int.from_bytes(data[i:i + 4], "big")
                ctype = data[i + 4:i + 8]
                if ctype in (b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"):
                    return "PNG 里仍有 %s 块" % ctype.decode("ascii", "replace")
                if ctype == b"IEND" or ln > len(data) - i - 12:
                    break
                i += 12 + ln
        elif ext == ".webp":
            i = 12
            while i + 8 <= len(data):
                ctype = data[i:i + 4]
                ln = int.from_bytes(data[i + 4:i + 8], "little")
                end = i + 8 + ln + (ln & 1)
                if ctype in (b"EXIF", b"XMP "):
                    return "WebP 里仍有 %s 块" % ctype.decode("ascii", "replace").strip()
                if ctype == b"VP8X" and ln >= 1 and (data[i + 8] & 0x0C):
                    return "WebP 的 VP8X 仍标着 EXIF/XMP"
                if end > len(data):
                    break
                i = end
        elif ext == ".gif":
            for i, _j, kind, label in _gif_scan(data):
                if kind != 0x21:
                    continue
                blen = data[i + 2]
                if label == 0xFE:
                    return "GIF 里仍有注释扩展"
                if label == 0xFF and blen >= 3 and \
                        data[i + 3:i + 3 + blen].startswith(b"XMP"):
                    return "GIF 里仍有 XMP 应用扩展"
        elif ext == ".mp4":
            for _off, _head, btype, ps, pe in _mp4_boxes(data, 0, len(data)):
                if btype == b"moov":
                    for _o, _s, _e in _mp4_meta_boxes(data, ps, pe):
                        return "MP4 的 moov 里仍有 udta/meta"
                elif btype == b"uuid" and any(data[ps:pe]):
                    return "MP4 顶层仍有非零 uuid（XMP）"
    except Exception:
        return "残留扫描异常"
    return ""


def strip_meta_checked(ext: str, data: bytes) -> tuple:
    """跑一次剥离并**如实回报**：(输出字节, 是否确认已抹干净, 没抹干净的原因)。

    B5 那 4 条 P1 的共同点是「函数正常返回 ⇒ 调用方以为抹干净了」：五个 strip_*
    都是 fail-safe 的，结构异常时把原字节原样吐回来，可调用方只会看到「拿到了一个
    bytes」，于是接口报 `cleaned:true`、前端打「元数据已抹除」—— 用户据此把带
    EXIF / XMP 的成品发出去。这里把「确认」变成可判定的两件事：
      ① 剥离函数自己报的结构异常（warn 通道）；
      ② 独立的一遍残留结构扫描（不信剥离函数的一面之词）。
    两道都过才 ok；任一不过 ⇒ ok=False + 原因，调用方据此改口。
    """
    fn = _STRIP_BY_EXT.get(ext)
    if fn is None:
        return data, False, "该格式不支持抹除元数据"
    warn = []
    try:
        out = fn(data, warn)
    except Exception:
        return data, False, "剥离过程异常"
    why = _meta_residue(ext, out)
    if why:
        return out, False, why
    if warn:
        return out, False, warn[0]
    return out, True, ""


def _unique_path(p: Path) -> Path:
    """目标已存在时依次加 _1/_2/… 后缀，保持原名优先。"""
    if not p.exists():
        return p
    stem, suf = p.stem, p.suffix
    for i in range(1, 1000):
        q = p.with_name(f"{stem}_{i}{suf}")
        if not q.exists():
            return q
    return p.with_name(f"{stem}_{int(time.time())}{suf}")


def _open_exclusive(dest_dir: Path, name: str):
    """在 dest_dir 下**独占创建** name（重名顺延 _1/_2/…，规则同 `_unique_path`）。
    返回 (实际路径, 已打开的文件对象)，写什么由调用方决定。

    B5-P2-5（2026-10-17）：`_unique_path` 是「先 exists() 判断、再 write_bytes」
    两趟，中间没有原子性 —— 两次并发导出（连点两下、两个窗口、桥接脚本与界面
    同时跑）算出同一个目标路径，后到的 write_bytes 先截断再写，先完成的那份被
    覆盖，两边都报成功。改成 O_EXCL 一次完成「判重 + 占位」：抢输的一方拿到
    FileExistsError，顺延下一个后缀重试，谁也不会踩到别人。

    **有意不用 `.part` + `os.replace`**：rename 会覆盖已存在的目标，等于把
    「并发覆盖」原样挪到换名那一步；而 O_EXCL 占位后直接写，同样的文件句柄，
    语义更简单。进程被强杀的极端情况下可能留半截文件 —— 那由调用方的 except
    清理兜住（正常异常路径都能删掉，硬杀属于既有风险，本批不新增）。
    """
    stem, suf = os.path.splitext(name)
    for i in range(0, 1000):
        cand = dest_dir / (name if i == 0 else f"{stem}_{i}{suf}")
        try:
            return cand, open(cand, "xb")
        except FileExistsError:
            continue
    cand = dest_dir / f"{stem}_{int(time.time())}{suf}"
    return cand, open(cand, "xb")


def _export_dest_allowed(dest: str):
    """`/api/export_save` 的 `dest` 直写通道：判断这个目标路径能不能写。

    A02（2026-11-05 审计复核）：`dest` 原先是**任意路径直写** —— 一个 POST 就能
    把内容原子替换到任何位置（实测可覆盖 hosts、启动项、程序目录里的文件），
    等于给本机 HTTP 端口开了一个「覆盖任意文件」的写原语；LAN 模式下同 token
    的设备也能打到这个端口，影响不止本机。

    这里收成三条，够用且不误伤：
      ① 必须是**绝对路径**（相对路径全靠进程 CWD，行为不可预期）；
      ② 后缀必须是 `.json` —— 本端点产出物就是工作流 JSON，放开后缀等于允许
         把任意内容写成 .exe/.dll/.bat/hosts；
      ③ 只能落在 **图库根目录 / 数据目录 / 系统临时目录** 之内。
         自动化测试的 `dest` 全在 `%TEMP%\\cmv_iso_*` 下，前两条也不受影响；
         用户手动导出走的是系统「另存为」框（`dest` 不传），完全不经这里。

    ⚠️ 这里用 `abspath` 而非 `resolve`：`dest` 的父目录允许不存在（E7 要的是
    「写失败如实报错」而不是 400），`resolve` 在 Windows 上对不存在的深层路径
    行为不稳。代价是不穿透 junction / 符号链接 —— 但能在本机建 junction 的
    进程本来就能直接写目标文件，这一层不为它兜底。

    返回 `(ok, 拒绝原因)`；`dest` 为空（走另存为框）时恒 `(True, "")`。
    """
    if not dest:
        return True, ""
    try:
        dp = Path(dest)
        if not dp.is_absolute():
            return False, "dest 必须是绝对路径"
        if dp.suffix.lower() != ".json":
            return False, "dest 只接受 .json 文件"
        target = os.path.normcase(os.path.abspath(str(dp)))
    except (OSError, ValueError):
        return False, "dest 不是合法路径"
    for _b in (STATE.get("root"), DATA_DIR, tempfile.gettempdir()):
        if not _b:
            continue
        try:
            base = os.path.normcase(os.path.abspath(str(_b)))
        except (OSError, ValueError):
            continue
        if target == base or target.startswith(base + os.sep):
            return True, ""
    return False, "dest 只能落在图库根目录、数据目录或临时目录内"


def _write_exclusive(dest_dir: Path, name: str, data: bytes) -> Path:
    """`_open_exclusive` + 全量写入 + 失败清残。返回实际路径。"""
    target, fh = _open_exclusive(dest_dir, name)
    try:
        with fh:
            fh.write(data)
    except OSError:
        try:
            target.unlink()
        except OSError:
            pass
        raise
    return target


def _move_into(dest_dir: Path, name: str, src: Path) -> Path:
    """把已经写好的临时文件搬成 dest_dir 下的**独占**最终名。

    B7-P1-2（2026-10-17）：mp4 必须先复制、抹完，才知道该不该加 `_clean` 后缀
    （后缀与「**确认**抹过」绑定，见 B5 的说明），所以没法一开始就用最终名写。
    最终名仍要独占：先 O_EXCL 占位拿到一个属于自己的名字，再把临时文件换上去 ——
    抢输的一方在占位那一步就顺延了后缀，谁也不会踩到别人。
    """
    target, fh = _open_exclusive(dest_dir, name)
    fh.close()
    try:
        os.replace(src, target)
    except OSError:
        for q in (target, src):
            try:
                q.unlink()
            except OSError:
                pass
        raise
    return target


# ---------------------------------------------------------------- trash（应用内回收站）

def load_trash() -> dict:
    """回收站索引 {key: {name, orig, t, size}}。"""
    try:
        d = json.loads((TRASH_DIR / "index.json").read_text("utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_trash(idx: dict):
    try:
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        tmp = TRASH_DIR / f"index.{_tmp_tag()}.tmp"
        tmp.write_text(json.dumps(idx, ensure_ascii=False), "utf-8")
        tmp.replace(TRASH_DIR / "index.json")
        return True
    except OSError as ex:
        log(f"[warn] 回收站索引写入失败: {ex}")
        return False


def trash_move(p: Path):
    """移入应用内回收站。成功返回 (None, key)，失败返回 (错误信息, None)。"""
    if not p.is_file():
        return "文件不存在或已被删除", None
    if not inside_root(p):
        return "文件在根目录之外，拒绝删除", None
    moved = None                     # 提前占位：失败分支要清残留，不能引 NameError
    try:
        st = p.stat()
        key = f"{int(time.time() * 1000)}_{os.urandom(3).hex()}"
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        moved = TRASH_DIR / (key + p.suffix)
        shutil.move(str(p), str(moved))
    except OSError as ex:
        # B6-1（2026-10-09 全代码审计）：图库在外接盘、DATA_DIR 在系统盘（跨卷）时
        # `shutil.move` 走「复制 + 删源」两段，**每一段都可能失败**：
        #   ① 复制写一半抛错（目标盘空间不足 / 中断）⇒ 回收站里留下半截文件；
        #   ② 复制成功但删源失败（源被占用 / 只读）⇒ 留下完整副本。
        # 实测两种情形**原文件都完好留在原地**（不会两处丢失），但回收站里会多出
        # 一个不在 index.json 里的文件：UI 看不见、`trash_gc` 管不着（只遍历索引）、
        # `trash_purge` 也清不掉（只 glob 索引里的 key）⇒ 数据盘被静默持续占用；
        # 用户看到「删除失败」会重试，每次重试再泄漏一份，直到系统盘被吃满，
        # 然后 save_trash / save_favs 也开始失败，故障向外扩散。
        # ⇒ 失败分支必须把残留清掉（清不掉也要如实说明）。
        left = ""
        try:
            if moved is not None and moved.exists():
                moved.unlink()
        except OSError as ex2:
            left = f"（回收站里残留 {moved.name} 未能清除：{ex2}）"
            log(f"[error] 移入回收站失败且残留未清：{moved}：{ex2}")
        return f"移入回收站失败: {ex}{left}", None
    with _store_lock(TRASH_LOCK, "回收站"):
        idx = load_trash()
        idx[key] = {"name": p.name, "orig": str(p), "t": int(time.time()),
                    "size": st.st_size,
                    # B6-3：把真实落盘名记进索引 —— 无扩展名的文件（README / a.）
                    # 落盘名里**不含点**，靠 glob(key+".*") 反查永远匹配不到它。
                    "stored": moved.name}
        if not save_trash(idx):
            # §6.1：索引没落盘 ⇒ 这个 key 不存在于下次 load_trash() 的结果里，
            # 文件就成了「在回收站目录里、但谁也看不见」的孤儿（也不受自动清理管辖）。
            # 光「说一句失败了」不够 —— 必须**把文件退回去**（回滚），别留孤儿。
            try:
                _dest = _rollback_move(moved, p)
            except OSError as ex2:
                log(f"[error] 回收站索引写入失败且回滚失败：文件仍在 {moved}，原路径 {p}：{ex2}")
                return (f"移入回收站失败：索引与回滚均失败，文件暂存在 {moved}（详见日志）", None)
            _where = "原位置" if _dest == p else f"旁边的「{_dest.name}」（原文件名已被占用）"
            log(f"[warn] 回收站索引写入失败，已把 {p.name} 退回到{_where}")
            return f"移入回收站失败：索引写入失败（磁盘可能已满），文件已退回到{_where}", None
    return None, key


def _rollback_move(moved: Path, p: Path) -> Path:
    """把回收站里的文件退回原位置，返回**实际落点**。

    4.1-4（2026-11-05 审计）：`shutil.move` 在目标已存在时（Windows 上 os.rename
    抛 FileExistsError）会退化成 copy2 —— 以 "wb" 打开目标 ⇒ 把用户在「索引写盘
    失败 → 回滚」这个毫秒窗口里刚新建的同名文件覆盖掉。回收站里的那份必须回来，
    但不能拿另一份文件当代价：重名时落到「name (恢复).ext」。
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    dest = p
    if os.path.lexists(str(p)):
        stem, suf = os.path.splitext(p.name)
        for i in range(1, 1000):
            tail = " (恢复)" if i == 1 else f" (恢复{i})"
            cand = p.with_name(stem + tail + suf)
            if not os.path.lexists(str(cand)):
                dest = cand
                break
        else:
            raise OSError("回滚目标重名，且备用文件名已用尽")
    shutil.move(str(moved), str(dest))
    return dest


def _trash_rollback(p: Path, moved: Path) -> str:
    """索引没落盘 ⇒ 把文件退回原位置，返回给用户看的那句话（与单条路径同款文案）。"""
    try:
        _dest = _rollback_move(moved, p)
    except OSError as ex:
        log(f"[error] 回收站索引写入失败且回滚失败：文件仍在 {moved}，原路径 {p}：{ex}")
        return f"移入回收站失败：索引与回滚均失败，文件暂存在 {moved}（详见日志）"
    where = "原位置" if _dest == p else f"旁边的「{_dest.name}」（原文件名已被占用）"
    log(f"[warn] 回收站索引写入失败，已把 {p.name} 退回到{where}")
    return f"移入回收站失败：索引写入失败（磁盘可能已满），文件已退回到{where}"


def trash_move_many(paths) -> dict:
    """批量移入应用内回收站：**一把锁、按块落盘**，语义与逐条 `trash_move` 完全一致。

    返回 `{原路径字符串: (错误信息|None, key|None)}`（与原路径同序无关，调用方按路径取）。

    为什么需要它（O3）：`/api/delete` 原来逐条调 `trash_move`，而每条都要
    `load_trash()`（整份 index.json 解析）+ `save_trash()`（整份序列化 + tmp.replace），
    全程反复争同一把 `TRASH_LOCK`。实测单次 load+save：500 条 1.75ms /
    2000 条 3.87ms / 10000 条 15.36ms ⇒ 回收站已有 1 万条时删 500 个约 **7.5s**，
    界面一直转圈、请求还可能被前端超时掐断。

    落盘为什么按块：块内条目一起写；某块 `save_trash` 失败就只回滚**这一块**的文件，
    前面的块照常算已删。整批一起落盘会让「磁盘满」把几百个已经搬走的文件全退回去
    （用户白删一次）；而完全不回滚又会留下「文件在回收站、索引里没有」的孤儿
    （`trash_gc` 只遍历索引 ⇒ 永远清不掉）。
    """
    out = {}
    staged = []          # [(Path, key, moved, entry)]
    used = set()         # 同批内 key 去重（同一毫秒搬 200 个，3 字节随机虽够用但没必要赌）
    for raw in paths:
        p = Path(raw)
        # 与单条路径同一套前置校验（顺序也一致：先「不存在」再「根外」）
        if not p.is_file():
            out[str(p)] = ("文件不存在或已被删除", None)
            continue
        if not inside_root(p):
            out[str(p)] = ("文件在根目录之外，拒绝删除", None)
            continue
        moved = None
        try:
            st = p.stat()
            key = f"{int(time.time() * 1000)}_{os.urandom(3).hex()}"
            while key in used:
                key = f"{int(time.time() * 1000)}_{os.urandom(3).hex()}"
            used.add(key)
            TRASH_DIR.mkdir(parents=True, exist_ok=True)
            moved = TRASH_DIR / (key + p.suffix)
            shutil.move(str(p), str(moved))
        except OSError as ex:
            # 与 B6-1 同款：跨卷 move 的「复制成功、删源失败」会在回收站留下残片
            left = ""
            try:
                if moved is not None and moved.exists():
                    moved.unlink()
            except OSError as ex2:
                left = f"（回收站里残留 {moved.name} 未能清除：{ex2}）"
                log(f"[error] 移入回收站失败且残留未清：{moved}：{ex2}")
            out[str(p)] = (f"移入回收站失败: {ex}{left}", None)
            continue
        staged.append((p, key, moved, {"name": p.name, "orig": str(p),
                                       "t": int(time.time()), "size": st.st_size,
                                       "stored": moved.name}))

    if not staged:
        return out
    chunk = max(1, int(TRASH_BATCH_CHUNK))
    with _store_lock(TRASH_LOCK, "回收站"):
        idx = load_trash()                       # 整批只读一次（后续块在内存里累加）
        for i in range(0, len(staged), chunk):
            batch = staged[i:i + chunk]
            for _p, key, _m, entry in batch:
                idx[key] = entry
            if save_trash(idx):
                for _p, key, _m, _e in batch:
                    out[str(_p)] = (None, key)
                continue
            # 这一块没落盘 ⇒ 索引里查不到它们 ⇒ 文件必须退回原处，否则就是孤儿
            for _p, key, moved, _e in batch:
                idx.pop(key, None)
                out[str(_p)] = (_trash_rollback(_p, moved), None)
    return out


def _trash_stored_path(key: str, entry: dict = None):
    """按 key 找到回收站里真正落盘的那个文件；找不到返回 None（B6-3）。

    为什么不能再用 `TRASH_DIR.glob(key + ".*")`：落盘名是 `key + Path.suffix`，
    **无扩展名的文件**（`README` / `a.` / Windows 会去掉尾部点）后缀为空 ⇒
    落盘名就是 `key` 本身（恒不含点），而 `glob("key.*")` 要求「key 后面必须有点」
    ⇒ 永远匹配不到。实测后果：点「还原」返回「回收站文件已丢失」（文件其实好端端
    躺在目录里），旧代码随即**删掉索引条目** ⇒ 该文件从回收站 UI 永久消失；
    此后 `trash_purge([key])` 也因为同一个 glob 匹配不到而 `purged: 0`
    ⇒ 索引没了、文件还在，对用户等于永久丢失，回收站目录还永久残留。

    新口径：① 优先用索引里记的落盘名（新条目都有 `stored`）；
    ② 老条目没有 `stored` ⇒ 用「名字等于 key 或以 `key.` 开头」前缀匹配兜底
    （点号保证了不会误命中别的 key）。
    """
    if entry:
        nm = str(entry.get("stored") or "")
        # 2026-11-05 P2：只认**纯文件名**。`stored` 由 `moved.name` 生成，本身安全；
        #   但索引被手改 / 历史污染时，`"..\\..\\x.png"` 能命中回收站外的文件，
        #   绝对路径更会让 `Path.__truediv__` 直接返回该绝对路径（等于任意文件读）。
        #   不合法就退回下面的前缀匹配。
        if nm and nm == Path(nm).name and not Path(nm).is_absolute():
            cand = TRASH_DIR / nm
            try:
                if cand.is_file():
                    return cand
            except OSError:
                pass
    try:
        hit = [f for f in TRASH_DIR.iterdir()
               if f.is_file() and (f.name == key or f.name.startswith(key + "."))]
    except OSError:
        return None
    if not hit:
        return None
    try:                                  # 万一有历史残留，取最大的那个（真正的数据）
        hit.sort(key=lambda f: f.stat().st_size, reverse=True)
    except OSError:
        pass
    return hit[0]


def trash_restore(key: str) -> dict:
    with _store_lock(TRASH_LOCK, "回收站"):
        idx = load_trash()
        e = idx.get(key)
        if not e:
            return {"ok": False, "error": "回收站条目不存在"}
        src = _trash_stored_path(key, e)
        if not src:
            # B6-3：**绝不**在这里删索引条目 —— 条目是用户唯一还能看见它的地方。
            # 旧代码 `del idx[key]` + 报「回收站文件已丢失」，可文件其实还在目录里
            # （无扩展名条目就是这条路径），于是它从回收站 UI 永久消失、purge 也
            # 够不到。改为保留条目、把「期望的文件名」如实说出来，可重试、可手工处理。
            return {"ok": False, "keep": True,
                    "error": "回收站里找不到这个文件（索引记录的文件名：%s）。"
                             "条目已保留 —— 可稍后重试，或到数据目录的 trash\\ 里手工处理"
                             % (e.get("stored") or (key + ".*"))}
        orig = Path(e["orig"])
        # B6-P2-7（2026-10-19）：还原目标必须仍在**当前**库根（或暂存区）内。
        # 删完之后换根 / 换盘符（外接盘重插拿到新盘符）时，`orig` 指向旧位置 ——
        # 不校验就把文件写到当前 root 之外，用户在新根目录里找不到，响应却仍报 ok；
        # 跨盘还会悄悄退化成一次复制。这里明确失败并保留条目（用户可换回旧根再还原）。
        try:
            _dest_ok = inside_root(orig)
        except Exception:                      # root 未设置 / 盘不可达：一律按不安全处理
            _dest_ok = False
        if not _dest_ok:
            return {"ok": False, "keep": True,
                    "error": "原始位置不在当前图库目录内（多半是换过库根或盘符变了）：%s。"
                             "条目已保留 —— 切回原来的库根再还原，或手工移动。"
                             % str(orig)}
        snap_fav = e.get("fav")
        snap_tags = e.get("tags")
        snap_mark = e.get("mark")
        try:
            orig.parent.mkdir(parents=True, exist_ok=True)
            dest = _unique_path(orig)
            shutil.move(str(src), str(dest))
        except OSError as ex:
            return {"ok": False, "error": f"还原失败: {ex}"}
        del idx[key]
        # §6.1：索引没落盘 ⇒ 条目残留，用户再点一次「还原」会看到「条目不存在」，
        # 而文件其实早就回去了（脏索引 + 误报）。带回 idx_stale 让前端能说清。
        idx_stale = not save_trash(idx)
        if idx_stale:
            log("[warn] 回收站索引落盘失败（还原之后），条目可能残留")
    # P1-2：还原「文件」与还原「它的收藏 / 标签 / 标记」是两件事 —— 后者失败也必须说出来，
    # 否则用户看到「已还原」却发现收藏没了，且没有任何线索（沿用 P0-2 数据目录迁移的口径）。
    failed = []
    if snap_fav or snap_tags or snap_mark:
        failed = _restore_meta(str(dest), fav=snap_fav, tags=snap_tags, mark=snap_mark)
    r = {"ok": True, "path": str(dest), "renamed": dest.name != e["name"]}
    if idx_stale:
        r["idx_stale"] = True
    if failed:
        r["partial"] = True
        r["failed"] = failed
        r["warning"] = ("文件已还原到原位置，但 " + "、".join(failed)
                        + "未能写回（磁盘不可写或被占用），这部分需要手工恢复")
        log(f"[warn] 还原后元数据回写失败：{failed}  path={dest}")
    return r


def _restore_meta(path: str, fav=None, tags=None, mark=None) -> list:
    """把移入回收站前快照的收藏/标签/标记写回还原后的文件路径。

    P1-2：返回**写失败的类别名列表**（空列表=全部成功）。此前三个 save 的返回值
    一个都没看，写盘失败（磁盘满 / 只读 / 文件被占用）被完全吞掉，调用方照报成功。
    """
    global _FAIL_SAVE_AT
    if os.environ.get("CMV_TEST_FAIL_SAVE") == "favs":
        _FAIL_SAVE_AT = "favs"          # 测试注入：只影响本次恢复回写，返回前复位
    bad = []
    if fav:
        with _store_lock(FAV_LOCK, "回收站还原"):
            fv = load_favs()
            if path not in fv:
                fv[path] = {"t": int(time.time()), "cat": fav if isinstance(fav, str) and fav else "默认"}
                if not save_favs(fv):
                    bad.append("收藏")
    if tags:
        clean, seen = [], set()
        for t in tags:
            t = _norm_tag(t)
            if t and t not in seen:
                clean.append(t)
                seen.add(t)
        with _store_lock(TAGS_LOCK, "回收站还原"):
            ts = load_tags()
            if clean:
                ts[path] = clean
            elif path in ts:
                ts.pop(path, None)
            if not save_tags(ts):
                bad.append("标签")
    if mark and mark in MARK_LABELS:
        with _store_lock(MARKS_LOCK, "回收站还原"):
            mk = load_marks()
            if path not in mk:
                mk[path] = {"state": mark, "updated": int(time.time())}
                if not _save_json(MARKS_FILE, mk):
                    bad.append("标记")
    _FAIL_SAVE_AT = None                 # 注入只在一次恢复内有效
    return bad


def trash_purge(keys: list, all_: bool = False) -> dict:
    """彻底删除指定条目，返回删除文件数。

    ⚠️ P0-2（2026-10-08 全代码审计）：「清空全部」曾**隐含**在 `if not keys` 里 ——
       keys 缺字段 / 为 null / 为 [] 三种形态全都落进 `keys = list(idx)`，
       于是任何一次「前端漏传字段」都变成**不可逆的整站清空**，且无二次确认。
       更糟的是它与「用户确实想清空」在服务端完全不可区分。
       现在拆开：**清空全部必须显式传 `all_=True`**（HTTP 侧 `{"all":true}`）；
       `keys` 为空 ⇒ 什么都不做（no-op），返回 purged=0，绝不代用户做决定。
       调用方：`trash_gc` 走显式 `keys=` 列表；前端「清空回收站」改传 `{"all":true}`。

    §6.1：此前 `except OSError: pass` —— 文件删不掉（被占用 / 只读 / 权限）时静默跳过，
    可索引项**已经先删了** ⇒ 文件变成「在回收站目录里但谁也看不见」的孤儿：
    白占空间、不受自动清理管辖、也无法重试。现在两处都改：
      ① 删不掉的条目**保留在索引里**（用户看得见、能重试）；
      ② 失败明细如实带回给前端（`failed` + `error`）。
    """
    with _store_lock(TRASH_LOCK, "回收站"):
        idx = load_trash()
        if all_:
            keys = list(idx)
        elif not keys:
            log("[trash] purge 收到空 keys 且未指定 all —— 按 no-op 处理（P0-2：不再默认整站清空）")
            return {"ok": True, "purged": 0, "empty": True}
        n = 0
        failed = []
        missing = []
        unknown = []
        for k in keys:
            if k not in idx:
                # B9-P2-8（2026-10-18）：原先**静默跳过** ⇒ 返回 ok:true / purged:0，
                # 前端照着提示「已彻底删除 0 个文件」，而真相是「这个 key 根本不在
                # 回收站里」。与 trash_restore 的「回收站条目不存在」对齐：如实带回。
                unknown.append(k)
                continue
            # B6-P2-3（2026-10-19）：条目值不是对象时下面每一处 `.get` 都会抛
            # AttributeError ⇒ HTTP 500（后台线程里则是整个清理静默死亡）。
            # 坏条目既不删也不猜，按「文件找不到」如实列出并保留。
            if not isinstance(idx[k], dict):
                missing.append({"key": k, "name": k, "stored": k + ".*"})
                log(f"[warn] 回收站条目损坏（值不是对象），已跳过并保留：{k}")
                continue
            # B6-3：不再用 `glob(k + ".*")` 找落盘文件 —— 无扩展名条目（README / a.）
            # 的落盘名恒不含点，那个 glob 永远匹配不到。旧行为是 purged:0 且**摘索引**
            # ⇒ 条目没了、文件还留在回收站目录里，谁都不管（用户等于永久丢失）。
            hit = _trash_stored_path(k, idx[k])
            if hit is None:
                missing.append({"key": k, "name": idx[k].get("name") or k,
                                "stored": idx[k].get("stored") or (k + ".*")})
                log("[warn] 回收站文件缺失，条目保留: %s（key=%s，期望文件名 %s）"
                    % (idx[k].get("name") or k, k,
                       idx[k].get("stored") or (k + ".*")))
                continue
            stuck = []
            try:
                hit.unlink()
                n += 1
            except OSError as ex:
                stuck.append("%s: %s" % (hit.name, ex))
            if stuck:
                failed.append({"key": k, "name": idx[k].get("name") or k,
                               "files": len(stuck), "reason": stuck[0]})
                log("[warn] 回收站文件删除失败，条目保留: %s（%d 个文件）: %s"
                    % (idx[k].get("name") or k, len(stuck), stuck[0]))
            else:
                del idx[k]              # 文件都删干净了才摘索引
        _saved = save_trash(idx)
        if not _saved:
            # A10（2026-11-05 审计复核）：落盘失败此前只写日志，返回值仍是
            # `ok:True / purged:n`，前端照着提示「已彻底删除 n 个文件」。可内存里的
            # 索引已经摘掉这些条目、磁盘上还是旧的 —— 重启后条目复活、文件却没了，
            # 下次 purge 只能归到 missing，从此**无法自愈**。
            # 文件确实删掉了，所以不把 ok 置 False（那是另一种谎报），但必须把这个
            # 事实带出去，让用户知道「再清一次就好」。
            log("[warn] 回收站索引落盘失败（purge 之后）：%d 条已删除但索引未更新" % n)
    _dir_usage_invalidate(TRASH_DIR)     # F5：清空回收站后，设置页占用数字要立刻更新
    r = {"ok": True, "purged": n}
    if not _saved:
        r["index_saved"] = False
        # 2026-11-05 增量复读：`n == 0` 时（key 全不在 / 文件全缺失 / 全删失败）
        # 一个文件都没删，却报「文件已删除」是第二条谎报。按实际结果分两种文案。
        r["warning"] = ("文件已删除，但回收站索引没能保存（磁盘只读或空间不足？）；"
                        "重启后这些条目可能重新出现，再清一次即可") if n else \
                       ("回收站索引没能保存（磁盘只读或空间不足？）；"
                        "本次没有文件被删除，索引仍是上一次落盘的内容")
    if unknown:
        # 全部 key 都不存在 ⇒ 这次「彻底删除」实际什么也没做，必须让调用方看见。
        # 混着来时保持 ok:true（确实删掉了存在的那些），只把不存在的列出来。
        r["unknown"] = unknown
        if n == 0 and not failed and not missing:
            r["ok"] = False
            r["error"] = "%d 个条目不在回收站中（可能已被彻底删除）" % len(unknown)
    if failed:
        r["failed"] = failed
        r["error"] = ("部分文件未能彻底删除（可能被占用或只读），"
                      "已保留在回收站列表中，可稍后重试")
    if missing:
        # B6-3：条目在、文件没了 —— 既不静默摘条目，也不谎报「已清理」。
        r["missing"] = missing
        r["error"] = ((r["error"] + "；" if r.get("error") else "")
                      + "%d 个条目的文件在回收站目录里找不到（条目已保留，"
                        "未计入清理数）" % len(missing))
    return r


def load_trash_keep_days() -> int:
    """回收站自动清理的保留天数。

    P1-3：0 = 关闭自动清理（回收站永不自动丢东西）。此前是写死的模块常量 30 天，
    到点就在启动时**静默永久删除**——用户完全无从察觉、也无从关闭，等发现时文件已没了。
    默认值保持 30（行为不放大），只是让它变得**可查、可配、可关**。
    优先级：环境变量 CMV_TRASH_KEEP_DAYS > settings.json 的 trash_keep_days > 默认 30。
    """
    raw = os.environ.get("CMV_TRASH_KEEP_DAYS")
    if raw is None or not str(raw).strip():
        raw = load_settings().get("trash_keep_days")
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return TRASH_KEEP_DAYS
    return n if n >= 0 else TRASH_KEEP_DAYS


def trash_gc(keep_days: int = None):
    """启动时清理超龄回收站条目（锁内收集、锁外清理，避免锁重入）。

    keep_days=None ⇒ 取运行时配置；<=0 ⇒ 关闭清理（什么都不做）。
    P1-3：清理**必须留痕** —— 此前是静默 delete，用户丢文件后查不到任何记录。
    """
    kd = load_trash_keep_days() if keep_days is None else int(keep_days)
    if kd <= 0:
        log("[trash] 回收站自动清理已关闭（trash_keep_days=0）")
    else:
        cutoff = time.time() - kd * 86400
        with _store_lock(TRASH_LOCK, "回收站"):
            items = load_trash()
            # B6-P2-2 / P2-3（2026-10-19）：原来是一行列表推导
            #   `dead = [k for k, e in items.items() if (e.get("t") or 0) < cutoff]`
            # 它同时有两个相反方向的毛病：
            #   · 缺 `t`（或 t=0/字符串/负）⇒ 取 0 ⇒ 判「已超龄」⇒ **下次启动就永久删除**；
            #   · 条目不是 dict ⇒ `.get` 抛 AttributeError ⇒ **整个后台清理线程静默死亡**，
            #     回收站此后无限增长且界面没有任何提示。
            # 现在的口径：**缺时间 = 不清楚 = 永不过期**（宁可留着），坏的单独点名。
            dead, notime = [], []
            for k, e in items.items():
                if not isinstance(e, dict):
                    notime.append(k)
                    continue
                t = e.get("t")
                if isinstance(t, bool) or not isinstance(t, (int, float)) or t <= 0:
                    notime.append(k)
                    continue
                if t < cutoff:
                    dead.append(k)
        if notime:
            names = []
            for k in notime[:5]:
                e = items.get(k)
                names.append(str((e.get("name") if isinstance(e, dict) else None) or k))
            more = "" if len(notime) <= 5 else f" 等 {len(notime)} 个"
            log(f"[trash] {len(notime)} 个条目缺少有效的删除时间，一律保留不自动删除："
                + "、".join(names) + more)
        if dead:
            names = [str(items[k].get("name") or k) for k in dead[:5]]
            more = "" if len(dead) <= 5 else f" 等 {len(dead)} 个"
            log(f"[trash] 自动清理 {len(dead)} 个超过 {kd} 天的条目："
                + "、".join(names) + more)
            trash_purge(dead)
        else:
            log(f"[trash] 自动清理检查完成：没有超过 {kd} 天的条目")
    # B6-1：把「不在索引里」的文件点名叫出来。⚠️ 这一段与 kd 无关 —— 自动清理
    # 关掉（kd=0）时孤儿照样存在，不能跟着 early-return 一起被跳过。
    # 只统计不删（见 trash_orphans 的说明）。此前完全没有信号：设置页占用数字
    # 比回收站列表大，用户只会以为软件算错了。
    orphan = trash_orphans()
    if orphan:
        shown = "、".join(orphan[:5]) + (f" 等 {len(orphan)} 个" if len(orphan) > 5 else "")
        log(f"[trash] 回收站里发现 {len(orphan)} 个不在索引里的文件（不显示、不自动清理，"
            f"只能手工删）：{shown}")


def trash_orphans() -> list:
    """回收站目录里「不在索引里」的文件名（B6-1 泄漏残留 + 历史孤儿）。

    这类文件最阴的地方是**三不管**：界面只列索引里的条目 ⇒ 用户看不见；
    `trash_gc` 只遍历索引 ⇒ 不会被自动清理；`trash_purge` 只 glob 索引里的
    key ⇒ 手动也清不掉。它们白占数据盘，并且让「设置页占用数字」与回收站
    列表对不上（用户会以为软件算错了）。

    这里只**列出**、不删 —— 它们不在索引里，用户无从确认来源，自动删掉比
    留着更危险。由 `trash_gc` 在启动时点名叫出来，并由设置页显示个数。
    """
    try:
        entries = load_trash()
    except Exception:                                  # noqa: BLE001 读索引失败不该拖垮统计
        entries = {}
    exact = set()
    for k, e in entries.items():
        exact.add(k)
        # B6-P2-3（2026-10-19）：条目值不是 dict 时 `.get` 会抛 AttributeError，
        # 而本函数是被 `trash_gc` 在启动线程里调的 ⇒ 整个自动清理静默死亡。
        # 坏条目的 key 仍然要算进 exact（否则它会被误当成孤儿）。
        if isinstance(e, dict):
            nm = e.get("stored")
            if nm:
                exact.add(str(nm))
    keys = list(entries)
    out = []
    try:
        for f in TRASH_DIR.iterdir():
            if not f.is_file() or f.name == "index.json" or f.name.startswith("index."):
                continue
            if f.name in exact:
                continue
            # 前缀兜底：老条目（B6-3 之前）没记 stored，落盘名是「key + 后缀」
            if any(f.name.startswith(k + ".") for k in keys):
                continue
            out.append(f.name)
    except OSError:
        return []
    return sorted(out)


def trash_list() -> list:
    """回收站列表。坏条目跳过并计数，**绝不**让一条脏索引拖垮整个界面。

    2026-11-05 P2：此前 `load_trash()` 只校验外层是 dict，值原样返回，这里直接
    `e.get(...)` —— 索引里出现一个 `"k": 1` 就抛 AttributeError，而 `/api/trash`
    没有 try ⇒ 每次 500，回收站界面永久打不开。
    """
    out = []
    bad = 0

    def _int_or(v, d=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return d

    for k, e in load_trash().items():
        if not isinstance(e, dict):
            bad += 1
            continue
        name = e.get("name")
        name = name if isinstance(name, str) and name else "?"
        orig = e.get("orig")
        ext = os.path.splitext(name)[1].lower()
        out.append({"key": k, "name": name,
                    "orig": orig if isinstance(orig, str) else "",
                    "t": _int_or(e.get("t")),
                    "size": _int_or(e.get("size")),
                    "type": "image" if ext in IMAGE_EXTS else
                            ("video" if ext in VIDEO_EXTS else "file")})
    if bad:
        log(f"[warn] 回收站索引有 {bad} 条无效条目，已跳过（其余 {len(out)} 条正常列出）")
    out.sort(key=lambda x: -x["t"])
    return out


# ---------------------------------------------------------------- 系统回收站（Windows Shell）

FO_DELETE = 0x0003
FOF_SILENT = 0x0004
FOF_NOCONFIRMATION = 0x0010
FOF_ALLOWUNDO = 0x0040          # 关键位：有它才是「移入回收站」，没有就是永久删除
FOF_NOERRORUI = 0x0400
# ⚠️ P0-1：FOF_ALLOWUNDO|FOF_NOCONFIRMATION 在「卷上没有回收站」时会被 Windows 降级成
# 永久删除，且返回码仍是 0 —— 文件不可恢复，而调用方以为成功了（exFAT / 网络映射盘 /
# 部分 USB 可移动盘 / 超配额都会走到这条路径）。加上这一位后，Windows 必须弹
# 「确实要永久删除吗」而不是静默销毁，是本函数的最后一道兜底。
FOF_WANTNUKEWARNING = 0x4000

_SHFILEOP = None                # 惰性缓存 SHFILEOPSTRUCTW（非 Windows 无 ctypes.wintypes）
_SHFILEOP_LOCK = threading.Lock()
# B6-P2-4（2026-10-19）：这两个缓存原来**按卷永久有效**，进程内从不失效。
# 后果是外接盘被弹出/换盘、或回收站配置被改（启用/禁用、配额调整）之后，程序继续
# 用旧判定：可能把「已经没有回收站」的卷当成有，于是删文件永久销毁还回报成功。
# 现在都带 TTL；过期后重新取证（不是直接信任旧值）。
RB_SUPPORT_TTL = 60.0           # 「本卷有没有系统回收站」：1 分钟复核一次
RB_LIE_TTL = 300.0              # 「本卷 shell32 返回码不可信」：5 分钟抽样复核一次

_RB_SUPPORT = {}                # P0-1：卷 → (是否有系统回收站, 判定时刻)
_RB_SUPPORT_LOCK = threading.Lock()
# N-1：卷 → Shell API 返回码是否已被「硬证据」确立为不可信（见 _rb_confirm_moved）。
# 只在拿到过 $I 取证之后才置位，不是猜的；按卷隔离，缺陷卷不影响正常卷。
_RB_LIE = {}
_RB_LIE_LOCK = threading.Lock()
# B6-2：卷 → 首次「返回 0 的成功删样本」是否已取证通过（见 _rb_verify_first_success）。
# 三重含义：True=本卷确实进了回收站 / False=本卷不接收（后续改走应用内回收站）。
_RB_SAMPLED = {}
_RB_SAMPLED_LOCK = threading.Lock()
# B6-2b（CMV3-LOCAL-FIX，2026-10-17 自 v3.0.0 回植）：卷 → (扫描时刻, _rb_records 结果)
#
# 为什么必须有：`_rb_records` 是**全量**扫本卷 $Recycle.Bin 下的所有 $I 记录。
# 原注释写的「实测本机 9966 个 $I 全量读取仅 0.49s」在**今天已完全不成立** ——
#   实测本机 C 盘 $Recycle.Bin 有 **61334 条 $I**，全量扫一次 **34.78s**
#   （G 盘 57 条 / 0.58s，D 盘 0 条）。而 /api/delete 的前端/门禁 HTTP 超时是 30s
#   ⇒ 在 C 盘上删任何一个文件都会 **超时**，界面表现为「删除卡死」，
#   门禁侧 `delmode_check` C1 直接 `TimeoutError: timed out`。
#   这不是测试环境特例：用户机器上回收站攒到几万条是常态（尤其数据盘）。
#
# 语义：TTL 内复用同一次扫描结果。`_rb_confirm_moved` / `_rb_verify_first_success`
#   判「本条记录的时刻 ts >= t_before」用的仍是记录**自带 FILETIME**，
#   缓存只影响「我们能看见哪些 $I」，不影响时间判据。
# ⚠️ **本次删除后必须主动失效**（见 _rb_records_invalidate）—— 否则刚删进去的
#   那条 $I 还在缓存里看不见 ⇒ 取证判失败 ⇒ 把「真进了回收站」谎报成失败
#   （正是 N-1 要修的那类谎报，方向相反）。**每一次删除成功/尝试后都失效。**
_RB_REC_CACHE = {}
_RB_REC_LOCK = threading.Lock()
_RB_REC_TTL = 20.0            # 秒；一次删除操作内多次取证复用，跨操作重新扫


def _rb_records_invalidate(drv: str = None):
    """作废 $I 记录缓存。本次删除**之后**必须调用（见 _RB_REC_CACHE 说明）。

    C29（2026-10-17 自 v3.0.0 回植）：缓存键已由 `drv` 改为 `(drv, 分钟桶)` ⇒ 这里必须**按前缀**删。
    否则单卷失效会静默失效 —— 刚删进去的 $I 仍在缓存里看不见，取证判失败，把
    「真进了回收站」谎报成失败（正是 N-1 要修的那一类，只是方向相反）。
    """
    with _RB_REC_LOCK:
        if drv is None:
            _RB_REC_CACHE.clear()
        else:
            for k in [k for k in _RB_REC_CACHE if k[0] == drv]:
                _RB_REC_CACHE.pop(k, None)

def _shfileop_struct():
    """SHFILEOPSTRUCTW 结构体（延迟构造）。

    ctypes.wintypes 只在 Windows 上存在，模块级引用会让非 Windows 直接导入失败。
    """
    global _SHFILEOP
    with _SHFILEOP_LOCK:
        if _SHFILEOP is None:
            from ctypes import wintypes as W

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [("hwnd", W.HWND),
                            ("wFunc", W.UINT),
                            ("pFrom", W.LPCWSTR),
                            ("pTo", W.LPCWSTR),
                            ("fFlags", ctypes.c_uint16),   # FILEOP_FLAGS 是 WORD
                            ("fAnyOperationsAborted", W.BOOL),
                            ("hNameMappings", ctypes.c_void_p),
                            ("lpszProgressTitle", W.LPCWSTR)]

            _SHFILEOP = SHFILEOPSTRUCTW
        return _SHFILEOP


def _rb_nuke_on_delete(p: Path) -> bool:
    """该卷是否被设成「不把文件放进回收站，直接永久删除」（B6-2 前置判定）。

    注册表位置（资源管理器「回收站属性」里的按卷设置）：
        HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Explorer
            \\BitBucket\\Volume\\<卷GUID>\\NukeOnDelete = 1
    卷 GUID 由 `GetVolumeNameForVolumeMountPointW` 取到（形如 `\\\\?\\Volume{GUID}\\`），
    子键名是其中的 `{GUID}` —— **本机实测确认**：C: → `{66c36c59-…}`、
    D: → `{0de88e57-…}`，两者都在 BitBucket\\Volume 下且带 `NukeOnDelete=0`。
    （个别系统上子键可能带 `Volume` 前缀，两种写法都试。）

    为什么要这道前置：`SHQueryRecycleBinW` 只看返回码，**感知不到这个配置** ——
    于是「卷确实有回收站（查询成功）但它被设成 NukeOnDelete」时，带 FOF_ALLOWUNDO
    的删除会被 Windows 静默降级成永久销毁**且返回 0**，而调用方据此回报
    「已移入系统回收站」，用户以为还能还原、实际已不可恢复。

    读不到 / 非 Windows / 取不到卷 ⇒ 返回 False（**不拦**）：这是前置拦截，漏判还有
    `FOF_WANTNUKEWARNING` 与 rc==0 的首次成功样本取证兜底；反过来误判成 True 会把
    正常卷的删除全赶去应用内回收站，代价更大。
    """
    if os.name != "nt":
        return False
    try:
        import winreg
        from ctypes import wintypes as W
        drv = os.path.splitdrive(str(p))[0]
        if not drv:
            return False
        buf = ctypes.create_unicode_buffer(64)
        kernel32 = ctypes.windll.kernel32
        kernel32.GetVolumeNameForVolumeMountPointW.argtypes = [W.LPCWSTR, W.LPWSTR, W.DWORD]
        kernel32.GetVolumeNameForVolumeMountPointW.restype = W.BOOL
        if not kernel32.GetVolumeNameForVolumeMountPointW(drv + "\\", buf, 64):
            return False
        tail = buf.value.rstrip("\\").split("\\")[-1]        # "Volume{GUID}"
        if not tail:
            return False
        cands = [tail]
        if tail.lower().startswith("volume"):
            cands.append(tail[len("Volume"):])               # "{GUID}"
        path = (r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\BitBucket\Volume")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            n = winreg.QueryInfoKey(k)[0]
            low = {winreg.EnumKey(k, i).lower(): winreg.EnumKey(k, i)
                   for i in range(n)}
        for c in cands:
            real = low.get(c.lower())
            if not real:
                continue
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    path + "\\" + real) as vk:
                    val, _t = winreg.QueryValueEx(vk, "NukeOnDelete")
                return bool(int(val))
            except (OSError, ValueError, TypeError):
                continue
        return False
    except (OSError, ValueError, TypeError, ImportError, AttributeError):
        return False


def _recycle_bin_supported(p: Path) -> bool:
    """该卷是否真的有系统回收站（P0-1 前置判定）。

    SHFileOperationW 带 FOF_ALLOWUNDO 时，若目标卷没有回收站（exFAT / 网络映射盘 /
    部分 USB 可移动盘），Windows 会**静默改成永久删除且返回 0**。所以删除前先问一次
    SHQueryRecycleBinW：查不到回收站的卷直接判失败，由 /api/delete 退回应用内回收站
    （可还原），而不是交给 Windows 悄悄销毁。

    结果按卷缓存 —— 批量删除几百个文件时只查一次，不放大开销。

    ⚠️ B6-2c（CMV3-LOCAL-FIX，2026-10-10 阶段 3）：**不能再把 SHQueryRecycleBinW
       当作第一判据** —— 那是个**聚合统计**调用，在回收站条目多时慢到不可用。
       实测本机 C 盘（$Recycle.Bin 下 61334 条 $I / 6.1 万条）：
           SHQueryRecycleBinW("C:\\\\") 单次 **11.2 ~ 17.8s**
           （同机器 G 盘 57 条：0.063s）
       而 /api/delete 的调用方（前端 fetch / 门禁 urllib）超时是 30s ⇒
       **首次在 C 盘删任何一个文件都会卡十几秒到超时**，界面表现为「删除卡死」，
       门禁侧 `delmode_check` C1 直接 `TimeoutError: timed out`。
       且它是"first-call"，缓存救不了首删。真实用户机器上回收站攒到几万条是常态。

       改为 **目录探测优先、Shell 查询兜底**：
         ① 轻量判据（O(1)、无 Shell 调用）：该卷 `$Recycle.Bin` 目录存在、
            且当前用户的 SID 子目录存在且**可写**（能建临时文件即删）。
            这三条同时成立 ⇒ 该卷按设计就支持回收站 —— 直接判 True，**不调 Shell**。
         ② 只有①不成立时才退回 SHQueryRecycleBinW（此时通常是 exFAT/网络盘等
            真没有回收站的情形，条目必为 0 ⇒ 查询本身也快）。
       保留 NukeOnDelete 注册表补强（B6-2）不变。
    """
    if os.name != "nt":
        return False
    drv = os.path.splitdrive(str(p))[0]
    if not drv:
        return False
    root = drv if drv.endswith("\\") else drv + "\\"
    with _RB_SUPPORT_LOCK:
        hit = _RB_SUPPORT.get(root)
        if hit and (time.time() - hit[1]) < RB_SUPPORT_TTL:
            return hit[0]
    ok = _rb_dir_probe(root)
    if ok is None:                         # ① 探测不出结论 ⇒ 才走慢的 Shell 查询
        ok = False
        try:
            from ctypes import wintypes as W
            shell32 = ctypes.windll.shell32

            class SHQUERYRBINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint32),
                            ("i64Size", ctypes.c_int64),
                            ("i64NumItems", ctypes.c_int64)]

            info = SHQUERYRBINFO()
            info.cbSize = ctypes.sizeof(info)
            shell32.SHQueryRecycleBinW.argtypes = [W.LPCWSTR,
                                                   ctypes.POINTER(SHQUERYRBINFO)]
            shell32.SHQueryRecycleBinW.restype = ctypes.c_long
            ok = (shell32.SHQueryRecycleBinW(root, ctypes.byref(info)) == 0)
        except (OSError, AttributeError, ValueError, TypeError):
            ok = False                 # 查不动时不拦：还有 FOF_WANTNUKEWARNING 兜底
    if ok and _rb_nuke_on_delete(p):
        # B6-2：查询成功 ≠ 删了会进回收站。卷被设成「不将文件移入回收站」时，
        # Windows 会**静默永久删除且返回 0** —— 前置判定形同虚设。
        # 按「该卷没有系统回收站」处理 ⇒ 调用方退回应用内回收站（可还原）。
        log(f"[回收站] 卷 {root} 被设为「不将文件移入回收站」（NukeOnDelete）—— "
            f"按「该卷没有系统回收站」处理，删除退回应用内回收站（可还原）")
        ok = False
    with _RB_SUPPORT_LOCK:
        _RB_SUPPORT[root] = (ok, time.time())
    return ok

def _rb_dir_probe(root: str):
    """B6-2c 轻量判据：该卷回收站**按设计可用**？

    依次确认（全部 O(1)，不调 Shell）：
      ① `<root>$Recycle.Bin` 存在且是目录；
      ② 其下有**当前用户**的 SID 子目录（Vista+ 每用户一个）；
      ③ 该 SID 目录**可写** —— 建一个临时文件随即删掉。
    三条都成立 ⇒ True（该卷支持回收站，无需再问 Shell）。
    ①②任一不成立 ⇒ False（该卷确实没有回收站目录）。
    ③失败（权限）⇒ **None**（不可判定，交由调用方走 Shell 兜底，不擅自判死）。

    为什么要「当前用户」而不是「随便哪个能写的 SID 目录」：
      `$Recycle.Bin` 下通常有多个 SID —— 实测本机 C 盘是 `S-1-5-18`（SYSTEM）
      与当前用户 `S-1-5-21-<machine>-<uid>` 两个。**只有当前用户自己的 SID 目录
      会被 Windows 用于存放他的删除**；别的用户（或 SYSTEM）的目录即使侥幸可写，
      也证明不了「本用户删文件会进回收站」——那正是 P0-1 要拦的静默永久删除。
      所以：**当前用户 SID 优先**，其次才考虑「不认识的但可写」的目录；
      已知**系统 SID**（见 _RB_SYS_SIDS）只作最后兜底，且不作为判 True 的依据。

    为什么可写性要实测：SID 目录存在但不可写时，带 FOF_ALLOWUNDO 的删除同样会被
    Windows 静默降级成永久删除 —— 这与「卷没有回收站」是同一后果。实测一次比
    依赖聚合查询便宜四个数量级（~0.5s vs 11~18s）。
    """
    base = os.path.join(root, "$Recycle.Bin")
    if not os.path.isdir(base):
        return False
    try:
        sids = os.listdir(base)
    except OSError:
        return False
    if not sids:
        return False

    def _writable(sdir):
        probe = os.path.join(sdir, ".cmvprobe")
        try:
            with open(probe, "wb") as fh:
                fh.write(b"x")
            os.unlink(probe)
            return True
        except OSError:
            return False

    # ① 当前用户 SID 优先（最可信 —— 就是 Windows 会用来存他删除的那个目录）
    me = _current_sid()
    if me:
        for sid in sids:
            if os.path.normcase(sid) != os.path.normcase(me):
                continue
            sdir = os.path.join(base, sid)
            if os.path.isdir(sdir):
                return True if _writable(sdir) else None
            return None                    # 目录名对上了但不是目录 ⇒ 不可判定
    # ② 其次：任何一个**非系统** SID 目录可写（本机常见单用户情形兜底）
    for sid in sids:
        if sid.upper() in _RB_SYS_SIDS:
            continue
        sdir = os.path.join(base, sid)
        if os.path.isdir(sdir) and _writable(sdir):
            return True
    # ③ 剩下只有系统 SID ⇒ 不可判定，交 Shell 兜底（不擅自判 True/False）
    return None


# 已知的内置账户 SID（不属于某个真实登录用户）—— 见 _rb_dir_probe 的说明。
_RB_SYS_SIDS = frozenset((
    "S-1-5-18",        # LOCAL SYSTEM
    "S-1-5-19",        # LOCAL SERVICE
    "S-1-5-20",        # NETWORK SERVICE
    "S-1-5-32-544",    # Administrators（组 SID，出现时同样不作个人依据）
))


def _current_sid():
    """当前进程登录用户的 SID 字符串（取不到返回 ""）。

    为什么走 ctypes 取令牌而不是别的路：
      · `os.getlogin()` 给的是**用户名**不是 SID，且无控制台时会抛错；
      · 起 `whoami` 子进程要 spawn 一个进程（几十 ms 且引入外部依赖）；
      · `%USERDOMAIN%\\%USERNAME%` 同样只有名字，还得再查一次映射表。
    ⇒ 直接用 `OpenProcessToken(GetCurrentProcess())` + `GetTokenInformation(TokenUser)`
      + `ConvertSidToStringSidW` —— 纯内存、无子进程、无第三方依赖。

    失败一律返回 ""（调用方按「不知道」处理，走「非系统 SID 可写」兜底），
    绝不猜一个 SID 出来 —— 猜错会把「别的用户的回收站」当成「本用户的」。
    """
    try:
        import ctypes
        from ctypes import wintypes as W

        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        TOKEN_QUERY = 0x0008
        TokenUser = 1

        # ⚠️ **必须显式声明 argtypes/restype**。不声明时 ctypes 按默认的
        #    c_int 处理返回值/指针 ⇒ `GetTokenInformation(… None, 0, &need)`
        #    这个「只为取大小」的调用会拿不到内存、`need.value` 恒 0 ⇒ 整条链
        #    静默返回 ""（本机实测：不声明时 SID 取不到，声明后正常拿到）。
        advapi32.OpenProcessToken.argtypes = [W.HANDLE, W.DWORD,
                                              ctypes.POINTER(W.HANDLE)]
        advapi32.OpenProcessToken.restype = W.BOOL
        advapi32.GetTokenInformation.argtypes = [W.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, W.DWORD,
                                                 ctypes.POINTER(W.DWORD)]
        advapi32.GetTokenInformation.restype = W.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                                    ctypes.POINTER(W.LPWSTR)]
        advapi32.ConvertSidToStringSidW.restype = W.BOOL

        h = W.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                         TOKEN_QUERY, ctypes.byref(h)):
            return ""
        try:
            need = W.DWORD(0)
            advapi32.GetTokenInformation(h, TokenUser, None, 0, ctypes.byref(need))
            if not need.value:
                return ""
            buf = ctypes.create_string_buffer(need.value)
            if not advapi32.GetTokenInformation(h, TokenUser, buf, need.value,
                                                ctypes.byref(need)):
                return ""
            # TOKEN_USER 的第一个成员就是 SID_AND_ATTRIBUTES.Sid（一个指针）
            sid_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            if not sid_ptr:
                return ""
            strsid = W.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(sid_ptr),
                                                   ctypes.byref(strsid)):
                return ""
            try:
                return strsid.value or ""
            finally:
                kernel32.LocalFree(strsid)
        finally:
            kernel32.CloseHandle(h)
    except (OSError, AttributeError, ValueError, TypeError):
        return ""



# ---------------------------------------------------------------------------
# N-1：系统回收站「谎报失败」的独立取证
#
# 实测（2026-09-19 本机 Win11，见 _artifacts/reports/audit_fix_n1_20260919/）：
#   · 文件**确实成功移入回收站**时，shell32!SHFileOperationW 返回 2；
#   · 而 2 同时又是 ERROR_FILE_NOT_FOUND ——「路径根本不存在」的返回码。
#   · 真失败长这样：文件被独占打开时返回 32（ERROR_SHARING_VIOLATION），且原路径仍在。
# 也就是说**返回码在这个 API 上没有任何区分力**，成功与否只能独立取证。
#
# 取证链两道，缺一不可：
#   ① 原路径必须已经消失 —— 文件还在就一律算失败（实测独占锁定即此种情形）；
#   ② 本卷回收站里必须有一条**点名该路径**、且删除时刻晚于本次调用前的 $I 记录。
#
# ⚠️ 绝不能只凭「文件不见了」判成功：永久删除同样会让文件消失，而那正是 P0-1
#    要拦住的事（没有回收站的卷会被 Windows 静默降级成永久删除）。
# ---------------------------------------------------------------------------
# C29（2026-10-17 自 v3.0.0 回植）：$I 扫描的可扩展性
# ---------------------------------------------------------------------------
# 本机 C 盘 $I 已到 191,372 条（实测单条 open+read 0.21ms ⇒ 全量一次 ≈41s），而客户端
# 是 urlopen(timeout=30) ⇒ 必然超时（C26 取证：delmode_check C 段 30s 超时、
# p1bfix_recycle_check 跑 526.9s）。三处改动，**不放松任何判据**：
#   ① 元数据预筛（只 stat、不 open）：$I 是 Windows 在「移入回收站」那一刻**新建**的
#      544 字节文件，其 st_ctime 即入站时刻；只对 ctime >= since - SLACK 的条目做
#      open+解析。Windows 上 os.scandir 的 DirEntry.stat() 不额外发系统调用
#      （FindFirstFile 已带回 WIN32_FIND_DATA），故 19 万条预筛 < 1s。
#   ② 命中的条目并行解析（多核摊开）。
#   ③ **预筛为空 ⇒ 退化为全量扫** —— 硬约束：早年注释明确否定过「只扫 mtime 较新
#      的 $I」，理由是 $I 若保留原文件老时间戳就会漏掉真记录（= 谎报失败）。本实现
#      用 ctime（**创建时刻**，不是 mtime）+「空集退化」双重兜住：真失败路径下预筛
#      必然为空 ⇒ 老老实实全量，绝不因筛掉而放行。
# 返回语义：since 非空时只含「入站时刻不早于 since - SLACK」的子集。两个调用方
#   （_rb_confirm_moved / _rb_verify_first_success）都只做 recs.get(目标路径)，
#   已逐一核对 ⇒ 与返回全集对判据等价。
_RB_CTIME_SLACK = 120.0        # 秒；客户端与文件系统时钟可能有偏差，留宽裕余量
_RB_POOL = None
_RB_POOL_LOCK = threading.Lock()


def _rb_pool():
    """本模块共用的 $I 解析线程池（按需创建，进程退出时由解释器回收）。"""
    global _RB_POOL
    with _RB_POOL_LOCK:
        if _RB_POOL is None:
            _RB_POOL = concurrent.futures.ThreadPoolExecutor(
                max_workers=min(8, (os.cpu_count() or 4) * 2),
                thread_name_prefix="rb-scan")
        return _RB_POOL


def _rb_parse_one(path):
    """解析单条 $I，返回 (规范化原路径, 删除时刻 unix)；读不动/格式不符返回 None。

    $I 记录布局（Vista+，version>=2）：
        0..8   版本号      8..16  文件大小（字节）   16..24 删除时刻(FILETIME)
        24..28 路径字符数  28..    UTF-16LE 原路径
    （version 1 的旧记录在 24 处是定长 260 字符路径，一并兜住。）
    """
    try:
        with open(path, "rb") as fh:
            b = fh.read(4096)
    except OSError:
        return None
    if len(b) < 24:
        return None
    try:
        ft = int.from_bytes(b[16:24], "little")
        if ft <= 0:
            return None
        ts = (ft - 116444736000000000) / 1e7      # FILETIME → unix 秒
        ver = int.from_bytes(b[0:8], "little")
        if ver >= 2:
            if len(b) < 28:
                return None
            n = int.from_bytes(b[24:28], "little")
            raw = b[28:28 + n * 2]
        else:
            raw = b[24:24 + 520]
    except (ValueError, OverflowError):
        return None
    orig = raw.decode("utf-16-le", "replace").split("\x00")[0]
    if not orig:
        return None
    return os.path.normcase(orig.strip("\\")), ts

def _rb_records(drv: str, since: float = None):
    """扫该卷回收站的 $I 记录，返回 {规范化原路径: 删除时刻(unix)}；查不动返回 None。

    用记录**自带的 FILETIME** 而不是文件 mtime 判「是不是这次删的」：实测同一路径删
    两次会留下两条记录，只有时刻能区分（实测该字段精度优于 0.1s，与系统时钟一致）。

    ⚠️ 返回 None 是**硬信号** —— 调用方必须据此判失败。绝不能因为「查不到证据」就
    放行，那等于把 P0-1 的防线拆了。

    缓存：按 (卷, since 的分钟桶) 维度做 TTL 缓存 —— 同一次批量删除里的多次取证
    共享一份结果；跨桶重新扫。since 为 None（全量）也各占一档。
    ⚠️ C31：命中缓存还要求「快照时刻 >= since」—— 快照早于窗口起点就不可信（见下文）。

    B6-2b（2026-10-17 自 v3.0.0 回植）：加按卷 TTL 缓存。
    C29（2026-10-17 自 v3.0.0 回植）：预筛 + 并行 + 空集退化（见文件上方 C29 说明块）。
    C31（2026-10-17 自 v3.0.0 回植）：缓存可信性判据（born >= since）—— 修 C29 分钟桶缓存漏判。"""
    if since:
        bucket = int(since // 60)                     # 分钟桶：同批删除共享缓存
        cutoff = bucket * 60.0 - _RB_CTIME_SLACK      # 比 since 更保守（宁可多留）
    else:
        bucket, cutoff = None, None
    ck = (drv, bucket)
    with _RB_REC_LOCK:
        hit = _RB_REC_CACHE.get(ck)
        if hit is not None and (time.time() - hit[0]) < _RB_REC_TTL:
            # C31（2026-10-17 自 v3.0.0 回植）：快照**必须不早于窗口起点**（born >= since）才可信。
            #   否则 (born, since) 之间新落进回收站的 $I 不在快照里 ⇒ 把「真进了回收站」
            #   判成「查不到该路径的记录」⇒ 原样复现 N-1 的谎报失败（方向相反）。
            #   事故实测（p1bfix_recycle_check B11）：B10 直调 _rb_confirm_moved 建立了
            #   分钟桶快照（born=T10），紧接 B11 在**同一桶内**删除一个**新文件**并取证
            #   （since=T11 > T10）⇒ 命中陈旧快照 ⇒ recycle_bin_delete 返回「错误码 2」。
            #   ⚠️ 这条判据比「调用方记得 invalidate」更根本：_rb_records 是只读函数，
            #      不该把「删除后必须作废缓存」的责任外包给每一个调用方。
            #   `since is None`（全量调用；产品代码不用，只有门禁/调试用）沿用原 TTL 语义。
            if since is None or hit[0] >= since:
                return hit[1]
    root = os.path.join(drv, "$Recycle.Bin")
    try:
        sids = os.listdir(root)
    except OSError:
        return None                       # 连目录都列不出来 ⇒ 查不动

    # ---- ① 元数据预筛（只 stat，不 open）----
    cand, n_i = [], 0
    denied = set()                       # A09：哪些 SID 目录读不进去（见下方判据）
    for sid in sids:
        sdir = os.path.join(root, sid)
        try:
            with os.scandir(sdir) as it:
                for e in it:
                    if not e.name.startswith("$I"):
                        continue
                    n_i += 1
                    if cutoff is not None:
                        try:
                            if e.stat(follow_symlinks=False).st_ctime < cutoff:
                                continue
                        except OSError:
                            continue      # stat 不动 ⇒ 当不满足（下面 ③ 兜）
                    cand.append(e.path)
        except OSError:
            denied.add(sid)
            continue                      # 其他用户的 SID 目录可能拒绝访问

    # ---- ③ 预筛为空 ⇒ 退化为全量（绝不因筛掉而放行）----
    if cutoff is not None and not cand and n_i:
        log("[回收站] 预筛为空（%d 条 $I 的 ctime 都早于 %.1f）⇒ 退化为全量扫，"
            "确保不因预筛而漏判" % (n_i, cutoff))
        for sid in sids:
            sdir = os.path.join(root, sid)
            try:
                with os.scandir(sdir) as it:
                    cand.extend(e.path for e in it if e.name.startswith("$I"))
            except OSError:
                denied.add(sid)
                continue

    # ---- ③.5 读不到 ≠ 没有记录（A09，2026-11-05 审计复核）----
    # 旧行为：SID 目录读不进去就静默 `continue`。若整个 $Recycle.Bin 都读不动
    #（权限异常 / 卷策略 / 加密盘），函数会返回 **{}** —— 而调用方
    # `_rb_confirm_moved` 把 `{}` 读作「确实没有这条记录」⇒ 明明进了系统回收站
    # 也被报成失败，上层再去 trash_move（文件已不在原处）⇒ 用户看到「删除失败」，
    # 文件却躺在系统回收站里。本函数的契约写得很清楚：**查不动返回 None**，
    # 调用方据此保守判失败 —— 现在把它真正兑现。
    # 判据取「一条 $I 都没读到且有目录被拒」，而不是「只要有一个目录被拒就 None」：
    # 普通用户的 `$Recycle.Bin` 下常驻 SYSTEM / 其他账户的 SID 目录，一票否决会让
    # 每次删除都判失败（本卷所有删除被迫降级到应用内回收站），那是拿可用性换正确性。
    # 我们删的文件只会记在**本用户** SID 下，本用户目录读得到就不影响判据。
    if denied and n_i == 0:
        log("[回收站] %d/%d 个 SID 目录拒绝访问且一条 $I 都没读到 ⇒ 视为查不动（%s）"
            % (len(denied), len(sids), drv))
        return None
    if denied:
        log("[回收站] %d/%d 个 SID 目录拒绝访问（多为其他账户），本次读到 %d 条 $I（%s）"
            % (len(denied), len(sids), n_i, drv))

    # ---- ② 并行解析 ----
    res = {}
    if cand:
        for r in _rb_pool().map(_rb_parse_one, cand, chunksize=256):
            if r is None:
                continue
            key, ts = r
            if ts > res.get(key, 0.0):    # 同路径多条记录时取最新那条
                res[key] = ts
    with _RB_REC_LOCK:
        _RB_REC_CACHE[ck] = (time.time(), res)
    return res



def _rb_confirm_moved(p: Path, t_before: float) -> bool:
    """N-1：SHFileOperationW 返回码异常时，独立取证「它到底进没进回收站」。

    两道证据都成立才返回 True（判据细节见上面的长注释）。
    证据②只在**本卷首次**遇到返回码异常时做，否则批量删几千个文件会扫几千遍回收站；
    之后该卷走 _RB_LIE 缓存 —— 但证据①（原路径是否消失）**每次都做**，不省。
    """
    if os.path.lexists(str(p)):                       # ① 文件还在 ⇒ 一律失败
        return False
    drv = os.path.splitdrive(str(p))[0]
    if not drv:
        return False
    root = drv if drv.endswith("\\") else drv + "\\"
    with _RB_LIE_LOCK:
        rec = _RB_LIE.get(root)
        if rec and (time.time() - rec[1]) < RB_LIE_TTL:
            return True                               # 本卷已确立：① 成立即可判成功
        # 过了 TTL 就重新取证一次（B6-P2-4）：卷可能被换成另一个（盘符复用），
        # 或回收站被关掉了。留着旧结论等于永久跳过证据②。
    recs = _rb_records(root, since=t_before)
    if recs is None:
        return False                                  # 查不动 ⇒ 保守判失败
    ts = recs.get(os.path.normcase(str(p).strip("\\")))
    if ts is None or ts < t_before:                   # ② 没有点名本路径的新记录
        return False
    with _RB_LIE_LOCK:
        _RB_LIE[root] = (True, time.time())
    log("[回收站] %s 已进回收站（$I 记录取证成立），但 shell32 返回码不可信；"
        "本卷后续删除不再信任返回码（原路径是否消失仍逐次校验）" % p)
    return True


def _rb_verify_first_success(p: Path, t_before: float) -> bool:
    """B6-2：`rc == 0 && !fAnyOperationsAborted` 分支的**首次成功样本取证**（每卷一次）。

    为什么连「返回 0 且未中止」也不能直接信：卷**确实有回收站**（SHQueryRecycleBinW
    查询成功）但它被设成「不将文件移入回收站」（`NukeOnDelete`）/ 回收站配额为 0 /
    当前用户 SID 目录不可写时，带 `FOF_ALLOWUNDO` 的删除会被 Windows **静默降级成
    永久销毁，且仍然返回 0**。此时回报成功 = 界面弹「已移入系统回收站」，
    用户以为还能还原，实际已不可恢复 —— 正是 P0-1 想拦住的同一类事故，只是绕过了
    「该卷有没有回收站」那道前置判定（`_rb_nuke_on_delete` 拦住了最常见的
    NukeOnDelete 配置，这里是它漏判时的第二道）。

    判据与 N-1 的取证链同口径：① 原路径必须已消失；② 本卷 `$I` 里必须有一条点名该
    路径、且时刻不早于调用前的记录。都成立才判「真的进了回收站」。

    ③ `$I` **读不动**（权限）时不能判失败 —— 那会把「查不到」变成「一定没进」，
       属另一种谎报；这种情况沿用旧行为（信返回码）且不再重复扫。

    开销：每卷只在**首次**成功删除时扫一次 `$I`（本机实测 9966 条 0.49s），
    之后走 `_RB_SAMPLED` 缓存 ⇒ 批量删几千个文件只多这一次。
    但**证据①（原路径是否消失）每次都做，缓存不参与**（2026-11-05 增量复读修）：
    缓存只回答「本卷回收站会不会接收文件」，与「这一次文件到底还在不在」无关；
    先查缓存再 lexists 会让命中缓存的后续删除直接回报成功，即使文件其实没删成
    （被占用 / 只读 / 杀软拦截），于是调用方不退回应用内回收站 ⇒ 界面上「已删除」、
    文件还在原地。lexists 是一次 stat，代价可忽略。
    """
    if os.path.lexists(str(p)):          # ① 文件还在 ⇒ 这次操作其实没发生，如实报失败。
        return False                     #    不写缓存：单次异常不该把整个卷长期判死。
    drv = os.path.splitdrive(str(p))[0]
    root = drv + "\\" if drv and not drv.endswith("\\") else drv
    with _RB_SAMPLED_LOCK:
        if root in _RB_SAMPLED:
            return _RB_SAMPLED[root]
    recs = _rb_records(root, since=t_before) if root else None
    if recs is None:                     # ③ 查不动 ⇒ 不可判定，沿用旧行为（且不再重扫）
        with _RB_SAMPLED_LOCK:
            _RB_SAMPLED[root] = True
        log("[回收站] 读不到本卷 $I 记录，首次成功样本无法取证 —— "
            "本次沿用 SHFileOperationW 返回码，之后不再重复扫描")
        return True
    ts = recs.get(os.path.normcase(str(p).strip("\\")))
    ok = ts is not None and ts >= t_before
    with _RB_SAMPLED_LOCK:
        _RB_SAMPLED[root] = ok
    if not ok:
        log(f"[error] 卷 {root} 的系统回收站没有接收文件（$I 里查不到 {p} 的记录）—— "
            f"该卷疑似「不将文件移入回收站」或回收站配额已满；本卷后续删除改用"
            f"应用内回收站。⚠️ 本次删除的文件已不可恢复")
    return ok


def recycle_bin_delete(p: Path):
    """把单个文件移入 Windows 系统回收站。成功返回 None，失败返回错误信息。

    走 shell32!SHFileOperationW（系统自带 API，不引第三方依赖、不增打包体积）。
    两个易踩的点：
      ① 请求跑在 ThreadingHTTPServer 的子线程上，Shell API 要求线程先初始化 COM，
         所以显式 CoInitializeEx / CoUninitialize（本线程自己初始化的才反初始化）；
      ② SHFileOperationW 的 pFrom 要求**双 NUL 结尾**的多字符串，少一个 NUL 会读到垃圾。

    ⚠️ 第三点（P0-1）：「进不了回收站」必须如实报错，绝不能回报成功 —— 否则调用方
       不会退回应用内回收站，文件就永久没了，而界面弹的是「已移入系统回收站」。
       两道保险：前置 SHQueryRecycleBinW 判卷 + fFlags 带 FOF_WANTNUKEWARNING。

    ⚠️ 第四点（N-1）：反过来的谎报也存在 —— 文件**确实**进了回收站时 shell32
       仍返回 2，导致「删除成功」被报成失败：界面弹红，调用方还去试注定失败
       的 trash_move，最终把「已删除」报成「删除失败」。返回码非 0 时改用
       _rb_confirm_moved 独立取证；取证不成立仍按原样报错（宁误报失败，
       绝不误报成功）。

    ⚠️ 第五点（B6-2）：**返回 0 也不等于进了回收站**。前置判定只看
       SHQueryRecycleBinW 的返回码，感知不到该卷被设成「不将文件移入回收站」
       （NukeOnDelete，见 _rb_nuke_on_delete）——那种卷会静默永久删除且返回 0。
       两道补强：① 前置判定先读注册表的 NukeOnDelete；② 每次「首次成功删除」
       仍做一次 $I 样本取证（_rb_verify_first_success，按卷缓存）。
    """
    if os.name != "nt":
        return "当前系统没有 Windows 回收站"
    if not p.is_file():
        return "文件不存在或已被删除"
    if not inside_root(p):
        return "文件在根目录之外，拒绝删除"
    if not _recycle_bin_supported(p):
        # 这个卷没有回收站：SHFileOperationW 会静默永久删除，宁可报错让上层降级
        return "该卷没有系统回收站，已改用应用内回收站"
    try:
        from ctypes import wintypes as W
        ole32 = ctypes.windll.ole32
        shell32 = ctypes.windll.shell32
    except (OSError, AttributeError) as ex:
        return f"系统回收站不可用: {ex}"
    hr = ole32.CoInitializeEx(None, 0x2)          # COINIT_APARTMENTTHREADED
    own_com = hr in (0, 1)                        # S_OK / S_FALSE 才是自己初始化的
    try:                                          # RPC_E_CHANGED_MODE 时沿用既有模型
        op = _shfileop_struct()()
        op.wFunc = FO_DELETE
        op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_NOERRORUI | FOF_SILENT
                     | FOF_WANTNUKEWARNING)   # P0-1：禁止静默永久删除
        buf = ctypes.create_unicode_buffer(str(p) + "\0")   # 末尾再加 ctypes 自带的 NUL
        op.pFrom = ctypes.cast(buf, W.LPCWSTR)              # 必须留引用，否则被 GC 回收
        shell32.SHFileOperationW.restype = ctypes.c_int
        shell32.SHFileOperationW.argtypes = [ctypes.POINTER(_shfileop_struct())]
        # N-1：取证窗口起点。**刻意不留容差** —— 实测 $I 内的删除时刻(FILETIME)与
        # time.time() 一致到 0.1s 以内，而 shell 记录时刻必然发生在本行之后，所以
        # 「记录时刻 >= t_before」是严格成立的。任何向后容差都只是**假成功窗口**：
        # 容差内用户以前删掉同一路径留下的旧记录会被当成本次删除的证据。
        t_before = time.time()
        rc = shell32.SHFileOperationW(ctypes.byref(op))
        if rc == 0 and not op.fAnyOperationsAborted:
            # B6-2（2026-10-09 全代码审计）：**返回 0 不等于进了回收站** ——
            # 「不将文件移入回收站」/ 配额为 0 的卷会静默永久删除且照样返回 0。
            # 每卷首次成功删除时做一次 $I 样本取证（之后走缓存，批量删除只多这一次）。
            if _rb_verify_first_success(p, t_before):
                return None
            return ("系统回收站没有接收文件（本卷疑似「不将文件移入回收站」或"
                    "回收站配额已满），已改用应用内回收站")
        # ---- N-1：返回码非 0 **不等于**失败 ----------------------------------
        # 本机实测：成功进回收站也返回 2，而 2 同时又是「路径不存在」的返回码 ——
        # 返回码在这里没有任何区分力，必须独立取证（见 _rb_confirm_moved）。
        if _rb_confirm_moved(p, t_before):
            return None
        if rc:
            return f"系统回收站返回错误码 {rc}"
        return "系统回收站操作被中止"
    except (OSError, ValueError, TypeError) as ex:
        return f"移入系统回收站失败: {ex}"
    finally:
        # B6-2b（2026-10-17 自 v3.0.0 回植）：**每一次删除尝试之后都必须作废本卷 $I 缓存** ——
        # 刚才那次 SHFileOperationW 可能刚写进一条新 $I，缓存里看不见它；
        # 若不作废，紧随其后的取证（_rb_verify_first_success / _rb_confirm_moved）
        # 会判「查不到该路径的记录」⇒ 把真进了回收站报成失败（N-1 同类谎报，方向相反）。
        # 放在 finally ⇒ 覆盖上面**全部** return 分支（含异常与提前 return）。
        _drv = os.path.splitdrive(str(p))[0]
        if _drv:
            _rb_records_invalidate(_drv if _drv.endswith("\\") else _drv + "\\")
        if own_com:
            try:
                ole32.CoUninitialize()
            except OSError:
                pass


# ---------------------------------------------------------------- 查重（dhash 感知哈希）

# 2026-11-15 · 查重三连的第一刀（覆盖率 / 解码成本 / 结论可信度）。
#
# 先量再改（本机 11942 张 4K PNG 实测）：单张 dHash 42.2ms，其中**解码独占 35.4ms**
# （93%）；`convert("L")` 1.6ms、`resize` 0.7ms、`exif_transpose` 1.8ms。也就是说
# 「把 9×8 算快一点」毫无意义 —— 瓶颈是必须把整张 PNG 解出来。
# 有两条真正有效的路：① 线程并行（PNG 解码在 zlib 里释放 GIL，8 线程实测 5.8x）；
# ② 把结果缓存住，重复扫描只 stat、不解码。
#
# 缩略图缓存也试过，**否决**：本机命中率仅 15%（只有「浏览过的图」才有缩略图），
# 而且 320px 缩略图算出的哈希与原图有 15% 相差 1 位 —— 会把「完全重复」悄悄降级成
# 「相似」，换 15% 的覆盖率不值。宁可慢一次，也要让 dHash 与原图一一对应。
DUP_HASH_FILE = DATA_DIR / "dupe_hashes.json"
DH_VER = 2                          # 算法或取值口径一变就 +1，旧缓存自动作废
DH_WORKERS = max(1, min(8, os.cpu_count() or 4))
_DH = {"map": None, "dirty": False}
_DH_LOCK = threading.Lock()


def _dh_load():
    """载入 dHash 缓存（惰性、进程内单例）。结构 `{path: [mtime_ns, size, hash]}`。"""
    with _DH_LOCK:
        if _DH["map"] is not None:
            return _DH["map"]
        m = {}
        try:
            with io.open(DUP_HASH_FILE, "r", encoding="utf-8") as f:
                j = json.load(f)
            if int(j.get("ver") or 0) == DH_VER and isinstance(j.get("map"), dict):
                m = j["map"]
        except (OSError, ValueError, TypeError):
            m = {}                   # 没有缓存 / 坏了 ⇒ 当作空缓存重算，绝不因此报错
        _DH["map"] = m
        return m


def _dh_map():
    m = _DH["map"]
    return m if m is not None else _dh_load()


def _dh_hit(m, fp, st):
    """缓存命中返回哈希，否则 None。判重口径与索引一致：mtime_ns + size。

    只用秒级 mtime 会把「同一秒内原地覆盖成同尺寸」判成没变（NSFW 缓存踩过同样的坑）。
    """
    e = m.get(fp)
    if isinstance(e, list) and len(e) == 3 and e[0] == st.st_mtime_ns and e[1] == st.st_size:
        try:
            return int(e[2])
        except (TypeError, ValueError):
            return None
    return None


def _dh_store(fp, st, h):
    _dh_map()[fp] = [st.st_mtime_ns, st.st_size, h]
    _DH["dirty"] = True


def _dh_save(root_prefix=None, prune=False):
    """落盘（tmp + fsync + replace）。失败只记日志：缓存是加速手段，不能影响查重本身。"""
    if not _DH["dirty"] or _DH["map"] is None:
        return
    with _DH_LOCK:                   # 两个标签页同时查重时串行化落盘，避免互相覆盖
        if not _DH["dirty"] or _DH["map"] is None:
            return
        m = _DH["map"]
        out = m
        if prune and root_prefix:
            # 本次是**未截断**的扫描 ⇒ 该子树里已不存在的文件，缓存条目可以安全清掉。
            # 截断扫描绝不 prune：没扫到的部分不代表文件没了。
            pref = os.path.normcase(root_prefix)
            out = {k: v for k, v in m.items()
                   if not os.path.normcase(k).startswith(pref) or os.path.isfile(k)}
        tmp = None
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            # 唯一临时名：固定名在「两个请求同时落盘」时会互相截断，可能写出半个 JSON
            fd, tmp = tempfile.mkstemp(dir=str(DATA_DIR), prefix=".dupe_hashes_", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"ver": DH_VER, "map": out}, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, DUP_HASH_FILE)
            tmp = None
            _DH["map"] = out
            _DH["dirty"] = False
        except OSError as ex:
            log(f"[dupes] 哈希缓存写入失败（不影响本次结果）：{ex}")
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


def _dhash(p: Path) -> int:
    """dHash 64 位感知哈希：9x8 灰度相邻像素比较。

    ⚠️ 取像素的方式是**跨版本契约**：改了 convert/resize 的顺序，历史缓存与本轮结果
    就会对不上，「完全重复」的判定随之漂移。因此这里只做不改变像素的优化。
    """
    from PIL import ImageOps
    with Image.open(p) as im:
        # PNG 基本没有方向标记（本机 80/80 无），而 exif_transpose 在无标记时会复制
        # 一份全尺寸图（4K≈1.8ms/张）。只在真有标记时才转 —— 像素与哈希都不变。
        try:
            _ori = im.getexif().get(0x0112, 1)
        except Exception:
            _ori = 1
        if _ori not in (0, 1):
            im = ImageOps.exif_transpose(im)
        g = im.convert("L").resize((9, 8))
        px = g.tobytes()                 # mode "L" ⇒ 每像素 1 字节，行优先（比 getdata 快且不触发弃用告警）
    bits = 0
    for r in range(8):
        row = r * 9
        for c in range(8):
            bits = (bits << 1) | (1 if px[row + c] > px[row + c + 1] else 0)
    return bits


def find_dupes(path: Path, recursive: bool, limit: int = 3000,
               near: int = 6) -> dict:
    """扫描目录找相似图片：精确哈希分桶后，桶代表间按汉明距离 ≤ near 合并。

    `limit` 是**扫描文件数**上限，`limit <= 0` 表示不设上限（全库）。此前后端把 3000
    写死、端点也不收参数，11942 张的库永远只能覆盖 25%，而用户看到的却是「没有重复图」。
    """
    cap = limit if limit and limit > 0 else None      # None = 不设上限
    files = []
    stack = [(str(path), 0)]
    # 2026-11-08：查重也按隐藏名单剪枝 —— 否则「检测出 300 组重复」里有一半是
    # 用户明确不想管的备份目录，误导性比少检更糟。
    hid = hidden_snapshot()
    # ε-12（§6.3）：递归遍历加 realpath 防环（与 _index_builder 同款口径）。
    # 此前只有深度上限 —— junction/符号链接自环会让同一目录反复入栈，
    # limit=3000 的扫描预算被环目录吃满，真正的图反而一张都扫不到。
    visited = {os.path.realpath(str(path))}
    while stack and (cap is None or len(files) < cap):
        d, lvl = stack.pop()
        try:
            with os.scandir(d) as it:
                # 4.4-7（2026-11-05 审计）：limit 的截断点原先取决于 scandir 的返回
                # 顺序，同一图库两次查重可能扫到不同子集（结果不可复现）。按名字
                # 排序后再取，至少让「先扫到哪些」是确定的。
                for e in sorted(it, key=lambda x: (x.name.lower(), x.name)):
                    if cap is not None and len(files) >= cap:
                        break
                    try:
                        if e.is_dir():
                            if recursive and lvl < 10 and not _hid_hit(hid, e.path):
                                rp = os.path.realpath(e.path)
                                if rp not in visited:
                                    visited.add(rp)
                                    stack.append((e.path, lvl + 1))
                        elif os.path.splitext(e.name)[1].lower() in IMAGE_EXTS:
                            files.append(e.path)
                    except OSError:
                        continue
        except OSError:
            continue
    # 2026-11-15：哈希阶段改成「缓存优先 + 线程池」。原来是一张接一张地解原图，
    # 8 线程实测 42 → 7.3 ms/张；缓存命中时只 stat（本机全量第二轮约 1.5s）。
    cmap = _dh_map()
    got = {}                         # fp -> (hash, stat)
    todo = []                        # 缓存未命中，交给线程池
    bad = set()                      # 解不了 / stat 不了（不进比对，必须让人知道）
    for fp in files:
        try:
            st = os.stat(fp)
        except OSError:
            bad.add(fp)
            continue
        h = _dh_hit(cmap, fp, st)
        if h is None:
            todo.append((fp, st))
        else:
            got[fp] = (h, st)
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=DH_WORKERS) as ex:
            futs = {ex.submit(_dhash, Path(fp)): (fp, st) for fp, st in todo}
            for fu in concurrent.futures.as_completed(futs):
                fp, st = futs[fu]
                try:
                    h = fu.result()
                except Exception:            # 坏图 / Pillow 解不了 / 权限不足
                    bad.add(fp)
                    continue
                got[fp] = (h, st)
                _dh_store(fp, st, h)
    # 按**扫描顺序**回填（而不是完成顺序）：`failed_names` 与分组结果因此可复现，
    # 不会因为线程调度不同两次扫描报出不同的前 5 个失败文件名。
    # 原来这里是裸 `continue` —— 这些文件完全不参与比对，返回 `groups: []`，
    # 用户看到「没有重复图」就当成结论，其实是静默漏检。安全方向（不会误删）
    # 但结论不可信，所以把失败数如实带出去（与 truncated 同一个口径）。
    failed = [os.path.basename(fp) for fp in files if fp in bad]
    for nm in failed[:5]:
        log(f"[dupes] 跳过无法解码的图 {nm}")
    exact = {}                       # hash -> [(hash, path, stat), ...]
    for fp in files:
        e = got.get(fp)
        if e:
            exact.setdefault(e[0], []).append((None, fp, e[1]))
    # 桶间近邻合并：**组内全对**距离都 ≤ near 才并入一个组（B6-5）
    #
    # 为什么不是「只跟组代表比」：那样组内两两距离可以到 2*near。实测复刻该分组逻辑
    # （dev 的合成哈希实验）：三张图 A/B/C 满足 d(A,B)=6、d(A,C)=6、d(B,C)=12
    # （sorted 之后 A 当代表）会并成**同一组**，组内最大两两距离 12 = near 的两倍 ——
    # 64 位里 19% 比特不同，很可能是同场景不同曝光 / 不同裁剪 / 加了水印的**不同图**，
    # 而前端把整组当「重复图」默认保留第一张、删其余 ⇒ 用户按「重复」一键删就是真丢图。
    # 全对检查维持不变量「组内任意两张距离都 ≤ near」，从根上消灭 2*near 这个野值。
    # （注：阈值本身不松 —— 独立比特模型下随机 64 位两两距离 ≤6 的比例实测 0/200000。）
    groups = []                      # [ [entries...] ]
    for h in sorted(exact):
        placed = None
        for g in groups:
            if all(bin(m[0] ^ h).count("1") <= near for m in g):
                placed = g
                break
        if placed is None:
            placed = [(h, x[1], x[2]) for x in exact[h]]
            groups.append(placed)
        else:
            placed.extend((h, x[1], x[2]) for x in exact[h])
    out = []
    for g in groups:
        if len(g) < 2:
            continue
        items = [{"path": fp, "name": os.path.basename(fp),
                  "size": st.st_size, "mtime": int(st.st_mtime)}
                 for _, fp, st in g]
        items.sort(key=lambda x: -x["mtime"])       # 最新在前，前端默认保留第一张
        # B6-5：把「是不是完全重复」与「组内最大距离」如实带出去 —— 前端据此把
        # 近似组与完全重复组分开呈现，并且**不给近似组「一键全删」的入口**
        # （近似组里可能是不同图，全删 = 真丢图）。组太大时不作结论（maxdist=-1）。
        hs = [m[0] for m in g]
        md = 0
        if len(hs) <= 1000:
            for i in range(len(hs)):
                hi = hs[i]
                for j in range(i + 1, len(hs)):
                    d = bin(hi ^ hs[j]).count("1")
                    if d > md:
                        md = d
        else:
            md = -1
        out.append({"items": items, "exact": (md == 0), "maxdist": md})
    out.sort(key=lambda g: -len(g["items"]))
    truncated = (cap is not None and len(files) >= cap) or len(out) > 100
    # 只有「没被截断」的扫描才有资格 prune：截断时没扫到的部分不代表文件没了。
    # 另外只在**当前图库内**落盘：门禁/脚本会把 find_dupes 当普通函数调（夹具在临时目录），
    # 那种调用绝不该把临时路径写进用户真实数据目录（实测 e12 一行就写进去 40 条）。
    _root = STATE.get("root") or ""
    _cacheable = bool(_root) and os.path.normcase(str(path)).startswith(
        os.path.normcase(str(_root)))
    if _cacheable:
        _dh_save(str(path), prune=(cap is None or len(files) < cap))
    return {"groups": out[:100], "scanned": len(files),
            # ε-11（§6.4）：组数截断（>100 组丢弃）此前不带任何信号，
            # truncated 只反映扫描上限 —— 两种截断都要如实上报。
            "truncated": truncated,
            "limit": cap or 0,          # 0 = 本次不设上限（全库）
            # B6-P2-5：解码失败/读不了的图数量 + 前几个名字。前端据此提示
            # 「有 N 张图无法比对，本次结论不完整」，而不是让人把空结果当结论。
            "failed": len(failed), "failed_names": failed[:5]}


# ---------------------------------------------------------------- 目录签名（自动刷新）

_SIG = {"key": None, "at": 0.0, "sig": ""}
_SIG_LOCK = threading.Lock()
SIG_TTL = 1.5                      # 同一目录指纹的短缓存秒数（多标签页/多处轮询合并成一次遍历）


def dir_signature(path: Path, recursive: bool) -> str:
    """目录变更指纹：根 mtime + 各级目录 mtime + 媒体文件数。

    新增/删除文件必然改变所在目录的 mtime，因此无需逐文件 stat 即可检测变化，
    大目录轮询成本也只有目录遍历本身。SIG_TTL 内的重复请求直接复用上次结果。
    """
    key = (str(path), bool(recursive))
    now = time.time()
    with _SIG_LOCK:
        if _SIG["key"] == key and now - _SIG["at"] < SIG_TTL:
            return _SIG["sig"]
    sig = _dir_signature_calc(path, recursive)
    with _SIG_LOCK:
        _SIG.update({"key": key, "at": now, "sig": sig})
    return sig


def _dir_signature_calc(path: Path, recursive: bool) -> str:
    """目录变更指纹（B6-4 加强版）。

    旧口径 = 根 mtime + 各子目录 mtime + 媒体文件**个数**，有一个致命盲区：
    外部程序**原地覆盖同名文件**（ComfyUI 覆盖导出 / 覆盖保存 / 同步工具回写）时
    文件数不变，且实测**目录 mtime 也完全不变**（1791389827.979406 前后一致）
    ⇒ 指纹一字不差 ⇒ 前端判定「目录没变」不刷新 ⇒ 用户看到的永远是旧内容 /
    旧缩略图，且只要没有别的目录变动就**永不自愈**（SIG_TTL 短缓存只是加重观感）。

    新口径在原有三项之外加两个媒体侧量：
      · **字节总数**（覆盖导出通常改变大小）；
      · **最新的文件 mtime**（大小恰好相同时兜住）。
    ⚠️ 代价可控：`os.scandir` 的 DirEntry **自带** stat 信息（Windows 上取自目录枚举
    本身，不额外发起系统调用），所以逐文件 stat 不增加 I/O 次数。
    ⚠️ 同时把 `int(st_mtime)` 换成 `st_mtime_ns` —— 秒级截断会让「同一秒内删除并
    重建同名文件」漏检（B6 P2-1 与本次同源，一并收口）。
    """
    media = IMAGE_EXTS | VIDEO_EXTS
    try:
        parts = [f"r:{path.stat().st_mtime_ns}"]
    except OSError:
        return ""
    n = 0
    nbytes = 0
    newest = 0
    dcount = 0
    stack = [(str(path), 0)]
    while stack:
        d, lvl = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            # B6-P2-6（2026-10-19）：记**相对路径**而不是裸名字。
                            # 同名子目录（`a/2024` 与 `b/2024`）只记名字时，两者
                            # mtime 互换会互相抵消 ⇒ 签名不变 ⇒ 漏检不刷新。
                            try:
                                rel = os.path.relpath(e.path, str(path))
                            except ValueError:      # 跨盘（理论上不会，防御一下）
                                rel = e.path
                            parts.append(f"{rel}:{e.stat().st_mtime_ns}")
                            dcount += 1
                            if recursive and lvl < 10 and dcount < 5000:
                                stack.append((e.path, lvl + 1))
                        elif os.path.splitext(e.name)[1].lower() in media:
                            st = e.stat()
                            n += 1
                            nbytes += st.st_size
                            if st.st_mtime_ns > newest:
                                newest = st.st_mtime_ns
                    except OSError:
                        continue
        except OSError:
            continue
    # B6-P2-6：`os.scandir` 的返回顺序不保证稳定（不同卷/不同时刻可能不同），
    # 排序后再哈希，否则同样内容会算出两个签名，白白触发一次全量刷新。
    parts.sort()
    h = hashlib.md5("|".join(parts).encode("utf-8", "replace")).hexdigest()[:12]
    # 媒体侧三个量都进签名：个数（增删）+ 总字节（覆盖改写）+ 最新 mtime（同大小改写）
    return f"{n}:{nbytes}:{newest}:{h}"


# ---------------------------------------------------------------- 批量元数据 / 统计

def meta_batch(paths: list) -> dict:
    """批量提取轻量元数据 {path: {model, sampler, w, h}}（筛选面板用）。

    优先命中提示词索引（常驻内存，覆盖全量、秒回）；索引里没有的（新增文件、
    未参与索引的格式）再现场解析，现场解析上限 META_BATCH_MAX，避免首次打开
    筛选面板时把大目录整轮读一遍。
    """
    out = {}
    with IDX_LOCK:
        idx = dict(IDX["map"])
    pending = []
    for raw in paths:
        key = str(raw)
        e = idx.get(key)
        if not e:
            pending.append(key)
            continue
        w = h = None
        m = re.match(r"(\d+)\s*x\s*(\d+)", e.get("dim") or "")
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        out[key] = {"model": e.get("model") or "", "sampler": e.get("sampler") or "",
                    "w": w, "h": h,
                    # 工作流分类/类型，与 nav_stats 的统计同一判定口径
                    "wfk": e.get("wfk") or "",
                    "wf": e.get("wfc") or ("prompt" if (e.get("pos") or e.get("model")
                                                         or e.get("sampler")) else "none")}
    for raw in pending[:META_BATCH_MAX]:
        try:
            p = Path(str(raw))
            if not p.is_file() or not inside_root(p):
                continue
            suf = p.suffix.lower()
            if suf == ".png":
                meta = png_meta(p)
            elif suf in (".jpg", ".jpeg", ".webp") and HAS_PIL:
                meta = parse_jpeg_meta(p)
            else:
                continue
            parsed = parse_meta(meta) or {}
            w = h = None
            m = re.match(r"(\d+) x (\d+)", meta.get("IHDR") or "")
            if m:
                w, h = int(m.group(1)), int(m.group(2))
            out[str(raw)] = {"model": parsed.get("model") or "",
                             "sampler": parsed.get("sampler") or "",
                             "w": w, "h": h,
                             "wfk": parsed.get("wfk") or "",
                             "wf": _wf_class(meta, parsed)}
        except (OSError, ValueError):
            continue
    # ε-11（§6.4）：现场解析被 META_BATCH_MAX 截断时必须给信号 —— 此前调用方
    # 无法区分「都没有元数据」与「超限没解析」，筛选维度会静默缺失。
    # 哨兵键放顶层扁平 dict 里（历史上 meta_batch 直接返回 path→meta 映射，
    # 改包一层会破坏两个消费方的合并语义）；前端聚合循环按「非对象跳过」兜住。
    out["__meta_truncated"] = max(0, len(pending) - META_BATCH_MAX)
    return out


# ---------------------------------------------------------------- 统一数量口径

CENSUS_TTL = 45.0                  # 目录普查缓存秒数（过期或换根目录后重扫）
# B7-P1-1（2026-10-17）：索引构建期「旧值复用」的最长时长。旧实现 stale 分支
# **不更新 at**，于是整个索引期（外接盘上万张图常达数分钟）每一轮都命中同一分支，
# 总数被冻结在构建开始前那一瞬；这里给它一个上限，到点放行一次真扫（扫完 at 与
# stale_since 一起复位），索引期数字也能每分钟左右更新一次。
CENSUS_STALE_MAX = 60.0
_CENSUS = {"root": None, "at": 0.0, "data": None, "stale_since": 0.0}
_CENSUS_LOCK = threading.Lock()

# 「文件仍在」计数缓存（批次 C · P2，2026-10-06）。
# 背景：nav_stats() 里三处 isfile 循环此前**零缓存**，而 settings.js 在索引构建期
# **每 2 秒**调一次 /api/stats —— 全收藏时每秒数千次系统调用，外接盘再放大 10~50 倍。
# 同一个函数里的 media_census() 早已有 TTL，唯独这三处漏了。
# 缓存键 = 条目数 + 内容 hash：内容一变（增删收藏/标记）立即重算，否则 TTL 内复用。
# 「每次都要算签名」本身也不亏：hash() 是 C 层，比一次 isfile 便宜两个数量级。
_ALIVE_TTL = 10.0
_ALIVE = {"at": 0.0, "sig": None, "fav": 0, "recent": 0, "mark": 0,
          "mark_state": {}, "mark_ok": frozenset()}
_ALIVE_LOCK = threading.Lock()


def _is_media_alive(path: str) -> bool:
    """「是媒体格式、且文件仍在」—— 左栏计数与三个视图的**同一套判定**。

    B7-P2-3（2026-10-17）：原先 alive 计数只判 isfile，而 fav_view / recent_view /
    marks_view 全都还要求扩展名属于 IMAGE_EXTS|VIDEO_EXTS（`/api/fav` 只校验
    is_file()，库里的 .txt/.json 理论上也能被收藏）⇒ 左栏「收藏 3」点进去只有 2 条。
    计数比列表多一条，用户没法解释，只会认为列表漏了。
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in IMAGE_EXTS and ext not in VIDEO_EXTS:
        return False
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def _alive_counts(favs, recent, marks) -> dict:
    """一遍扫完三类「文件仍在」计数 + 各标记状态计数 + 仍在的标记路径集合。

    带 TTL + 内容签名缓存（见上方说明）。
    B7-P1-4（2026-10-17）：原先 nav_stats 里这里刚对全部 mark 路径做完 isfile，
    紧接着 `mark_stats(marks)` 又对**完全相同的一批路径**再走一遍 —— 上方注释写着
    「三处 isfile 循环收进缓存」，实际是四处，第四处（逐状态计数）没进来。
    现在这一遍同时产出 `mark_state`（给 mark_stats 用）与 `mark_ok`（给 mark_map 用），
    谁都不许再自己数第二遍：既省系统调用，也不会出现「同一响应里两个数对不上」。
    """
    # 2026-11-08：隐藏目录里的收藏 / 最近 / 标记一律不计入左栏数字 —— 用户要的是
    # 「这些文件夹不进软件」，数字亮着、点进去是空的就成了假象。
    hid = hidden_snapshot()
    sig = (len(favs), len(recent), len(marks),
           hash(tuple(favs)),
           hash(tuple((e.get("path") or "") for e in recent)),
           hash(tuple((p, (v or {}).get("state")) for p, v in marks.items())),
           hash(hid))
    now = time.time()
    with _ALIVE_LOCK:
        if _ALIVE["sig"] == sig and now - _ALIVE["at"] < _ALIVE_TTL:
            return {"fav": _ALIVE["fav"], "recent": _ALIVE["recent"],
                    "mark": _ALIVE["mark"], "mark_state": dict(_ALIVE["mark_state"]),
                    "mark_ok": _ALIVE["mark_ok"]}
    fav_alive = sum(1 for p in favs
                    if not _hid_hit(hid, p) and _is_media_alive(p))
    recent_alive = sum(1 for e in recent
                       if not _hid_hit(hid, e.get("path") or "")
                       and _is_media_alive(e.get("path") or ""))
    mark_state, mark_ok = {}, set()
    for p, v in marks.items():
        s = (v or {}).get("state")
        if s and not _hid_hit(hid, p) and _is_media_alive(p):
            mark_ok.add(p)
            mark_state[s] = mark_state.get(s, 0) + 1
    res = {"fav": fav_alive, "recent": recent_alive, "mark": len(mark_ok),
           "mark_state": mark_state, "mark_ok": frozenset(mark_ok)}
    with _ALIVE_LOCK:
        _ALIVE.update({"at": now, "sig": sig, "fav": fav_alive,
                       "recent": recent_alive, "mark": len(mark_ok),
                       "mark_state": dict(mark_state), "mark_ok": res["mark_ok"]})
    return res


# 「标签能不能在图库里看到」缓存（第 16 级，2026-10-17）。
# 与上面 `_ALIVE` 同一套思路：标签表可能上千条，而 /api/tags_list 会被左栏标签树
# 反复拉取、/api/stats 每 2 秒拉一次 —— 每条 isfile 一次就是上千次系统调用，
# 外接盘再放大一个数量级。缓存键 = 根目录 + 表内容 hash（内容一变立即重算）。
_TAGALIVE_TTL = 10.0
_TAGALIVE = {"at": 0.0, "sig": None, "live": frozenset(), "dangling": 0,
             "unknown": False}
_TAGALIVE_LOCK = threading.Lock()


def _tag_scope_prefixes() -> tuple:
    """标签「在图库里看得到」的路径前缀：当前根目录 + 单图暂存目录。

    与 `inside_root` 的放行范围保持一致 —— 标签写入端点（/api/tags 的 add /
    remove / set）只接受 `inside_root` 通过的目标，所以表里的键正常情况下落在
    这两处之内。但 `tags.json` 是**全局表**：换根之后旧根的键仍留在里面，而当前
    图库根本看不到它们，计数必须排除（否则左栏数字永远带着上一个目录的标签）。
    """
    out = []
    try:
        root = STATE.get("root")
        if root:
            out.append(str(Path(root).resolve()).lower().rstrip("\\/"))
    except (OSError, ValueError):
        pass
    try:
        out.append(str(SOLO_DIR.resolve()).lower().rstrip("\\/"))
    except (OSError, ValueError):
        pass
    return tuple(p for p in out if p)


def _tag_in_scope(path: str, prefixes: tuple) -> bool:
    """纯字符串前缀判定（不 resolve、不碰磁盘）。

    必须带上分隔符再比：只比 `C:\\a` 前缀会把 `C:\\ab\\x.png` 也算进来。
    """
    lp = path.lower().replace("/", "\\")
    for pref in prefixes:
        if lp == pref or lp.startswith(pref + "\\"):
            return True
    return False


def _root_readable(root: str) -> bool:
    """根目录现在读不读得到 —— 只问一句，不遍历（同 `_prune_dangling` 的预检）。"""
    if not root:
        return False
    try:
        os.scandir(root).close()
        return True
    except OSError:
        return False


def _tag_alive(ts: dict) -> dict:
    """标签表里「当前图库里看得到」的路径集合 + 看不到的条目数 + 可信度。

    返回 {"live": frozenset, "dangling": int, "unknown": bool}；带 TTL + 内容签名缓存。
    """
    root = STATE.get("root") or ""
    # 签名只含**路径集合**，不含标签内容：存活判定只跟「路径 / 文件 / 根目录」有关，
    # 给已有一张图加个标签不该触发一次全表 isfile（大库在外接盘上要几百毫秒）。
    # 代价：外部删图但标签内容同时变化时，最长 10s 的 TTL 到期才纠正 —— 与 _ALIVE 同一量级。
    sig = (root, len(ts), hash(frozenset(ts.keys())),
           hash(hidden_snapshot(root)))   # 2026-11-08：名单变 ⇒ 口径变 ⇒ 缓存作废
    now = time.time()
    with _TAGALIVE_LOCK:
        if _TAGALIVE["sig"] == sig and now - _TAGALIVE["at"] < _TAGALIVE_TTL:
            return {"live": _TAGALIVE["live"], "dangling": _TAGALIVE["dangling"],
                    "unknown": _TAGALIVE["unknown"]}
    if not root:
        # 还没选根：图库里什么都看不到，标签也一并为空（与 media_census 的空根口径一致）。
        # 此时不能说「N 条失效」—— 它们并没有失效，只是没有库可看。
        res = {"live": frozenset(), "dangling": 0, "unknown": False}
    elif not _root_readable(root):
        # 根目录读不到 ⇒ 无法判定谁还在。不做过滤，退回全量口径 + unknown 标记：
        # 外接盘掉线时把整库标签显示成 0，比显示「可能偏大」坏得多。
        res = {"live": frozenset(ts), "dangling": 0, "unknown": True}
    else:
        prefixes = _tag_scope_prefixes()
        hid = hidden_snapshot(root)
        live, dangling = set(), 0
        for p in ts:
            if not _tag_in_scope(p, prefixes):
                continue          # 旧根 / 库外条目：当前图库看不到，也不算「本库失效」
            if _hid_hit(hid, p):
                continue          # 2026-11-08：隐藏目录里的图「在图库里看不到」，不计标签数
            if _is_media_alive(p):
                live.add(p)
                continue
            # 只有「媒体格式、但文件已不在」才算失效。
            # 非媒体条目（.txt / .lnk …）文件本身可能好好的，把它报成「失效」就是假警报，
            # 而且 prune_tags 只清「确定不存在」的键 ⇒ 这条提示永远消不掉。
            ext = os.path.splitext(p)[1].lower()
            if ext in IMAGE_EXTS or ext in VIDEO_EXTS:
                dangling += 1
        res = {"live": frozenset(live), "dangling": dangling, "unknown": False}
    with _TAGALIVE_LOCK:
        _TAGALIVE.update({"at": now, "sig": sig, "live": res["live"],
                          "dangling": res["dangling"], "unknown": res["unknown"]})
    return res


def _index_busy() -> bool:
    """提示词索引线程是否正在跑（全树遍历期间不宜再叠加一套遍历）。"""
    with IDX_LOCK:
        return bool(IDX.get("building"))


def census_invalidate():
    """删除/还原/重扫等改动文件的操作后调用，让下一次 /api/stats 重新扫描目录。

    B7-P2-1（2026-10-17）：此前只清 `_CENSUS`，不动 `_ALIVE`（收藏/最近/已标记的
    「文件仍在」计数缓存，TTL 10s）。文件被**应用外**改动（资源管理器删/移、回收站
    清空、`/api/trash_restore` 还原）时三张表内容不变 ⇒ `_ALIVE` 的签名不变，
    命中缓存 ⇒ 「目录总数已经重算了，收藏数还是旧的」这种自相矛盾最长持续 10 秒。
    失效必须一起做，两个缓存描述的是同一批文件。
    """
    with _CENSUS_LOCK:
        _CENSUS["data"] = None
        _CENSUS["at"] = 0.0
        _CENSUS["stale_since"] = 0.0
    with _ALIVE_LOCK:
        _ALIVE["sig"] = None
        _ALIVE["at"] = 0.0
    # 第 16 级：标签的「文件仍在」缓存描述的是**同一批文件**，失效必须一起做 ——
    # 外部删图后 tags.json 一字未改、签名不变，标签计数会跟着旧 10 秒。
    with _TAGALIVE_LOCK:
        _TAGALIVE["sig"] = None
        _TAGALIVE["at"] = 0.0


def media_census(root: str = "", max_age: float = CENSUS_TTL) -> dict:
    """扫描根目录统计图片/视频/格式分布，带 TTL 缓存。

    遍历规则与 _index_builder 保持一致（深度上限 10、realpath 去重防成环、
    跳过不可读目录）；差别是这里同时统计视频——视频不进提示词索引，
    所以「全部」口径必须单独走一遍目录，不能拿索引条数冒充。
    """
    root = root or STATE.get("root") or ""
    if not root:
        return {"root": "", "images": 0, "videos": 0, "total": 0, "formats": {}}
    now = time.time()
    with _CENSUS_LOCK:
        hot = (_CENSUS["root"] == root and _CENSUS["data"] is not None)
        if hot and now - _CENSUS["at"] < max_age:
            return dict(_CENSUS["data"])
        data = dict(_CENSUS["data"]) if hot else None
    if data is not None and _index_busy():
        # 索引线程正在全树扫描：这次先用旧数，避免两套遍历同时压磁盘（索引完下次请求自然更新）
        # ε-11（§6.4）：旧值必须带 stale 标记 —— 此前返回值与新鲜值无法区分，
        # 公共函数被别处直接消费时会被当成全量真值。
        # B7-P1-1（2026-10-17）：旧值不能无限复用 —— 索引期可能长达数分钟，
        # 到 CENSUS_STALE_MAX 就放行一次真扫（每分钟左右更新一次，代价是一次目录遍历）。
        with _CENSUS_LOCK:
            if not _CENSUS["stale_since"]:
                _CENSUS["stale_since"] = now
            since = _CENSUS["stale_since"]
        if now - since < CENSUS_STALE_MAX:
            return dict(data, stale=True)
    images = videos = 0
    formats: dict = {}
    unreadable = 0                  # 遍历中扫不动的目录数（含 root 自身）
    root_unreadable = False
    rootp = Path(root)
    # 2026-11-08：隐藏目录不计入「全部 / 图片 / 视频」—— 它与索引同口径，
    # 否则左栏的数字比网格里能看到的永远多出一截。
    hid = hidden_snapshot(root)
    stack = [(str(rootp), 0)]
    visited = {os.path.realpath(str(rootp))}
    while stack:
        cur, depth = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            if depth < 10 and not _hid_hit(hid, e.path):
                                rp = os.path.realpath(e.path)
                                if rp not in visited:
                                    visited.add(rp)
                                    stack.append((e.path, depth + 1))
                        else:
                            ext = os.path.splitext(e.name)[1].lower()
                            if ext in IMAGE_EXTS:
                                images += 1
                            elif ext in VIDEO_EXTS:
                                videos += 1
                            else:
                                continue
                            formats[ext] = formats.get(ext, 0) + 1
                    except OSError:
                        continue
        except OSError as ex:
            unreadable += 1
            if cur == str(rootp):
                root_unreadable = True
                log(f"[census] 根目录不可读（未挂载 / 无权限 / 已离线）：{rootp}："
                    f"{type(ex).__name__}: {ex}")
            continue
    # P0-16：根目录根本扫不动时，绝不能返回 total=0 冒充「一张图都没有」——
    # 那跟「用户真的清空了」在界面上完全不可区分，用户只会以为图丢了。
    # 带 error 字段如实上报，并**不写缓存**：下一次请求重新试，盘/权限一恢复，
    # 数字立刻回来（旧实现会把 0 缓存 45 秒，误导窗更长）。
    if root_unreadable:
        return {"root": root, "images": 0, "videos": 0, "total": 0, "formats": {},
                "stale": False, "unreadable": unreadable,
                "error": "根目录当前不可读（未挂载 / 无权限 / 已离线），数量不可用"}
    data = {"root": root, "images": images, "videos": videos,
            "total": images + videos, "formats": formats, "stale": False,
            "unreadable": unreadable}
    with _CENSUS_LOCK:
        _CENSUS.update({"root": root, "at": now, "data": data, "stale_since": 0.0})
    return dict(data)


def nav_stats() -> dict:
    """左栏导航与筛选共用的统一数量口径（各处数字都从这里取，避免各算各的）。

    - total/images/videos：根目录下（含子目录）的全局口径，与「含子文件夹」开关无关
    - favorites：只统计文件仍在的收藏（收藏表里可能残留已删除/已移动的路径）
    - workflow：提示词索引中能解析出正向提示词或模型/采样器的图片数；索引未建完时为 0，
      前端据此显示占位（…）而不是假的 0
    - tags：与 /api/tags_list 同一份 `tag_stats_detail()`，保证标签计数一致；
      只统计当前图库里看得到的条目（媒体格式 + 文件仍在 + 在当前根内），
      `tags_dangling` 是看不到的条数，`tags_unknown` 表示根目录不可读、口径存疑
    """
    cens = media_census()
    with IDX_LOCK:
        snap = list(IDX["map"].values())
        building, done, total = IDX["building"], IDX["done"], IDX["total"]
        idx_stale, idx_stale_reason = IDX.get("stale", False), IDX.get("stale_reason", "")
    wf_full = wf_prompt = wf_none = 0
    wfk = {"t2i": 0, "i2i": 0, "cn": 0, "other": 0}
    for e in snap:
        c = e.get("wfc")
        if c not in ("full", "prompt", "none"):      # 旧缓存条目：按字段兜底判定
            c = "prompt" if (e.get("pos") or e.get("model") or e.get("sampler")) else "none"
        if c == "full":
            wf_full += 1
        elif c == "prompt":
            wf_prompt += 1
        else:
            wf_none += 1
        k = e.get("wfk") or ""
        wfk[k if k in ("t2i", "i2i", "cn") else "other"] += 1
    marks = load_marks()
    # 批次 C · P2：三处 isfile 循环收进带 TTL + 内容签名的缓存（见 _alive_counts 上方说明）
    alive = _alive_counts(load_favs(), load_recent(), marks)
    # 第 16 级：标签计数只算一次（一次 load_tags + 一遍缓存判定），三个字段同源
    _tags_d = tag_stats_detail()
    return {"total": cens["total"], "images": cens["images"], "videos": cens["videos"],
            "favorites": alive["fav"], "formats": cens["formats"],
            "workflow": wf_full + wf_prompt, "wf_full": wf_full,
            "wf_prompt": wf_prompt, "wf_none": wf_none, "wfk": wfk,
            "recent": alive["recent"], "marked": alive["mark"],
            # B7-P1-4：各状态计数复用上面那**同一遍** isfile 的结果，不再自己扫第二遍
            "marks": mark_stats(marks, alive["mark_state"]),
            # B7-P2-2：mark_map 与 marked / marks / marks_view **同源**（媒体且文件仍在）。
            # 原先这里是全量口径 ⇒ 同一响应里「左栏 3、网格亮了 5 个角标、点进去 3 条」。
            "mark_map": {p: marks[p].get("state") for p in alive["mark_ok"]},
            # B7-P1-1 / P0-16：把 census 的「旧值」「有目录没读出来」「根目录不可读」
            # 原样透给前端。此前 nav_stats 只取 total/images/videos，唯一消费点把这些
            # 标记全丢了 —— 界面上的「全部 9749」到底是此刻的真值还是几分钟前的旧值，
            # 用户完全无从判断；根目录掉线时更是只看到一个 0。
            "stale": bool(cens.get("stale")),
            "unreadable": int(cens.get("unreadable") or 0),
            "census_error": cens.get("error") or "",
            "indexed": len(snap),
            "index_building": building, "index_done": done, "index_total": total,
            # P0-1：索引本轮校验失败（根/子目录读不到）时如实上报，前端据此提示
            # 「显示的是上一次的结果」而不是假装一切正常 —— 这正是原先最误导的地方。
            "index_stale": bool(idx_stale), "index_stale_reason": idx_stale_reason,
            # 第 16 级：与 /api/tags_list 同一份计数；顺带把「有多少条在图库里看不到」
            # 和「这次口径可不可信」透出去 —— 数字比昨天小了，界面上得有一句话解释。
            "tags": _tags_d["tags"],
            "tags_dangling": _tags_d["dangling"],
            "tags_unknown": _tags_d["unknown"]}


# 2026-10-27（代码梳理 O7）：stats_view 的结果按「索引身份 + 30 s TTL」缓存。
# 它每次都要全表遍历（10 万条约 132 ms 实测），而 /api/stats 会被前端在打开大图、
# 切视图、底栏刷新时反复拉。缓存身份 = 代际 + 发布时刻 + 条目数 + 是否在构建，
# 任一变化（重建完成 / 换根 / 增量换表）都会自然失效，不需要额外的失效钩子。
_STATS_CACHE = {"key": None, "at": 0.0, "val": None}
_STATS_CACHE_LOCK = threading.Lock()
_STATS_CACHE_TTL = 30.0


def stats_view() -> dict:
    """从提示词索引聚合出图统计。索引构建中时返回进度由前端提示。

    返回值**每次都是新 dict**（哪怕命中缓存）：/api/stats 会往返回对象上补
    `nav` / `mig` 两个字段，直接把缓存对象交出去会被上一次请求的附加值污染。
    """
    with IDX_LOCK:
        building = IDX["building"]
        done, total = IDX["done"], IDX["total"]
        snap = list(IDX["map"].items())
        key = (IDX_GEN[0], IDX.get("built_at") or 0.0, len(snap), building)
    now = time.time()
    with _STATS_CACHE_LOCK:
        c = _STATS_CACHE
        if c["val"] is not None and c["key"] == key and now - c["at"] < _STATS_CACHE_TTL:
            # 进度三元组换成现值：重算它们零成本，重算整表才是贵的
            return dict(c["val"], building=building, done=done, total=total)
    if not snap:
        return {"building": building, "done": done, "total": total, "count": 0}
    days, models, samplers, dims = {}, {}, {}, {}
    nbytes = 0
    tmin = tmax = None
    for _path, e in snap:
        nbytes += e.get("size") or 0
        for src, dst in ((e.get("model"), models), (e.get("sampler"), samplers),
                         (e.get("dim"), dims)):
            if isinstance(src, str) and src:   # 非字符串（历史缓存里的连线引用）跳过，不能当 dict key
                dst[src] = dst.get(src, 0) + 1
        mt = e.get("mtime") or 0
        if mt <= 0:
            # ε-11（§6.4）：mtime 缺失/为 0 的条目不进时间桶（bytes / Top10 照常统计）——
            # 此前把统计起点拉到 1970-01-01，span 与 by_day 横轴全被撑爆。
            continue
        t = time.localtime(mt)
        day = f"{t.tm_year:04}-{t.tm_mon:02}-{t.tm_mday:02}"
        days[day] = days.get(day, 0) + 1
        if tmin is None or mt < tmin:
            tmin = mt
        if tmax is None or mt > tmax:
            tmax = mt
    span = ([time.strftime("%Y-%m-%d", time.localtime(tmin)),
             time.strftime("%Y-%m-%d", time.localtime(tmax))] if tmin is not None else None)
    val = {"building": building, "done": done, "total": total,
           "count": len(snap), "bytes": nbytes, "span": span,
           "by_day": sorted(days.items())[-30:],
           "by_model": sorted(models.items(), key=lambda x: -x[1])[:10],
           "by_sampler": sorted(samplers.items(), key=lambda x: -x[1])[:10],
           "by_dim": sorted(dims.items(), key=lambda x: -x[1])[:10]}
    with _STATS_CACHE_LOCK:
        _STATS_CACHE.update({"key": key, "at": now, "val": val})
    return dict(val)


# ---------------------------------------------------------------- 收藏夹视图

# ---------------------------------------------------------------- 最近使用 / 已标记

RECENT_FILE = DATA_DIR / "recent.json"
MARKS_FILE = DATA_DIR / "marks.json"
RECENT_MAX = 200                    # 只保留最近 200 条（按路径去重）
RECENT_LOCK = threading.Lock()
MARKS_LOCK = threading.Lock()
RECENT_ACTIONS = {"preview": "预览", "comfy": "打开 ComfyUI", "download": "下载",
                  "copy": "复制提示词", "folder": "打开位置"}
# 标记与收藏分开：收藏=长期保留，标记=临时工作状态
MARK_STATES = (("review", "待处理"), ("publish", "待发布"),
               ("compare", "待比较"), ("delete", "待删除"))
MARK_LABELS = dict(MARK_STATES)


def _save_json(path: Path, obj) -> bool:
    """原子写 JSON（先写临时文件再替换），失败只记日志不抛。

    B7-P2-7（2026-10-17）：`write_text` + `replace` 中间没有 flush/fsync —— 断电或
    强杀时 rename 已经生效、数据页却可能还在缓存里，留下 0 字节或半截的
    recent.json / marks.json（NTFS 上 rename 是原子的，**内容**不是）。另外写入失败时
    临时文件不会被清理，`.PID.TID.tmp` 永久堆在 DATA_DIR 里，被 `_dir_usage` 算进
    「目录占用」的文件数 —— 用户清完缓存发现占用数字不降。
    """
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{_tmp_tag()}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=1))
            f.flush()
            os.fsync(f.fileno())             # 先落盘再换名：rename 之后就不能再补了
        tmp.replace(path)
        _invalidate_json(path)               # 有读方走 _cached_json ⇒ 写后一律失效
        return True
    except Exception as ex:
        log(f"[warn] 写入失败 {path.name}: {type(ex).__name__}: {ex}")
        if tmp is not None:
            try:
                tmp.unlink()
            except OSError:
                pass
        return False


def load_recent() -> list:
    """最近使用 [{path, action, time}]，最新在前。"""
    out = []
    try:
        data = json.loads(RECENT_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return out
    if isinstance(data, list):
        for e in data:
            if isinstance(e, dict) and e.get("path"):
                out.append({"path": str(e["path"]),
                            "action": str(e.get("action") or ""),
                            "time": int(e.get("time") or 0)})
    return out


def recent_add(path: str, action: str) -> dict:
    """记录一次使用：按路径去重（保留最新一条），最多 RECENT_MAX 条。"""
    key = str(Path(path).resolve())
    act = action if action in RECENT_ACTIONS else "preview"
    with _store_lock(RECENT_LOCK, "最近使用"):
        items = [e for e in load_recent() if e["path"] != key]
        items.insert(0, {"path": key, "action": act, "time": int(time.time())})
        del items[RECENT_MAX:]
        if not _save_json(RECENT_FILE, items):     # P1-4：不写成功就别报成功
            return {"ok": False, "count": len(items),
                    "error": "最近使用记录写入失败（磁盘不可写或被占用）"}
    return {"ok": True, "count": len(items)}


def _recent_key(p) -> str:
    """最近使用里的路径比较口径：解析后 normcase（Windows 大小写 / 分隔符不敏感）。

    记录存的是 `Path(...).resolve()` 的结果，但历史文件可能来自旧版本或另一台机器
    （同一文件经 junction / 盘符大小写不同），直接字符串比会「点了移除却还在」。
    """
    try:
        return os.path.normcase(str(Path(str(p)).resolve()))
    except (OSError, ValueError, TypeError):
        return ""


def recent_remove(path: str) -> dict:
    """从「最近使用」里摘掉一条：只动 recent.json，不碰任何文件。

    不校验文件是否仍存在 —— 死链（原图已删 / 换过根）正是用户最想清掉的那类记录；
    也不校验 inside_root：换根以后旧根下的记录同样要能删，而这里只删一条**记录**，
    不会触碰磁盘上的文件，误传路径最多是「没找到、什么都没做」。
    """
    key = _recent_key(path)
    if not key:
        return {"ok": False, "removed": False, "count": len(load_recent()),
                "error": "路径非法"}
    with _store_lock(RECENT_LOCK, "最近使用"):
        items = load_recent()
        kept = [e for e in items if _recent_key(e["path"]) != key]
        if len(kept) == len(items):               # 幂等：本来就没有 ⇒ 成功但未变更
            return {"ok": True, "removed": False, "count": len(items)}
        if not _save_json(RECENT_FILE, kept):     # 没落盘就不能报「已移除」
            return {"ok": False, "removed": False, "count": len(items),
                    "error": "最近使用记录写入失败（磁盘不可写或被占用）"}
    return {"ok": True, "removed": True, "count": len(kept)}


def recent_clear() -> dict:
    with _store_lock(RECENT_LOCK, "最近使用"):
        if not _save_json(RECENT_FILE, []):        # P1-4：清空没落盘 ⇒ 界面不能报「已清空」
            return {"ok": False, "count": 0,
                    "error": "最近使用记录清空失败（磁盘不可写或被占用）"}
    return {"ok": True, "count": 0}


def load_marks() -> dict:
    """标记表 {路径: {state, updated}}。"""
    try:
        data = json.loads(MARKS_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict) and v.get("state") in MARK_LABELS:
                out[str(k)] = {"state": str(v["state"]),
                               "updated": int(v.get("updated") or 0)}
    return out


def mark_stats(marks: dict = None, state_counts: dict = None) -> list:
    """各状态计数 [{state, label, count}]，顺序固定便于左栏展示。

    ε-11（§6.4）：只统计**文件仍在**的标记 —— 与 nav_stats 的 marked / favorites
    同一口径。此前全量统计（含文件已删/已移走的），左栏数字与点进去的列表对不上。

    B7-P1-4（2026-10-17）：`state_counts` 由 `_alive_counts` 同一次扫描产出。nav_stats
    每 2 秒调一次这里，此前每次都把上一行刚扫过的那批路径再 isfile 一遍 —— 外接盘上
    是成百上千次多余的系统调用，而且两遍算出的值还必须一致（不一致就是新的分叉源）。
    """
    m = load_marks() if marks is None else marks
    if state_counts is None:
        cnt = {}
        for p, v in m.items():
            s = v.get("state")
            if s and os.path.isfile(p):
                cnt[s] = cnt.get(s, 0) + 1
    else:
        cnt = dict(state_counts)             # 复用上一次扫描的结果（含媒体格式判定）
    return [{"state": st, "label": lab, "count": cnt.get(st, 0)}
            for st, lab in MARK_STATES]


def mark_set(path: str, state: str) -> dict:
    """设置或清除标记；state 为空/"none" 表示取消。"""
    key = str(Path(path).resolve())
    st = (state or "").strip()
    with _store_lock(MARKS_LOCK, "标记"):
        m = load_marks()
        if not st or st == "none":
            m.pop(key, None)
            cur = ""
        elif st in MARK_LABELS:
            m[key] = {"state": st, "updated": int(time.time())}
            cur = st
        else:
            return {"ok": False, "error": "未知的标记状态"}
        if not _save_json(MARKS_FILE, m):          # P1-4：没落盘就报失败，别让角标闪完就没了
            return {"ok": False, "state": cur, "marks": mark_stats(m),
                    "error": "标记写入失败（磁盘不可写或被占用）"}
    return {"ok": True, "state": cur, "marks": mark_stats(m)}


def _pack_media(path: str, extra: dict = None, nctx=None):
    """把一个绝对路径打包成网格卡片用的 item；不存在/非媒体返回 None。

    nctx 可由调用方传入一份 NSFW 快照（列表循环里复用同一份，省掉逐条取锁）；
    不传就自己取一份，单条调用场景下这点成本可以忽略。
    """
    p = Path(path)
    try:
        # 2026-10-27（代码梳理 O8）：一次 stat 同时回答「存在吗」「是普通文件吗」。
        # 原先 `p.is_file()` 之后紧跟 `p.stat()` —— 每条 item 两次系统调用，
        # 2000 条的收藏/最近/标记视图就是 4000 次多余 stat（外接盘上是实打实的延迟）。
        st = p.stat()
    except OSError:
        return None
    if not stat_mod.S_ISREG(st.st_mode):   # 目录 / 设备 / 断链占位：与 is_file() 同义
        return None
    ext = p.suffix.lower()
    t = "image" if ext in IMAGE_EXTS else ("video" if ext in VIDEO_EXTS else None)
    if not t:
        return None
    _nl, _ns = nsfw_fields(str(p), int(st.st_mtime), st.st_size, nctx)
    it = {"name": p.name, "path": str(p), "type": t, "size": st.st_size,
          "mtime": int(st.st_mtime),
          "playable": t == "image" or ext in PLAYABLE_VIDEO,
          "png": ext == ".png", "fav": False, "favcat": None,
          # 收藏视图 / 最近使用 / 已标记全走这里，补一处就全覆盖 ——
          # 漏掉的话会出现「网格里是模糊的、收藏夹里却是原图」这种自相矛盾的界面。
          # （回收站不走 _pack_media，是**有意**的：那是管理视图，用户刚删的图要能看清再决定还原。）
          "n": _nl, "ns": _ns}
    if extra:
        it.update(extra)
    return it


def recent_view(limit: int = 0) -> list:
    """最近使用视图（跳过已不存在的文件），附带收藏/标签/动作信息。"""
    favs, tags = load_favs(), load_tags()
    ctx = nsfw_ctx()        # 一次请求一份快照，别让 _pack_media 每条都去取锁（O8）
    hid = hidden_snapshot()  # 2026-11-08：隐藏目录里的「最近使用」不进视图
    out = []
    for e in load_recent():
        if _hid_hit(hid, e["path"]):
            continue
        it = _pack_media(e["path"], nctx=ctx)
        if not it:
            continue
        it["fav"] = e["path"] in favs
        it["favcat"] = (favs.get(e["path"]) or {}).get("cat")
        it["tags"] = sorted(tags.get(e["path"], []))
        it["ract"] = e["action"]
        it["rtime"] = e["time"]
        out.append(it)
        if limit and len(out) >= limit:
            break
    return out


def marks_view(state: str = "", marks: dict = None) -> list:
    """已标记视图（按标记时间倒序），可按状态过滤。

    `marks` 可由调用方传入（路由里已经读过一次，别为同一个响应读两遍）。
    """
    favs, tags = load_favs(), load_tags()
    if marks is None:
        marks = load_marks()
    ctx = nsfw_ctx()        # 同上：一份快照管整轮循环（O8）
    hid = hidden_snapshot()  # 2026-11-08：隐藏目录里的标记不进视图
    out = []
    for path, v in marks.items():
        if _hid_hit(hid, path):
            continue
        if state and state != "*" and v.get("state") != state:
            continue
        it = _pack_media(path, nctx=ctx)
        if not it:
            continue
        it["fav"] = path in favs
        it["favcat"] = (favs.get(path) or {}).get("cat")
        it["tags"] = sorted(tags.get(path, []))
        it["mark"] = v.get("state")
        it["marklabel"] = MARK_LABELS.get(v.get("state"), "")
        it["marktime"] = v.get("updated") or 0
        out.append(it)
    out.sort(key=lambda x: -x["marktime"])
    return out


def fav_view(cat: str = "") -> list:
    """跨目录列出所有仍存在的收藏 [{item}]，按分类+时间排序。

    注意：这里的 item 是**手搓**的，不走 _pack_media（收藏表里已经存了分类，
    _pack_media 会把 fav/favcat 覆盖成初始值）。所以新加的字段这里要单独补一份 ——
    V2.11.0 就漏过 n 字段，表现是「网格里糊了、收藏夹里是原图」（验证脚本抓到的）。
    """
    favs = load_favs()
    tags = load_tags()                          # P1：下面的 item 要带 tags（见下）
    out = []
    _nctx = nsfw_ctx()                          # 循环外取一份，别每条都取锁
    _hid = hidden_snapshot()                    # 2026-11-08：隐藏目录里的收藏不进视图
    for path, v in favs.items():
        if _hid_hit(_hid, path):
            continue
        c = v.get("cat") or "默认"
        if cat and cat != "*" and c != cat:
            continue
        try:
            p = Path(path)
            if not p.is_file():
                continue
            st = p.stat()
        except OSError:
            continue
        ext = p.suffix.lower()
        t = "image" if ext in IMAGE_EXTS else ("video" if ext in VIDEO_EXTS else None)
        if not t:
            continue
        _nl, _ns = nsfw_fields(path, int(st.st_mtime), st.st_size, _nctx)
        out.append({"name": p.name, "path": path, "type": t,
                    "size": st.st_size, "mtime": int(st.st_mtime),
                    "playable": t == "image" or ext in PLAYABLE_VIDEO,
                    "png": ext == ".png", "fav": True, "favcat": c,
                    # P1（2026-10-09）：/api/list、/api/tags_filter、/api/search 都带
                    # tags，唯独手搓的这里漏了 —— `tag:xxx` 在收藏夹视图里恒筛空
                    # （queryFilter 的 tag 分支 `have=(it.tags||[])` 拿到 []），
                    # 大图标签面板也显示 0 个，用户以为标签丢了。与 recent_view
                    # 同口径补上。（同 V2.11.0 漏 n 字段一类，见 docstring。）
                    "tags": sorted(tags.get(path, [])),
                    "n": _nl, "ns": _ns})
    out.sort(key=lambda x: (x["favcat"], -x["mtime"]))
    return out


def _exif_transpose_bytes(ext: str, data: bytes):
    """ε-11（§6.4）：EXIF Orientation≠1 时把像素转正（返回新 bytes）；无则返回 None。

    剥元数据默认「像素不重编码」，但 Orientation 标记随 APP1/eXIf/EXIF 块一起被剥掉，
    导出的 JPEG/WebP 在其它查看器里会旋转 90°/180°。仅在确有旋转标记时才走 PIL
    重编码把方向物化进像素（ComfyUI 生成图无 EXIF，零开销）；任何异常返回 None
    （fail-safe，保持原行为）。
    """
    if not HAS_PIL or ext not in (".jpg", ".jpeg", ".webp", ".png"):
        return None
    try:
        from PIL import ImageOps
        with Image.open(io.BytesIO(data)) as im:
            exif = im.getexif()
            if not exif or int(exif.get(274, 1) or 1) == 1:
                return None
            fixed = ImageOps.exif_transpose(im)
            out = io.BytesIO()
            if ext == ".png":
                fixed.save(out, "PNG")
            elif ext == ".webp":
                fixed.save(out, "WEBP", quality=95)
            else:
                if fixed.mode not in ("RGB", "L"):
                    fixed = fixed.convert("RGB")
                fixed.save(out, "JPEG", quality=95)
            return out.getvalue()
    except Exception:
        return None


def _make_stripper(ext: str, fn):
    """ε-11：包一层「先转正再剥」—— 三个调用点（批量导出 / 下载清理 / 还原清理）统一生效。

    B5（2026-10-17）：再**透传 warn 通道** —— 调用方要拿得到「这次到底剥干净没有」，
    不能只看「函数返回了一个 bytes」就当成功。
    """
    def _f(data: bytes, warn=None) -> bytes:
        fixed = _exif_transpose_bytes(ext, data)
        if fixed is not None:
            data = fixed
        return fn(data, warn)
    return _f


_STRIP_BY_EXT = {ext: _make_stripper(ext, fn) for ext, fn in
                 {".png": strip_png_meta, ".webp": strip_webp_meta,
                  ".jpg": strip_jpg_meta, ".jpeg": strip_jpg_meta,
                  ".mp4": strip_mp4_meta, ".gif": strip_gif_meta}.items()}


def batch_export(paths: list, dest: str) -> dict:
    """把多个文件复制到目标目录：图片剥离生成元数据，其余原样复制。

    目标目录不存在则创建（含多级）；目标已存在同名文件时加 _1/_2 后缀。
    ε-14（§6.8）：dest **有意不校验 inside_root** —— 导出语义就是写到用户指定的
    任意目录（U 盘、下载目录等，审计 §5.1 已从 P1 降级为 P2 留档）。来源侧
    paths 逐条过 inside_root，风险面只剩「用户自己选的目标」。

    B7（2026-10-17）四件事：
      · P1-2：`.mp4` 不再 `read_bytes()` 整份进内存（峰值曾是 3× 文件大小，
        MemoryError 还不是 OSError，会直接穿出 handler ⇒ 500 + 回执全丢）；
        改成「流式复制 → 目标文件上按结构计划就地清零」，内存与文件大小无关。
      · P2-6①：保留原文件 mtime（`copystat`），导出件的时间不再变成导出那一刻。
      · P2-6②：同一路径在列表里出现两次只导出一份（此前会生成 `x_1.png`）。
      · P2-6③：目标目录落在应用数据/回收站目录里直接拒绝 —— 那些导出件会被
        「目录占用」统计算进去，删库/清缓存时还会被一起清掉。
    """
    destp = Path(dest.strip().strip('"'))
    if not destp.is_absolute():
        return {"ok": False, "error": "请输入绝对路径（如 D:\\export）"}
    try:
        dp = _dir_cmp(str(destp))
    except OSError:
        dp = ""
    for _label, _guard in (("回收站目录", TRASH_DIR), ("应用数据目录", DATA_DIR)):
        try:
            gp = _dir_cmp(str(_guard))
        except OSError:
            continue
        if dp and (dp == gp or dp.startswith(gp)):
            return {"ok": False,
                    "error": f"不能导出到{_label}（{_guard}）：那里的文件会被当成应用数据，"
                             f"清理缓存或删库时一起删掉。请换一个目录"}
    try:
        destp.mkdir(parents=True, exist_ok=True)
    except OSError as ex:
        return {"ok": False, "error": f"目标目录无法创建: {ex}"}
    done, failed, unclean, dup = 0, [], [], 0
    seen = set()
    for raw in paths:
        try:
            p = Path(str(raw))
            if not p.is_file() or not inside_root(p):
                failed.append({"name": os.path.basename(str(raw)),
                               "error": "文件不存在或在根目录之外"})
                continue
            key = os.path.normcase(os.path.normpath(str(p)))
            if key in seen:                       # P2-6：同一张图别导出两份
                dup += 1
                continue
            seen.add(key)
            ext = p.suffix.lower()
            if ext == ".mp4" and ext in _STRIP_BY_EXT:
                _t, _ok, _why = _copy_then_strip_mp4(p, destp, p.name)
                if not _ok:
                    unclean.append({"name": p.name, "error": _why or "未能确认"})
            elif ext in _STRIP_BY_EXT:
                data, _ok, _why = strip_meta_checked(ext, p.read_bytes())
                if not _ok:
                    # B5（2026-10-17）：导出的文件照样写出，但「元数据没抹干净」
                    # 必须跟着回执一起回去 —— 原先这里连成功与否都不上报。
                    unclean.append({"name": p.name, "error": _why or "未能确认"})
                # P2-5（2026-10-17）：独占创建，并发导出不会互相覆盖
                _copystat(p, _write_exclusive(destp, p.name, data))
            else:
                target, fh = _open_exclusive(destp, p.name)
                try:
                    with fh, p.open("rb") as sf:
                        shutil.copyfileobj(sf, fh, 1024 * 1024)
                    _copystat(p, target)          # P2-6：保留原始修改时间
                except BaseException:
                    try:
                        target.unlink()        # 别把半截文件留在用户的导出目录里
                    except OSError:
                        pass
                    raise
            done += 1
        except OSError as ex:
            failed.append({"name": os.path.basename(str(raw)), "error": str(ex)})
        except Exception as ex:
            # B7-P1-2：MemoryError 之类**不是 OSError** —— 一条坏文件不该炸掉整次导出，
            # 更不该让已经成功的那些文件连回执都拿不到。
            failed.append({"name": os.path.basename(str(raw)),
                           "error": "%s: %s" % (type(ex).__name__, ex)})
    if done:
        with SETTINGS_LOCK:
            s = load_settings()
            s["last_export_dir"] = str(destp.resolve())   # 记住上次目录，下次默认填
            save_settings(s)
    return {"ok": not failed, "done": done, "failed": failed,
            "unclean": unclean, "dup": dup, "dest": str(destp.resolve())}


# ---------------------------------------------------------------- handler

# 4xx 访问日志的分钟窗口（5xx 不限流）。见 Handler.log_message 的说明。
HTTP_4XX_PER_MIN = 60
HTTP_4XX = {"min": 0, "n": 0, "drop": 0}


class _HeadSink:
    """HEAD 用的 wfile 包装：**头照发，body 丢弃**。

    ⚠️ 不能一上来就丢弃 —— `flush_headers()` 也是往 `self.wfile` 写（状态行 + 头），
    直接吞掉会让客户端一个字节都收不到，请求挂到超时（本批第一版就这么炸的）。
    所以分两段：`drop=False` 时透传，`end_headers()` 发完头后置 `True`。
    """

    def __init__(self, real):
        self.real = real
        self.drop = False

    def write(self, data):
        if self.drop:
            return len(data)
        return self.real.write(data)

    def flush(self):
        self.real.flush()

    def close(self):
        pass


class Handler(BaseHTTPRequestHandler):
    server_version = "MediaViewer/" + APP_VERSION
    protocol_version = "HTTP/1.1"
    # ε-12（§6.3）：socket 超时（读写都生效）。此前无超时 —— 一个建立连接后
    # 既不发完请求头也不关的客户端，会永久占住 ThreadingHTTPServer 的一个线程；
    # 多个慢连接即可把线程池耗尽（DoS 不需要恶意，浏览器崩溃也会留下这种半开连接）。
    # 120s：正常请求毫秒级完成；大图流式响应里客户端暂停超过 2 分钟才允许断
    # （恢复播放时 <video> 会自动按 range 重连，无感）。
    timeout = 120

    # B9-P2-2（2026-10-18）：不要求 application/json 的 POST 端点。
    # ⚠️ 清单只能收「确实不读 body」或「读 body 但不是 JSON」的端点；
    #    **新增写端点不要往这里加** —— 让它默认走强制 JSON（见 do_POST 的说明）。
    JSON_EXEMPT_PATHS = frozenset((
        "/api/open",            # 只用 query（打开文件/URL）
        "/api/activate",        # 二次启动唤起窗口，空 body
        "/api/restart",         # 空 body
        "/api/reindex",         # 空 body
        "/api/rescan",          # 空 body
        "/api/nsfw/scan",       # 只用 query
        "/api/open_data",       # 空 body
        "/api/bridge/install",  # 空 body
        "/api/lan/rotate",      # 空 body（前端只发一个裸 POST）
        "/api/lan/code",        # 空 body：换验证码
        "/api/solo",            # 原始图片字节，Content-Type 由 api_solo() 自己校验
        "/api/save_copy",      # 原始 PNG 字节（前端 Worker 产物），Content-Type 由端点自己校验
    ))

    # ---- 响应头（第 22 级 P1-4）----------------------------------------
    def send_response(self, code, message=None):
        """每个响应重置一次令牌标记。

        HTTP/1.1 keep-alive 下同一个 Handler 实例会连着跑多个请求，所以这个标记
        不能只在 __init__ 里置一次，必须在**每个响应起点**复位。
        """
        self._shell_tok_sent = False
        super().send_response(code, message)

    def end_headers(self):
        """把壳给的一次性令牌回显在每个响应上（含 4xx / 静态资源 / 流式响应）。

        为什么放在 end_headers 而不是逐个 _json/_static 里加：全类共 20 多处
        `end_headers()`，注入式写法只需改一处，也不会漏掉将来新增的出口
        （漏一个出口 = 那个路径上壳的探测失败 = 「后端明明起来了却报未就绪」）。
        """
        if SHELL_TOKEN and not getattr(self, "_shell_tok_sent", False):
            self.send_header(SHELL_TOKEN_HEADER, SHELL_TOKEN)
            self._shell_tok_sent = True
        super().end_headers()
        # B8-P2-1：HEAD 下头已经发完（上面的 super() 里 flush_headers 已写出去），
        # 从这里开始把 body 丢弃。放在 end_headers 而不是 do_HEAD 开头 ——
        # 否则连状态行/头一起吞掉，客户端只能等到超时。
        if getattr(self, "_head_only", False) and isinstance(self.wfile, _HeadSink):
            self.wfile.drop = True

    def log_message(self, fmt, *args):
        """§6.1：此前恒 pass ⇒ 服务端零访问日志，出问题只能靠猜。
        只记 4xx/5xx（正常的 2xx 不记）。

        ⚠️ 但「都记」会带来新问题：缺缩略图 / 文件已删这类 404 在滚屏与索引期
        可能成百上千次，而 log() 的轮转只留 1 个备份（2MB）⇒ 会把更有价值的日志挤掉，
        **等于把「没有日志」换成「日志里全是噪音」**。故：
          · 5xx 不限流（每次都记）；
          · 4xx 按分钟窗口限流（HTTP_4XX_PER_MIN 条/分），溢出时补一条汇总说明。
        """
        try:
            msg = fmt % args if args else str(fmt)
        except Exception:
            msg = str(fmt)
        # B8-P2-7：请求行里的 `?token=...` 必须剥掉再落日志（异常兜底早就剥了，
        # 这条主路径原先没剥 ⇒ LAN 令牌明文进 cmv.log 与 .log.1）。
        msg = _strip_query_for_log(msg)
        is5 = any(t in msg for t in ('" 5', "code 5"))
        is4 = any(t in msg for t in ('" 4', "code 4"))
        if not (is4 or is5):
            return
        if is5:
            log("[http] " + msg)
            return
        now = int(time.time() // 60)
        if HTTP_4XX["min"] != now:
            if HTTP_4XX["drop"]:
                log("[http] 上一分钟另有 %d 条 4xx 未逐条记录（已限流）" % HTTP_4XX["drop"])
            HTTP_4XX["min"] = now
            HTTP_4XX["n"] = 0
            HTTP_4XX["drop"] = 0
        if HTTP_4XX["n"] >= HTTP_4XX_PER_MIN:
            HTTP_4XX["drop"] += 1
            return
        HTTP_4XX["n"] += 1
        log("[http] " + msg)

    # ---- helpers -------------------------------------------------------

    def _fail500(self, ex):
        """5xx 对外统一脱敏（B8-P2-6，2026-10-18）。

        原先直接把 `类型: 异常串` 回给调用方 —— 异常里常带服务器绝对路径
        （PermissionError 的 `'C:\\...\\locked.png'`），配合桥接端点的 CORS 放行
        等于把本机目录结构读走。现在对外只有 `internal error` + 一个请求号，
        明细（含 traceback）只进日志，靠请求号把两边对上。

        ⚠️ 必须是一个**共用出口**：do_GET / do_POST 两处兜底原先各写一份，
        改文案时只改一处就会出现「两个端点两种口径」—— 这正是本批要收的形状。
        """
        rid = secrets.token_hex(4)
        log("[error] req=%s %s 处理失败: %s: %s\n%s"
            % (rid, (self.path or "").split("?")[0], type(ex).__name__, ex,
               traceback.format_exc()))
        try:
            self._json({"error": "internal error", "id": rid}, 500)
        except Exception:
            pass

    # B9-P2-7（2026-10-18）：请求体的整体读取预算（秒）。
    BODY_READ_BUDGET = 15.0

    def _read_body(self, cl: int):
        """限时读满 Content-Length 指定的字节；读不满返回 None。

        原先是一句 `self.rfile.read(_cl)`：只要声明的长度大于实际发送的字节，
        这个线程就一直等到 socket 超时（`Handler.timeout = 120s`）。若干个这样的
        半开 body 就能把 ThreadingHTTPServer 的工作线程逐个占满 —— 界面表现为
        「点什么都没反应」。触发方必须是能连上 127.0.0.1 的本地进程（浏览器发不出
        这种请求），所以这是可用性问题，不是远程利用。

        做法：分块读 + 整体 15s 预算；每块之内再用 socket 超时兜一层（随剩余预算
        收紧）。读不满一律 None，由调用方 400 + 断连（半截 body 不能留给 keep-alive）。
        """
        if cl <= 0:
            return b""
        chunks, left, t0 = [], cl, time.time()
        try:
            old_to = self.connection.gettimeout()
        except Exception:
            old_to = None
        try:
            while left > 0:
                remain = self.BODY_READ_BUDGET - (time.time() - t0)
                if remain <= 0:
                    return None
                try:
                    self.connection.settimeout(min(remain, 5.0))
                except Exception:
                    pass
                try:
                    b = self.rfile.read(min(65536, left))
                except (socket.timeout, TimeoutError, OSError):
                    return None
                if not b:
                    return None
                chunks.append(b)
                left -= len(b)
        finally:
            try:
                if old_to is not None:
                    self.connection.settimeout(old_to)
            except Exception:
                pass
        return b"".join(chunks)

    def _host_ok(self) -> bool:
        """关闭 LAN 后，即使旧监听器还活着，也只接受本机直连。"""
        return self._local_direct() or bool(STATE.get("lan"))

    def _auth_ok(self) -> bool:
        if self._local_direct():
            return True
        if not STATE.get("lan"):
            return False
        tok = STATE.get("token") or ""
        if not tok:
            return False
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "cmv_token":
                return bool(v) and v.isascii() and secrets.compare_digest(v, tok)
        return False

    _LAN_GATE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"><title>访问验证</title></head>
<body style="margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#131314;color:#e8e6e3;font:14px/1.6 -apple-system,'Segoe UI','Microsoft YaHei',sans-serif">
<div style="background:#1d1d1f;border:1px solid #333338;border-radius:14px;padding:26px 24px;width:300px;
max-width:calc(100vw - 48px);text-align:center">
<div style="font-size:15px;margin-bottom:4px">ComfyMediaViewer</div>
<div style="font-size:12px;color:#8a8a90;margin-bottom:18px">局域网访问 · 输入电脑上显示的验证码</div>
<input id="c" type="text" inputmode="numeric" autocomplete="one-time-code" maxlength="6"
placeholder="000000" style="width:100%;box-sizing:border-box;font:600 30px/1.2 ui-monospace,Consolas,monospace;
letter-spacing:10px;text-align:center;padding:12px 8px 12px 18px;background:#26262a;
border:1px solid #333338;border-radius:10px;color:#e8e6e3;outline:none">
<div id="m" style="min-height:20px;margin-top:10px;font-size:12px;color:#e0736a"></div>
<button id="b" type="button" style="width:100%;font:inherit;padding:10px;background:#4a90d9;
color:#fff;border:0;border-radius:9px;cursor:pointer">进入</button>
<div style="margin-top:14px;font-size:11px;color:#6f6f76;line-height:1.7">
验证码在电脑端「设置 · 数据 · 局域网访问」里，5 分钟内有效，用一次就换新的</div></div>
<script>
var c=document.getElementById('c'),m=document.getElementById('m'),b=document.getElementById('b'),busy=false;
function say(t){m.textContent=t||''}
c.addEventListener('input',function(){
  c.value=c.value.replace(/\\D/g,'').slice(0,6); say('');
  if(c.value.length===6) go();
});
c.addEventListener('keydown',function(e){if(e.key==='Enter')go()});
b.addEventListener('click',go);
function go(){
  var v=c.value.replace(/\\D/g,'');
  if(v.length!==6){say('请输入 6 位数字');return}
  if(busy)return; busy=true; b.textContent='验证中…';
  fetch('/api/lan/pair',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code:v})}).then(function(r){return r.json()}).then(function(j){
    if(j&&j.ok){location.replace(j.url||'/');return}
    busy=false; b.textContent='进入'; c.value=''; c.focus();
    say((j&&j.error)||'验证失败，请重试');
  }).catch(function(){busy=false;b.textContent='进入';say('网络异常，请重试')});
}
c.focus();
</script>
</body></html>"""

    def _lan_gate(self):
        """LAN 鉴权门：未通过验证时给出 401 网关页（输入 6 位验证码换 cookie）。"""
        body = self._LAN_GATE.encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # 局域网默认拒绝新端点。收藏、标签、最近使用与标记是图库元数据，
    # 远端可以编辑；主机设置、进程操作与主机文件写入仅限本机直连。
    #
    # 2026-11-09 合版（第三方 V2.13.2 StartupFixed）：判据从「黑名单」(LAN_REMOTE_DENY)
    # 翻成「白名单」—— 新端点默认对手机 403，漏配只会「点不动」，不会「删掉图」。
    # 白名单按产品语义补了 3 个（不补手机端会 403）：
    #   POST /api/prefs    排序 / 筛选 / 显示模式（写偏好）
    #   GET  /api/hidden   隐藏名单只读（启动与设置页都会拉）
    #   POST /api/solo     手机拖图上传（原先不在拒绝名单里，翻白名单后差点漏掉）
    # 写操作的 POST 白名单只放「图库元数据 + 配对」这类可回滚动作；
    # POST /api/hidden（改隐藏名单）与 POST /api/save_copy（往主机磁盘写文件）一律不给远端。
    LAN_READ_PATHS = frozenset({
        "/", "/index.html", "/api/info", "/api/lan", "/api/nsfw/status",
        "/api/list", "/api/search", "/api/meta", "/api/favs", "/api/fav_view",
        "/api/tags_list", "/api/tags_get", "/api/tags_filter", "/api/roots",
        "/api/file", "/api/thumb", "/api/dirinfo", "/api/dupes", "/api/stats",
        "/api/recent_view", "/api/marks_view", "/api/templates",
        "/api/hidden",                                 # 2026-11-09 补齐（隐藏名单只读）
    })
    LAN_POST_PATHS = frozenset({
        "/api/lan/pair", "/api/meta_batch", "/api/dims", "/api/export",
        "/api/fav", "/api/recent", "/api/mark", "/api/tags",
        "/api/prefs",                                     # 2026-11-09 补齐（排序 / 筛选 / 显示模式，POST 写偏好）
        "/api/solo",                                      # 2026-11-09 补齐（手机拖图上传）
    })
    STATIC_PREFIXES = ("/css/", "/js/", "/assets/", "/bridge/")

    def _client_is_local(self) -> bool:
        """请求是否来自本机（环回）。LAN 下用来区分「手机」与「电脑自己」。"""
        ip = (self.client_address or ("",))[0]
        return ip in ("127.0.0.1", "::1") or ip.startswith("::ffff:127.")

    def _local_direct(self) -> bool:
        """本机权限要求环回来源以及环回 Host。兼容不带 Host 的本机脚本。"""
        host = (self.headers.get("Host") or "").strip().lower()
        return self._client_is_local() and (
            not host or bool(re.fullmatch(r"(?:127[.]0[.]0[.]1|localhost|\[::1\])(?::[0-9]+)?", host))
        )

    def _remote_readonly(self) -> bool:
        return not self._local_direct()

    def _lan_code(self):
        """当前 6 位配对验证码。**只给本机看** —— 它就是这一轮的进门钥匙。"""
        if not STATE.get("lan"):
            return self._json({"error": "not found"}, 404)
        if not self._local_direct():
            return self._json({"error": "forbidden"}, 403)
        return self._json(_pair_status())

    def _json(self, obj, code=200, cors=None, *, cookie=None):
        try:
            # 2026-10-27（代码梳理 O4）：紧凑分隔符。json 默认的 ", " / ": " 会在
            # 列表类接口（上千条 item）上多出约 10% 体积，纯属协议无意义的空白。
            # 对 JSON.parse 完全等价，只影响字节数（顺带缩短序列化后的 str 驻留）。
            body = json.dumps(obj, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            # B2-P1-1 的最后一道闸：NaN / ±Inf 不是合法 JSON 字面量，json.dumps 默认
            # 会写出裸 `NaN` —— 前端 JSON.parse 与 ComfyUI 侧严格解析器都直接失败，
            # 而服务端一切正常，用户只看到「点了没反应 / 报错信息为空」。
            # 退一步把非有限值洗成 null 再序列化，保证响应体永远是合法 JSON。
            # 正常路径（没有 NaN）不进这里，所以对列表类接口零额外开销。
            body = json.dumps(_finite_clean(obj), ensure_ascii=False,
                              allow_nan=False, separators=(",", ":")).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        if cors:
            # 桥接端点专用：只有明确放行的来源才回 CORS 头，其余一概不回
            self.send_header("Access-Control-Allow-Origin", cors)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _cors_origin(self):
        """桥接端点的跨源许可：只放行**环回 http(s)** 来源（ComfyUI 端口可变，故不限端口）。

        返回可回写的 Origin，空/不放行时返回 None。不回 * 是刻意的：
        域名来源（含公网页面）拿不到 CORS 头，就读不到响应体。

        B8-P2-2（2026-10-18）：原先只校验 hostname 是环回 —— 于是 `tauri://localhost`、
        `file://`、甚至自定义 scheme 都被当成合法来源回写。scheme 也必须限死，
        否则「本机某个注册了协议的客户端」也能读桥接端点。
        """
        o = (self.headers.get("Origin") or "").rstrip("/")
        if not o:
            return None
        try:
            u = urllib.parse.urlparse(o)
            scheme = (u.scheme or "").lower()
            host = (u.hostname or "").lower()
        except Exception:
            return None
        if scheme not in ("http", "https"):
            return None
        return o if host in ("127.0.0.1", "localhost", "::1") else None

    def _send_clean_mp4(self, p: Path):
        """B7-P1-2：浏览器下载大 mp4 时不再整份读入内存。

        只读结构算出「要清零的字节范围」，再按 1MB 块顺序读源文件、边发边把落在
        范围内的字节替换成 0。等长剥离 ⇒ Content-Length 与原文件一致，进度条与
        断点续传语义都不变；内存 = 一块缓冲区。
        结构异常 ⇒ **原样**发出（与内存版 fail-safe 同口径），但 X-Clean-Applied 给 0，
        前端据此改口，不谎报「已抹除」。
        """
        ops, ok, why = _mp4_strip_plan(p)
        try:
            size = p.stat().st_size
        except OSError:
            return self._json({"error": "stat failed"}, 404)
        name = (p.stem + "_clean" + p.suffix) if ok else p.name
        self.send_response(200)
        self.send_header("Content-Type",
                         mimetypes.guess_type(str(p))[0] or "application/octet-stream")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Clean-Applied", "1" if ok else "0")
        if not ok:
            self.send_header("X-Clean-Warn",
                             urllib.parse.quote(why or "未能确认", safe=""))
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         f'attachment; filename="{urllib.parse.quote(name)}"')
        self.end_headers()
        # B8-P2-1：HEAD 只要头。这里若不早退，一个 2GB 视频的 HEAD 会真的把整份
        # 读一遍（写进 _DiscardWriter）—— 头对了，但请求要跑几分钟。
        if getattr(self, "_head_only", False):
            return
        pos = 0
        try:
            with open(p, "rb") as fh:
                while pos < size:
                    chunk = fh.read(_MP4_ZCHUNK)
                    if not chunk:
                        break
                    if ok and ops:
                        buf = bytearray(chunk)
                        _apply_ops_to_buf(buf, pos, ops)
                        self.wfile.write(buf)
                    else:
                        self.wfile.write(chunk)
                    pos += len(chunk)
        except (OSError, ValueError):
            return              # 客户端中断 / 写失败：头已经发出，只能结束

    def _send_file(self, p: Path, download=False, check_root=True, clean=False,
                   immutable=False):
        if not p.is_file() or (check_root and not inside_root(p)):
            return self._json({"error": "not found or outside root"}, 404)
        ext = p.suffix.lower()
        if ext not in MEDIA_EXTS:
            # 非媒体（.html/.svg/.txt/.lnk…）一律当附件下载，避免导航到
            # /api/file?p=… 时被同源内联渲染执行脚本
            download = True
        if clean and download and ext in _STRIP_BY_EXT:
            if ext == ".mp4":
                return self._send_clean_mp4(p)     # B7-P1-2：大视频走流式，不整份进内存
            try:
                data, clean_ok, clean_why = strip_meta_checked(ext, p.read_bytes())
            except OSError:
                data = None                       # 读取失败 → 落到下面的原始下载
            if data is not None:
                # B5（2026-10-17）：文件名加不加 _clean 后缀，与「**确认**抹过」绑定 ——
                # 没抹干净就不能挂一个名不副实的后缀（用户会以为那份是干净的）。
                name = (p.stem + "_clean" + p.suffix) if clean_ok else p.name
                self.send_response(200)
                self.send_header("Content-Type",
                                 mimetypes.guess_type(str(p))[0] or "application/octet-stream")
                self.send_header("X-Content-Type-Options", "nosniff")
                # 如实告知「这一份真的抹过了」：前端按它说话（.avi/.mkv 之类不在
                # 白名单时后端走原始下载，前端就不会再谎报「元数据已抹除」）。
                # ⚠️ B5 之前这里是**无条件给 1** —— 剥离函数 fail-safe 吐回原字节时
                # 也照样说「已抹除」。现在 1=确认抹干净、0=试过但没抹干净（原因在
                # X-Clean-Warn，percent-encoded UTF-8）。
                self.send_header("X-Clean-Applied", "1" if clean_ok else "0")
                if not clean_ok:
                    self.send_header("X-Clean-Warn",
                                     urllib.parse.quote(clean_why or "未能确认", safe=""))
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition",
                                 f'attachment; filename="{urllib.parse.quote(name)}"')
                self.end_headers()
                # 2026-11-05 P2：头已经发出去了，body 写失败（客户端提前断开）时
                # **必须**就此结束。旧写法把 write 包在 `except OSError: pass` 里，
                # 然后继续往下走原始下载 ⇒ 在同一条连接上第二次 send_response(200)。
                try:
                    self.wfile.write(data)
                except OSError:
                    pass
                return
        # P0-4：is_file() 与 stat() 之间存在 TOCTOU 窗口（外接盘掉线 / 文件被删），
        # stat 抛的 OSError 在头发出前 ⇒ 同样走单份 404 报文，不触发兜底二次报文。
        try:
            stt = p.stat()
        except OSError:
            return self._json({"error": "stat failed"}, 404)
        size = stt.st_size
        ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
        # 内容校验器：同名文件被重建（删除后重新生成到同一文件名）时 URL 不变，
        # 必须让缓存能自己发现内容已换，否则浏览器会一直拿旧图（配合前端 &v= 版本号双保险）
        etag = f'"{stt.st_mtime_ns}-{size}"'
        last_mod = self.date_time_string(stt.st_mtime)
        rng = self.headers.get("Range")
        if not rng:                       # 带 Range 的（视频拖进度）不做 304，交给下面的分段逻辑
            inm = (self.headers.get("If-None-Match") or "").strip()
            fresh = False
            if inm:
                fresh = inm == "*" or etag in [t.strip() for t in inm.split(",")]
            else:
                ims = self.headers.get("If-Modified-Since")
                if ims:
                    try:
                        _t = email.utils.parsedate_to_datetime(ims)
                        fresh = _t is not None and int(stt.st_mtime) <= int(_t.timestamp())
                    except (TypeError, ValueError):
                        fresh = False
            if fresh:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", "0")   # 显式给长度，避免 keep-alive 下等 body 挂死
                self.end_headers()
                return
        start, end = 0, size - 1
        partial = False
        if rng:
            # B8-P2-3 / B8-P2-4（2026-10-18）：只认**单段、整串匹配**的 `bytes=a-b`。
            #   · 原先用 re.match ⇒ `bytes=0-abc` 被当成 `bytes=0-`（畸形当合法）；
            #    · 原先不认逗号 ⇒ `bytes=0-0,2-3` 只发第一段，**却回 206 声称成功**，
            #      第二段被无声吞掉（HTTP 层根本没有 multipart/byteranges）。
            # 现在两种都按 RFC 允许的「忽略 Range」处理：回 200 全量，语义诚实。
            _rv = (rng or "").strip()
            m = None if "," in _rv else re.fullmatch(r"bytes=(\d*)-(\d*)", _rv)
            if m and (m.group(1) or m.group(2)):
                if m.group(1):
                    start = int(m.group(1))
                    if m.group(2):
                        end = min(int(m.group(2)), size - 1)
                else:                                 # suffix range: bytes=-N
                    start = max(0, size - int(m.group(2)))
                partial = True
        if start >= size or start > end:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("X-Content-Type-Options", "nosniff")
            # 必须显式给 Content-Length：本服务是 HTTP/1.1 + keep-alive，
            # 无长度又无响应体时客户端会一直等响应体直到连接关闭（永不发生）→ 请求挂死
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        length = end - start + 1
        # P0-4（审计二批次·第 3 级）：**先确认可读、再发响应头**。
        # 原实现是 end_headers() 之后才 open()，而 open 抛的 OSError
        # （FileNotFoundError / PermissionError / 外接盘掉线 / OneDrive 占位）
        # 不在下面的捕获集里 ⇒ 异常冒泡到 do_GET 兜底再发一份 500 报文 ⇒ 客户端
        # 收到「200 + Content-Length: N」却零字节，body 变成第二份 HTTP 报文，
        # keep-alive 同连接出现多个状态行、后续响应错位配发（协议破坏，已实测）。
        # 修法：头发出之前就把文件打开，打不开就走正常 404/500 单份报文路径。
        try:
            f = open(p, "rb")
        except OSError:
            return self._json({"error": "open failed"}, 404)
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        # 2026-10-27（代码梳理 O5）：缩略图 URL 由前端拼了 `v=<mtime>-<size>`
        # （见 frontend/js/state.js 的 thumbURL），内容一变 URL 就变 ⇒ 可以安全长缓存。
        # 原先一律 no-cache：一次网格刷新就是上百次「再验证」往返（哪怕 304 也要过网络）。
        # 原图 /api/file 不带这个保证（同名重建覆盖时 URL 不变），仍走 no-cache。
        self.send_header("Cache-Control",
                         "public, max-age=31536000, immutable" if immutable else "no-cache")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_mod)
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{urllib.parse.quote(p.name)}"')
        self.end_headers()
        # B8-P2-1：HEAD 只回头。文件已在上面打开过（确认可读），这里直接关掉走人，
        # 不把整份内容读进 _DiscardWriter。
        if getattr(self, "_head_only", False):
            f.close()
            return
        try:
            with f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except OSError:
            # P0-4：写循环里也可能抛 OSError（盘掉线 / 读取中途失败）。此时头已发出，
            # 无法再改状态码，但**必须吞掉**，否则同样会触发 do_GET 兜底的第二次报文。
            # BrokenPipe/ConnectionReset/ConnectionAborted 本就属 OSError 子类，一并覆盖
            # （ε-14 补 ConnectionAbortedError 的语义在下面保留说明）。
            pass

    def _page(self):
        html = page_file().read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header("Cache-Control", "no-store")   # 页面随 exe 更新，禁用浏览器缓存
        self.end_headers()
        self.wfile.write(html)

    def _static(self, path: str):
        """/css/ /js/ /assets/ /bridge/ 静态资源：走条件请求（304），取代原 no-store。

        批次 E-P5（2026-10-06）：原先这四个前缀一律 `no-store`，每次启动/F5 都要
        重下 731KB / 25 个文件（实测）。改成 `no-cache` + ETag/Last-Modified 后，
        内容未变的文件在浏览器缓存里直接复用（回 304 空体），省掉的是传输与解析。

        ⚠️ 为什么**不能**用 `max-age=...` 长期缓存：这些文件名**不带内容 hash**
        （`app.js` 而非 `app.<hash>.js`），换版本后浏览器会一直拿旧 JS ⇒ 症状是
        「界面没更新」甚至白屏，用户会当成 exe 坏了。`no-cache` 每次都回源校验，
        是这个命名方式下唯一安全的做法。要消除请求数就必须先给文件名加 hash。

        ETag 用 mtime_ns + size，与 `_send_file` 同口径：源码模式下改完 JS 即失效
        （开发态 F5 立刻看到新内容），打包后 exe 换了 JS 也必然对不上。
        """
        fp = frontend_file(path)
        if not fp:
            return self._json({"error": "not found"}, 404)
        try:
            stt = fp.stat()
        except OSError:
            return self._json({"error": "not found"}, 404)
        etag = f'"{stt.st_mtime_ns}-{stt.st_size}"'
        last_mod = self.date_time_string(stt.st_mtime)
        inm = (self.headers.get("If-None-Match") or "").strip()
        fresh = False
        if inm:
            fresh = inm == "*" or etag in [t.strip() for t in inm.split(",")]
        else:
            ims = self.headers.get("If-Modified-Since")
            if ims:
                try:
                    _t = email.utils.parsedate_to_datetime(ims)
                    fresh = _t is not None and int(stt.st_mtime) <= int(_t.timestamp())
                except (TypeError, ValueError):
                    fresh = False
        if fresh:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", last_mod)
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", "0")   # 显式给长度，避免 keep-alive 下等 body 挂死
            self.end_headers()
            return
        data = fp.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         STATIC_TYPES.get(fp.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")   # 复用前必须校验：文件名不带 hash
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", last_mod)
        self.end_headers()
        self.wfile.write(data)

    # ---- HEAD / OPTIONS（B8-P2-1，2026-10-18）---------------------------
    # 此前这两个方法完全没定义 ⇒ BaseHTTPRequestHandler 走 send_error(501)，
    # 响应体还是 `text/html` 的 Python 错误页。后果有两层：
    #   ① 任何 HEAD 健康检查 / 链接预览 / 断点续传探测全部拿不到长度；
    #   ② CORS 预检（非简单请求必发 OPTIONS）必然失败 —— 桥接脚本现在只发简单 GET
    #      才侥幸能用，一旦加自定义头就整条链路断掉。
    def do_HEAD(self):
        """与 GET 完全同一条路径，只把响应体丢进黑洞。

        这样做而不是另写一套头输出：HEAD 的契约就是「头与 GET 一致、无 body」，
        复制一份头逻辑早晚会漂移（本批 15 条里有一半就是这种漂移的后果）。

        ⚠️ 4.3-1（2026-11-05 审计）：**写副作用必须挡在 do_GET 之前**。
        这些端点全挂在 GET 上（历史设计），HEAD 直接复用 do_GET 等于「只看头就
        把事干了」：`/api/pull` 会真的摘走队列里的件并挂 in-flight（回执永远不来
        ⇒ 120s 后重投）、`/api/migrate_report` 取走即清（用户唯一一次换根提示被
        吞掉）、`/api/stats` / `/api/search` 会起全量索引、`/api/thumb` 会写缓存。
        HEAD 对这些路径只回「这条路存在」，状态一点不碰。
        """
        real = self.wfile
        self.wfile = _HeadSink(real)
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self.wfile = real
            self._head_only = False

    def do_OPTIONS(self):
        """CORS 预检：只对环回来源回许可头，其余一律不回（更不回 *）。"""
        if not self._local_direct():
            self.close_connection = True
            return self._json({"error": "forbidden host"}, 403)
        origin = self._cors_origin()
        self.send_response(200 if origin else 403)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
            # 允许调用方要求的自定义头（桥接脚本可能要带 X-Requested-With 之类）
            req_h = (self.headers.get("Access-Control-Request-Headers") or "").strip()
            self.send_header("Access-Control-Allow-Headers", req_h or "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
        if not origin:
            self.send_header("Allow", "GET, HEAD, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    # ---- GET -----------------------------------------------------------

    def do_GET(self):
        if not self._host_ok():
            self.close_connection = True
            return self._json({"error": "forbidden host"}, 403)
        u = urllib.parse.urlparse(self.path)
        if not self._auth_ok():
            return self._lan_gate()
        is_static = u.path.startswith(self.STATIC_PREFIXES)
        if self._remote_readonly() and not (is_static or u.path in self.LAN_READ_PATHS):
            return self._json({"error": "远程只读：这一步需要在电脑上操作",
                               "readonly": True}, 403)
        # HEAD 走同一套访问控制，但不能执行带副作用的 GET。
        if getattr(self, "_head_only", False) and u.path in self.WRITE_GET_PATHS:
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "0")
            self.send_header("Allow", "GET, HEAD")
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(u.query)
        # P1-38：带写副作用的 GET（桥接取件/回执）拒绝跨站请求。
        # 这些端点原先只在 _host_ok（只认 Host，而 Host 由 URL 决定、攻击者可控）
        # 之下裸奔 ⇒ 任意外网页面 <img src="http://127.0.0.1:8765/api/pull">
        # 即可取走并销毁一件排队的工作流。
        if u.path in self.WRITE_GET_PATHS and self._cross_site_write_get():
            log("[security] 拒绝跨站写 GET：%s" % u.path)
            return self._json({"error": "forbidden"}, 403)
        # B8-P1-1（2026-10-10）：敏感 GET（目录枚举）同款拒跨站。
        if u.path in self.SENSITIVE_GET_PATHS and self._cross_site_write_get():
            log("[security] 拒绝跨站敏感 GET：%s" % u.path)
            return self._json({"error": "forbidden"}, 403)
        try:
            if u.path in ("/", "/index.html"):
                return self._page()
            if is_static:   # 模块化前端静态资源 + 回传桥接脚本
                return self._static(u.path)
            if u.path == "/api/info":
                # P0-11：如实自报 pid。桌面壳靠它做退出兜底 —— 壳内「重启程序」是后端
                # 自己起新进程接管（同端口），壳登记的旧 pid 已死、taskkill 必失败，
                # 只能按端口反查真实 pid 才不至于把新后端留成孤儿（并锁住 backend exe）。
                return self._json({"root": STATE["root"], "has_pil": HAS_PIL,
                                   "title": APP_TITLE,
                                   "version": APP_VERSION,
                                   "pid": os.getpid(),
                                   "settings_file": str(SETTINGS_FILE),
                                   "delete_mode": load_delete_mode(),
                                   # P1-34：最近一次「重启程序」起新进程失败的原因
                                   # （null=没失败过/正在重启中；非空=重启失败但仍在本进程服务）
                                   "restart_failed": (None if _RestartState["ok"] is not False
                                                      else _RestartState["err"]),
                                   "trash_keep_days": load_trash_keep_days(),
                                   # 前端启动时同步一次（和 delete_mode 同款套路）：
                                   # 阈值/开关的权威值在服务端，前端只做缓存与显示
                                   "nsfw_available": nsfw_available(),
                                   "nsfw_reason": nsfw_reason(),
                                   "nsfw_enabled": nsfw_enabled(),
                                   "nsfw_threshold": load_nsfw_threshold(),
                                   "last_export_dir": load_settings().get("last_export_dir", ""),
                                   "download_dir": ("" if self._remote_readonly() else
                                                    load_settings().get("download_dir", "")),
                                   # B1-3：数据目录写入状态 —— 设置页据此显式告警。
                                   # 便携模式指向只读 U 盘 / 写保护卡 / 外接盘掉线时，
                                   # 设置 / 收藏 / 标签 / 换根记忆全都「看着改了、重启回原样」，
                                   # 此前唯一的痕迹是数据目录里那行日志（而它恰好写不进去）。
                                   # 字段：ok / dir_writable / what / error / at
                                   "data_write": data_write_state(),
                                   # 2026-11-07：手机访问状态（前端据此显示「只读」标记）
                                   "lan": bool(STATE.get("lan")),
                                   "readonly": self._remote_readonly()})
            if u.path == "/api/lan":              # 设置 · 数据页「手机访问」状态
                return self._json(_lan_status(readonly=self._remote_readonly(),
                                               local=self._local_direct()))
            if u.path == "/api/lan/code":         # 配对验证码（本机可见，远程 403）
                return self._lan_code()
            if u.path == "/api/nsfw/status":     # 内容安全：引擎可用性 / 档位 / 阈值 / 扫描进度
                return self._json(nsfw_status())
            if u.path == "/api/sysinfo":          # 设置面板「数据 / 关于」页：目录占用与版本
                return self._json(sysinfo())
            if u.path == "/api/hidden":           # 隐藏文件夹名单（2026-11-08）
                root = STATE.get("root") or ""
                return self._json({"ok": True,
                                   "root": _hidden_root_key(root),
                                   "dirs": hidden_list(),
                                   "max": HIDDEN_MAX})
            if u.path == "/api/list":
                return self.api_list(qs)
            if u.path == "/api/search":
                q = qs.get("q", [""])[0].strip()
                if not q:
                    return self._json({"building": False, "done": 0,
                                       "total": 0, "matched": 0, "truncated": False,
                                       "results": []})
                ensure_index()
                base = qs.get("base", [""])[0]
                if base:
                    bp = Path(base)
                    if not bp.is_dir() or not inside_root(bp):
                        base = ""           # 非法 base：退回全根目录搜索
                # 2026-10-18：`limit` 可选（前端/脚本想问「最多给几条」），
                # 一律经 _clamp_search_limit 收敛 —— 不接受任意大的数字。
                _lv = (qs.get("limit", [""])[0] or "").strip()
                return self._json(search_index(q, base, _lv or None))
            if u.path == "/api/meta":
                return self.api_meta(qs)
            if u.path == "/api/favs":
                return self._json({"cats": fav_cats()})
            if u.path == "/api/datadir_status":   # ε-12（§6.3）：datadir 后台迁移进度
                return self._json(datadir_status())
            if u.path == "/api/tags_list":
                # 第 16 级：dangling / unknown 与 tags **同源**一次性给出 ——
                # 前端据此把不可判定的数字标成「—」，并在左栏补一句「N 条标签已失效」。
                _td = tag_stats_detail()
                return self._json({"tags": _td["tags"],
                                   "dangling": _td["dangling"],
                                   "unknown": _td["unknown"]})
            if u.path == "/api/tags_get":
                p = qs.get("p", [""])[0]
                if not p:
                    return self._json({"error": "bad request"}, 400)
                # B8-P2-5（2026-10-18）：本端点原先不做 inside_root —— 同区间的
                # /api/file、/api/meta、/api/thumb 都校验，只有它漏。于是可以拿任意
                # 绝对路径来问「打过什么标签」，等于把根外文件的存在性当探针用。
                # 与 /api/tags_filter 同口径：根外直接 400，不做 Path.resolve 之外的任何处理。
                try:
                    tp = Path(p).resolve()
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not inside_root(tp):
                    return self._json({"error": "路径在根目录之外"}, 400)
                return self._json({"tags": tags_of(str(tp))})
            if u.path == "/api/tags_filter":
                raw = qs.get("tags", [""])[0]
                mode = qs.get("mode", ["and"])[0]
                if mode not in ("and", "or", "not"):
                    mode = "and"
                rec = qs.get("rec", ["1"])[0] == "1"
                base = qs.get("path", [""])[0]
                # ε-14（§6.8）：base 此前无校验 —— GET 可对任意目录做标签查询，
                # 借结果集探测根外文件的存在性。显式传 base 时要求在 root 内
                # （缺省走 STATE["root"]，天然安全）。
                if base and not inside_root(Path(base)):
                    return self._json({"error": "目录在根目录之外"}, 400)
                wanted = [t for t in raw.split("|") if t.strip()]
                excl = [t for t in (qs.get("not", [""])[0] or "").split("|") if t.strip()]
                return self._json({"items": tags_filter(wanted, base, mode, rec, excl),
                                   "tags": wanted, "mode": mode, "not": excl})
            if u.path == "/api/roots":
                return self._json({"roots": load_roots()})
            if u.path == "/api/templates":
                return self._json({"templates": list_templates()})
            if u.path == "/api/pull":            # 回传·拉取模式：ComfyUI 页面内脚本取件
                origin = self._cors_origin()
                want = (qs.get("port", [""])[0] or "").strip()
                tab = (qs.get("tab", [""])[0] or "").strip()   # ε-7：标签页标识（可空）
                # P0-10：页面侧报告「我正忙 / 不在载入」。带 busy=1 时不跑 abort 兜底回收
                # （旧判据会把正在载入的件当 abort 抢走重投 ⇒ 同一工作流进画布两遍）。
                busy = (qs.get("busy", [""])[0] or "").strip() == "1"
                # X1（2026-10-28）：纯心跳。桥接脚本在「等页面就绪」期间每 5s 发一次，
                # 只为报活 —— 绝不能取件。改前它打的是一条普通 /api/pull，于是队首件被
                # bridge_take 取走挂成 in-flight，而调用方只有 .catch、从不读 seq ⇒ 没人
                # 回执 ⇒ 件干等 BRIDGE_ACK_TTL(120s) 才被放回（「已送达但两分钟后才动」）。
                # 这里的顺序有讲究：**先 touch 再短路**，否则心跳不续命、CMV 会判「没有
                # 活着的桥接页面」。beat 是纯新增参数，老脚本不带 ⇒ 行为完全不变。
                beat = (qs.get("beat", [""])[0] or "").strip() == "1"
                if want:                         # 空轮询也算心跳，据此判断页面是否还在
                    bridge_touch(want, tab)
                    if busy:
                        bridge_busy(want)        # 4.3-7：载入期不按 stale 抢件
                elif not PULL_NOPORT_WARN[0]:
                    # P2-3：无 port 的取件无法归属到任何页面，件会记成 port=""。
                    # 只提示一次（这类请求可能每 1.5s 来一轮，不能刷日志）。
                    PULL_NOPORT_WARN[0] = True
                    log("[bridge] 收到不带 port 的取件（老脚本 / 手工请求）："
                        "该件记为无主，只能由 release(空端口) 或 120s 超时回收")
                if beat:
                    return self._json({"seq": 0}, cors=origin)
                return self._json(bridge_take(want, reclaim=not busy) or {"seq": 0},
                                  cors=origin)
            if u.path == "/api/pull/ack":        # 页面回执·载入成功 -> 该件才真正销毁（V2.8.4）
                return self._json(bridge_ack(qs.get("seq", [""])[0],
                                             qs.get("claim", [""])[0]),   # P0-9：投递代次
                                  cors=self._cors_origin())
            if u.path == "/api/pull/nack":       # 页面回执·载入失败 -> 放回队列重投（V2.8.4）
                return self._json(bridge_nack(qs.get("seq", [""])[0],
                                              qs.get("try", [""])[0],      # ε-7：带退避
                                              qs.get("claim", [""])[0]),   # P0-9：投递代次
                                  cors=self._cors_origin())
            if u.path == "/api/pull/drop":       # 页面回执·**已放弃** -> 终结该件 + 记账（ε-7 §6.7 P2-2）
                return self._json(bridge_drop(qs.get("seq", [""])[0],
                                              qs.get("reason", [""])[0],
                                              qs.get("name", [""])[0],
                                              qs.get("claim", [""])[0]),   # P0-9：投递代次
                                  cors=self._cors_origin())
            if u.path == "/api/pull/release":    # 页面取件超时放弃 -> 立刻放回队列（P1-32）
                return self._json(bridge_release((qs.get("port", [""])[0] or "").strip()),
                                  cors=self._cors_origin())
            if u.path == "/api/bridge/status":   # 前端查询：有没有活着的桥接页面
                return self._json({"ok": True, "pages": bridge_pages(),
                                   # 方案③：窗口可用性单独一份（10 分钟窗），
                                   # 前端只用它决定「另开窗口」；老前端仍读 pages。
                                   "windows": bridge_windows(),
                                   "tabs": bridge_tabs(),           # ε-7：标签页维度
                                   "dropped": bridge_dropped(),     # ε-7：被放弃的件
                                   "queued": len(PULL_QUEUE),
                                   "target": _comfy_port(),
                                   "installed": bridge_installed(),
                                   "stale": bridge_stale(),
                                   "active": bridge_active()},
                                  cors=self._cors_origin())
            if u.path == "/api/file":
                return self._send_file(Path(qs.get("p", [""])[0]),
                                       download=qs.get("dl") == ["1"],
                                       clean=qs.get("clean") == ["1"])
            if u.path == "/api/thumb":
                try:
                    size = int(qs.get("s", ["320"])[0])
                except ValueError:
                    size = 320
                size = max(64, min(size, 1024))   # 钳制尺寸，防异常参数撑爆缓存
                rp = Path(qs.get("p", [""])[0])
                if not rp or not inside_root(rp):      # 越权路径一律拒绝
                    return self._json({"error": "forbidden"}, 403)
                t = thumb_for(rp, size=size)
                if t:
                    # 带 v= 才敢长缓存：那是前端给的「内容指纹」（mtime-size），
                    # 源图一改 URL 就变，缓存自然失效。老书签/外部调用不带 v 的
                    # 仍然走 no-cache，行为不变（见 _send_file 的 immutable 分支）。
                    return self._send_file(t, check_root=False,
                                           immutable=bool(qs.get("v", [""])[0]))
                return self._json({"error": "no thumb"}, 404)
            if u.path == "/api/browse":          # 目录选择器：列出子目录/上级/盘符（不视为浏览 root，任意合法目录均可）
                p = qs.get("path", [""])[0]
                if not p:
                    return self._json({"drives": _fixed_dirs(), "path": "", "parent": None, "dirs": []})
                bp = Path(p)
                if not bp.is_dir():
                    return self._json({"error": "目录不存在"}, 400)
                dirs = []
                try:
                    with os.scandir(bp) as it:
                        for e in it:
                            try:
                                if e.is_dir():
                                    cnt = _dir_media_count(e.path)
                                    dirs.append({"name": e.name, "path": e.path, "n": cnt, "tag": "dir"})
                            except OSError:
                                continue
                except OSError as ex:
                    return self._json({"error": str(ex)}, 500)
                dirs.sort(key=lambda x: x["name"].lower())
                parent = str(bp.parent) if bp.parent != bp else None
                comfy = None
                try:
                    cr = comfy_root()
                    if cr and (cr / "output").is_dir():
                        comfy = str(cr / "output")
                    elif cr and cr.name.lower() == "output":
                        comfy = str(cr)
                except Exception:
                    comfy = None
                return self._json({"drives": _fixed_dirs(), "path": str(bp),
                                   "parent": parent, "dirs": dirs, "comfy": comfy})
            if u.path == "/api/dirinfo":            # 目录指纹：前端轮询检测新文件
                p = Path(qs.get("path", [""])[0] or STATE["root"])
                if not p.is_dir() or not inside_root(p):
                    return self._json({"error": "目录不存在或在根目录之外"}, 400)
                return self._json({"sig": dir_signature(
                    p, qs.get("rec", ["0"])[0] == "1")})
            if u.path == "/api/dupes":              # 重复图检测
                if not HAS_PIL:                     # 需要 Pillow 做 dHash，缺库时给提示而不是 500
                    return self._json({"error": "查重需要 Pillow（pip install pillow）",
                                       "need_pil": True}, 503)
                p = Path(qs.get("path", [""])[0] or STATE["root"])
                if not p.is_dir() or not inside_root(p):
                    return self._json({"error": "目录不存在或在根目录之外"}, 400)
                # 2026-11-15：扫描预算与相似阈值改成调用方可选（原先写死 3000 / 6，
                # 11942 张的库永远只覆盖 25%，而界面只写一句「已达扫描上限」）。
                # limit=0 / all ⇒ 不设上限（全库）；near 限在 0~16，避免误传把分组炸开。
                try:
                    _lim = (qs.get("limit", ["3000"])[0] or "").strip().lower()
                    limit = 0 if _lim in ("0", "all", "none", "") else int(_lim)
                    near = int(qs.get("near", ["6"])[0])
                except ValueError:
                    return self._json({"error": "limit / near 需要是整数"}, 400)
                limit = 0 if limit <= 0 else min(limit, 200000)
                near = max(0, min(near, 16))
                return self._json(find_dupes(
                    p, qs.get("rec", ["0"])[0] == "1", limit=limit, near=near))
            if u.path == "/api/fav_view":           # 跨目录收藏视图
                return self._json({"items": fav_view(qs.get("cat", [""])[0])})
            if u.path == "/api/stats":
                ensure_index()
                # 顺带做一次 NSFW 增量补测：挂在既有轮询上，不另开定时器。
                # auto_only=True 保证「从没扫过」时什么都不做 —— 首次打开软件
                # 不会因为看了眼统计就把 CPU 打满；用户点过「开始检测」之后才自动跟上新图。
                ensure_nsfw(auto_only=True)
                # 同款思路挂上换根迁移检测：前端每秒都在调这里，迁移检测自己有
                # 「新根无缓存文件才动手」+ 30 秒节流，空转成本接近零。
                root_migrate_maybe()
                j = stats_view()
                j["nav"] = nav_stats()      # 左栏导航统一口径（全部/图片/视频/收藏/工作流/标签）
                # P4（V2.12.1）：迁移完成待领取时随 stats 捎个信 —— 前端只在
                # boot+4s / 切根+4s 各查一次报告，大库迁移超 4s 提示就永远丢了；
                # 挂在 stats 上等于每次统计刷新都有一次领取机会（报告取走即清）。
                with _MIGRATE_LOCK:
                    j["mig"] = _MIGRATE_STATE["done"] is not None
                return self._json(j)
            if u.path == "/api/migrate_report":  # 换根迁移报告（取走即清：一次迁移只提示一次）
                with _MIGRATE_LOCK:
                    r = _MIGRATE_STATE["done"]
                    _MIGRATE_STATE["done"] = None
                return self._json({"report": r})
            if u.path == "/api/recent_view":        # 最近使用视图
                return self._json({"items": recent_view()})
            if u.path == "/api/marks_view":         # 已标记视图（可按状态过滤）
                # 2026-10-27（代码梳理 O6）：标记表读一次、存活判定扫一遍。
                # 原先 marks_view 内部 load_marks 一次、mark_stats() 再 load 一次，
                # 而且 mark_stats 会把 marks_view 刚 stat 过的那批路径重新 isfile 一遍
                # —— 与 B7-P1-4 在 stats_view 里修掉的是同一个「同一响应两套口径」的分叉源。
                _mk = load_marks()
                _alive = _alive_counts(load_favs(), load_recent(), _mk)
                return self._json({"items": marks_view(qs.get("state", [""])[0], _mk),
                                   "marks": mark_stats(_mk, _alive["mark_state"])})
            if u.path == "/api/trash":           # 回收站列表
                try:
                    return self._json({"items": trash_list()})
                except Exception as ex:
                    # trash_list 自身已容错；这里只兜「意料之外」——宁可回一条可读
                    # 错误，也不要让回收站界面永久 500 打不开。
                    log(f"[warn] 回收站列表异常: {type(ex).__name__}: {ex}")
                    return self._json({"items": [], "error": "回收站索引读取异常：%s"
                                       % type(ex).__name__}, 500)
            if u.path == "/api/trash_thumb":        # 回收站缩略图（路径服务端拼接，免穿越）
                key = qs.get("key", [""])[0]
                if not re.fullmatch(r"\d+_[0-9a-f]{6}", key):
                    return self._json({"error": "bad key"}, 400)
                # 2026-11-05 P2：与「还原 / 彻底删除」统一口径 —— 旧代码这里仍是
                # `glob(key + ".*")`，无扩展名文件（README）永远 404：列表里有条目、
                # 缩略图永远不显示。
                f = _trash_stored_path(key, load_trash().get(key))
                if not f or f.suffix.lower() not in IMAGE_EXTS:
                    return self._json({"error": "no thumb"}, 404)
                t = thumb_for(f, size=160)
                if t:
                    return self._send_file(t, check_root=False)
                return self._json({"error": "no thumb"}, 404)
            return self._json({"error": "unknown endpoint"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # ε-14（§6.8）：补 ConnectionAbortedError —— 客户端（下载器/杀软中间人）
            # 强杀连接时 Windows 抛的是它而不是 ConnectionReset，漏捕会刷 ERROR 日志。
            pass
        except DataLockTimeout as ex:
            # 第 24 级：跨进程数据锁超时（另一实例长时间持有）。
            # 明确回 409 + 可读文案 —— 绝不静默丢更新，也不让它落进 500 脱敏兜底。
            return self._json({"error": str(ex)}, 409)
        except Exception as ex:
            # §6.1：服务端要留痕；B8-P2-6：对外**不回显**异常串（含服务器绝对路径）。
            # do_GET / do_POST 两处兜底同款 —— 统一走 _fail500，不再各写一套。
            self._fail500(ex)

    def api_list(self, qs):
        path = qs.get("path", [""])[0] or STATE["root"]
        recursive = qs.get("rec", ["0"])[0] == "1"
        hid = hidden_snapshot()          # 隐藏名单快照（整表按 mtime 缓存，取一次用到底）
        p = Path(path)
        if not p.is_dir() or not inside_root(p):
            return self._json({"error": "目录不存在或在根目录之外"}, 400)
        dirs, items = [], []
        # P1（2026-11-15）：递归模式下子目录卡片上的 n 由下面的栈遍历**顺带累计**，
        # 这里只记下标、n 先留 0，遍历结束再回填。
        # 旧写法是给每个子目录单独调 `_count_media()`：它把整棵子树走一遍，紧接着
        # 下面的递归栈又把同一棵树走一遍 —— 同一批目录项读两次。一万多张的库上
        # 光是「读目录」就白花约四成时间，深目录越多越糟。
        _dirpos = {} if recursive else None
        # 列表本身不依赖索引，但「原比例铺满」布局要每条带 w/h。索引是后台线程建的，
        # 这里只负责**踢一脚**（已有索引时按 REINDEX_INTERVAL 节流，等于空操作）；
        # 冷启动的第一条列表可能还没尺寸，前端会调 /api/dims 补一次。
        try:
            ensure_index()
        except Exception:
            pass
        favs = load_favs()
        tags = load_tags()
        # 一份 NSFW 快照整个循环复用。这是性能红线：每张卡片各取一次锁+读一次盘，
        # 一次 api_list 就要多几千次系统调用，网格直接卡住。
        _nctx = nsfw_ctx()
        # 尺寸来源：提示词索引的内存快照（整表替换 ⇒ 引用在本请求内稳定，不用逐条抢锁）。
        # ⚠️ 冷启动时索引还在建（building=True），此时**一律不读文件头**：
        #    全库一万多张 png 在索引里都 miss，逐张 open 在机械盘 / 外置盘上要好几秒，
        #    而首屏的目录列表（含侧栏文件夹）会被它一起拖住 —— 表现就是「刚打开卡住、
        #    文件夹半天不出来」。索引建好后尺寸自然就有，中间那几十秒交给前端按需补
        #    （前端只补**已渲染**的那几百张，不再是全量）。
        with IDX_LOCK:
            _idxmap = IDX["map"]
            _idx_ready = bool(_idxmap) and not IDX["building"]
        _head_left = [240]        # 单次列表请求的读头预算（索引就绪后 miss 的只有新图，几十条封顶）

        def pack(name: str, sp: str, ext: str, t: str, st) -> dict:
            # ns = 分数（前端改阈值时现场重算，省一次全量重拉）；-1 = 以 n 为准
            _nl, _ns = nsfw_fields(sp, int(st.st_mtime), st.st_size, _nctx)
            d = {"name": name, "path": sp, "type": t,
                 "size": st.st_size, "mtime": int(st.st_mtime),
                 "playable": t == "image" or ext in PLAYABLE_VIDEO,
                 "png": ext == ".png", "fav": sp in favs,
                 "favcat": favs[sp]["cat"] if sp in favs else None,
                 "tags": list(tags.get(sp, [])),
                 "n": _nl, "ns": _ns}
            # 宽高给「原比例铺满」布局用；索引里没有（视频 / gif / bmp）就不带这两个键，
            # 前端按 1:1 处理 —— 不能给 0，那会让比例算出 0/0。
            # head=：只在「索引已就绪」且**确实没收录**（png/jpeg）时才读头 ——
            # 这种 miss 只可能是「刚出图还没进 30s 增量」，一次请求顶多几十条。
            # 冷启动（索引未就绪）不读头，见上面的说明；webp/gif 也不在这里读，
            # 交给前端 /api/dims 按需补。
            # ⚠️ 预算必须按「真的要去读」来扣：早先只看扩展名就扣，结果前面几百张
            #    索引命中的图把 240 次额度全吃掉，轮到末尾的新图时预算已空 ——
            #    「新图还是方格」原样复发（2026-11-04 实测 3002 张夹具时踩到）。
            _e = _idxmap.get(sp)
            _head = (_idx_ready and _e is None
                     and ext in (".png", ".jpg", ".jpeg")
                     and _head_left[0] > 0)
            _w, _h = dims_of(sp, _idxmap, head=_head, st=st)
            if _head:
                _head_left[0] -= 1
            if _w and _h:
                d["w"], d["h"] = _w, _h
            return d

        def media_type(ext: str):
            if ext in IMAGE_EXTS:
                return "image"
            if ext in VIDEO_EXTS:
                return "video"
            return None

        try:
            # 2026-10-22：子目录与文件**同一趟 scandir**收掉。
            # 原先非递归分支要 os.scandir 两次（先列目录、再列文件），1 万文件的目录上
            # 等于把整个目录项多读一遍；DirEntry 本身带类型，不需要二次 stat。
            with os.scandir(p) as it:
                for e in it:
                    try:
                        if e.is_dir():
                            # 2026-11-08：隐藏名单里的子目录连卡片都不出 —— 用户既然
                            # 不想在软件里看到它，留一张「点得进去」的卡片就白隐藏了。
                            if _hid_hit(hid, e.path):
                                continue
                            if recursive:
                                # 计数交给下面的栈遍历回填（见 _dirpos 说明）
                                _dirpos[e.path] = len(dirs)
                                dirs.append({"name": e.name, "path": e.path, "n": 0})
                            else:
                                # 非递归只数直接子文件，本来就是一趟 scandir，不用改
                                dirs.append({"name": e.name, "path": e.path,
                                             "n": _dir_media_count(e.path)})
                            continue
                        if recursive:
                            continue     # ⚠️ 递归模式的文件由下面的栈统一走（含根目录），
                                         # 这里再收一次会把根目录的文件算两遍（实测 2.86MB→5.34MB）
                        ext = os.path.splitext(e.name)[1].lower()
                        t = media_type(ext)
                        if t:
                            items.append(pack(e.name, e.path, ext, t, e.stat()))
                    except OSError:
                        continue
            if recursive:
                stack = [(str(p), 0)]
                visited = {os.path.realpath(str(p))}
                # P1：cnt[目录] = 该目录**直接**含的媒体数；遍历结束后按层级自底向上
                # 汇总，就得到卡片要的「含后代总数」—— 与旧 `_count_media()` 同一口径，
                # 但整棵树只 scandir 一次。
                cnt = {str(p): 0}
                parent_of = {}
                while stack:
                    cur, depth = stack.pop()
                    cnt.setdefault(cur, 0)
                    try:
                        with os.scandir(cur) as it:
                            for e in it:
                                try:
                                    if e.is_dir():
                                        # 2026-11-08：隐藏目录整棵剪掉 —— 不压栈 ⇒
                                        # 里面的文件一个都不会被 stat / 打包。
                                        if depth < 10 and not _hid_hit(hid, e.path):
                                            rp = os.path.realpath(e.path)
                                            if rp not in visited:
                                                visited.add(rp)
                                                parent_of[e.path] = cur
                                                cnt.setdefault(e.path, 0)
                                                stack.append((e.path, depth + 1))
                                        continue
                                    ext = os.path.splitext(e.name)[1].lower()
                                    t = media_type(ext)
                                    if t:
                                        items.append(pack(e.name, e.path, ext, t,
                                                          e.stat()))
                                        cnt[cur] += 1
                                except OSError:
                                    continue
                    except OSError:
                        continue
                # 自底向上：子目录先汇总完，再把自己的数加给父目录。
                # 按路径段数降序即可保证「子一定先于父」被处理（同一目录的段数唯一）。
                for d0 in sorted(cnt, key=lambda s: len(Path(s).parts), reverse=True):
                    par = parent_of.get(d0)
                    if par is not None:
                        cnt[par] = cnt.get(par, 0) + cnt[d0]
                for sp, i in _dirpos.items():
                    if sp in cnt:
                        dirs[i]["n"] = cnt[sp]
                    else:
                        # 边角情况：这个子目录在第一趟出现过、却没被递归栈收下
                        # （典型是 junction 指向本树里的另一处，被全局 visited 剪掉）。
                        # 旧实现是从它自己出发独立计数，会照常给出数字；这里退回旧路径，
                        # 保证「常规树零开销、边角树结果不变」。
                        dirs[i]["n"] = _count_media(Path(sp), hid)
        except OSError as ex:
            return self._json({"error": str(ex)}, 500)
        # hidden_self：当前所在目录本身就在隐藏名单里（从收藏 / 历史 / 地址栏进来的）。
        # 边界规则：这种时候**照常显示**，否则用户会被锁在门外；前端据此给一条提示，
        # 让「这里本来就没图」和「这里被你隐藏了」在界面上分得开。
        self._json({"root": STATE["root"], "path": str(p.resolve()),
                    "has_thumb": HAS_PIL, "dirs": dirs, "items": items,
                    "hidden_self": _hid_hit(hid, p)})

    def api_meta(self, qs):
        p = Path(qs.get("p", [""])[0])
        if not p.is_file() or not inside_root(p):
            return self._json({"error": "not found or outside root"}, 404)
        st = p.stat()
        suffix = p.suffix.lower()
        meta = {}
        if suffix == ".png":
            meta = png_meta(p)
        elif suffix in (".jpg", ".jpeg", ".webp"):
            meta = parse_jpeg_meta(p)
        elif suffix in VIDEO_EXTS:
            # 视频：读容器里的 prompt / workflow，产出的结构与 png_meta 一致，
            # 所以下面那句 parse_meta(meta) 原样就能用 —— 图片视频共用一条解析路径
            meta = video_meta(p)
        out = {"name": p.name, "size": st.st_size, "mtime": int(st.st_mtime),
               "meta": meta, "parsed": parse_meta(meta),
               # E5-7：区分「读失败」与「本来就没有」。为 None 时前端照旧说「没有内嵌生成信息」；
               # 有值时前端改说「读取失败：<原因>」—— 不再把「读不到」讲成「本来没有」。
               "metaError": meta_fail_reason(p)}
        return self._json(out)

    # ---- 单图查看（V2.10.7）--------------------------------------------

    def api_solo(self):
        """接收一张外部图片 -> 落 SOLO_DIR -> 返回绝对路径（前端随后照常预览）。

        只收原始字节，且 Content-Type 必须显式是受支持的图片类型：跨站页面想
        往这里塞文件，必先过 CORS 预检（image/* 与 application/octet-stream 都
        不在安全列表里），再叠加 do_POST 顶部的 _same_origin()，两道都过不去。
        库里原本的图片不会走这条路 —— 前端只有「打开图片」按钮会调它。
        """
        ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ct not in SOLO_CTYPES:
            return self._json({"error": "unsupported content-type"}, 415)
        if not self.headers.get("Content-Length"):
            return self._json({"error": "length required"}, 411)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json({"error": "bad request"}, 400)
        if n <= 0:
            return self._json({"error": "empty body"}, 400)
        if n > SOLO_MAX_BYTES:
            return self._json(
                {"error": "图片超过 %d MB 上限" % (SOLO_MAX_BYTES >> 20)}, 413)
        raw = self._post_body        # ε-14：body 已在 do_POST 顶部统一消费
        if len(raw) != n:
            return self._json({"error": "incomplete body"}, 400)
        ext = solo_sniff(raw)
        if not ext:
            return self._json({"error": "仅支持 PNG / JPG / WEBP / GIF / BMP"}, 415)
        dest = SOLO_DIR / (hashlib.sha1(raw).hexdigest()[:16] + ext)
        try:
            SOLO_DIR.mkdir(parents=True, exist_ok=True)
            if not dest.is_file() or dest.stat().st_size != len(raw):
                # 先写 .part 再原子换名：避免前端在写盘途中读到半截文件。
                # .part 名字带线程号，并发上传同一张图时不会互相踩。
                tmp = SOLO_DIR / ("%s.%s.part" % (dest.name, _tmp_tag()))
                tmp.write_bytes(raw)
                os.replace(tmp, dest)
        except OSError as ex:
            return self._json(
                {"error": "写入失败：%s: %s" % (type(ex).__name__, ex)}, 500)
        solo_prune()
        return self._json({"ok": True, "path": str(dest), "name": dest.name,
                           "size": len(raw), "dir": str(SOLO_DIR)})

    # ---- POST ----------------------------------------------------------

    def _same_origin(self) -> bool:
        """校验 POST 来源：无 Origin（同源 fetch/旧浏览器）放行；
        带 Origin 时必须与 Host 一致，跨站网页发来的请求一律拒绝。"""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            return urllib.parse.urlparse(origin).netloc == self.headers.get("Host", "")
        except Exception:
            return False

    # P1-38：带写副作用的 GET 端点。浏览器对 <img>/<script>/<link> 发起的跨站
    # 请求**不带 Origin**，所以光靠 _same_origin() 拦不住；这几条必须叠一层
    # 「跨站请求不得转发」的手段：
    #   ① Sec-Fetch-Site: 现代 Chromium/Edge/Firefox 对**所有**请求都带，
    #      值为 cross-site 即跨站 —— 这是最直接的判据，且 <img> 也带；
    #   ② Origin 存在且非环回 → 拒（带 Origin 的场景走 _same_origin 同款口径）；
    #   ③ Referer 存在且非环回 → 拒（老浏览器兜底）。
    # ⚠️ B8-P1-3（2026-10-10）：以上三条在「三个头**全缺**」时原先一律放行
    #    （fail-open）⇒ `<img referrerpolicy="no-referrer">` 或任何不发
    #    Sec-Fetch-Site 的旧浏览器照样能把件取走。现在：**有头就按头判**，
    #    三个都没有才回退到浏览器指纹（`_browser_fetch`）—— 浏览器一律拒，
    #    本机脚本放行（否则门禁/CLI 全被打成 403）。
    # ⚠️ 只对下面这几个端点 + `SENSITIVE_GET_PATHS` 生效，**不**能全局加：
    #    静态资源经 <img>/<link> 加载，被这些头带着走会把正常页面弄坏。
    # 2026-11-05 P2：曾列过 "/api/pull/status"，但 do_GET 里从来没有这个路由
    #   （只有 /api/pull 与 ack/nack/drop/release），是一行死名单，已删。
    WRITE_GET_PATHS = ("/api/pull", "/api/pull/ack", "/api/pull/nack",
                       "/api/pull/drop", "/api/pull/release",
                       # B8-P1-2（2026-10-10）：下面这些 GET 同样带**写副作用**，此前榜单上
                       # 没有它们 ⇒ 任意网页一张 <img> 就能让后端起全量索引 / 换根迁移线程、
                       # 逼它写缩略图缓存，还能把「取走即清」的迁移报告吞掉 —— 用户唯一
                       # 一次换根提示被吃掉，界面上看起来什么都没发生。
                       "/api/stats",           # ensure_index / ensure_nsfw / root_migrate_maybe
                       "/api/migrate_report",  # 取走即清（破坏性读）
                       "/api/search",          # ensure_index（冷启动会起索引线程）
                       "/api/thumb",           # 写缩略图缓存
                       "/api/trash_thumb",     # 写回收站缩略图缓存
                       "/api/dupes",           # 全目录 Pillow 解码：读盘 + CPU 放大
                       # A01（2026-11-05 审计复核）：这两个此前漏在名单外 ——
                       # `/api/list` 会 `ensure_index()`（冷启动起全量索引线程、写
                       # prompt_index.json），`/api/dirinfo` 会整目录遍历并冲掉
                       # `_SIG` 指纹缓存。任意外站一张 <img> 就能反复踢索引 / 逼着
                       # 递归扫盘，把缓存打掉后再让正常页面白跑一遍。
                       "/api/list",            # ensure_index（起索引线程 + 落盘）
                       "/api/dirinfo",         # 目录递归遍历 + 覆盖 _SIG 缓存
                       )
    # ⚠️ 判据分两套（2026-11-07 远程访问第 1 批）：
    #    本机模式 → 只认**环回**（`_origin_is_loopback`），与改前完全一致；
    #    LAN 模式 → 认「**同源**」（`_url_is_same_host`：Origin/Referer 的 host:port
    #    与 Host 头一致）。后者是为 Safari/iOS 修的 —— 它不发 `Sec-Fetch-Site`，
    #    只能落到 Referer 判据，而 Referer 就是局域网地址，按环回判必 403
    #    （症状：iPhone 能开页面、画廊永远空白）。详见 `_origin_trusted`。

    # B8-P1-1（2026-10-10）：没有写副作用、但**会吐出本机信息**的 GET —— 目录选择器
    # 返回全盘一级目录名 + 绝对路径（含 ComfyUI 安装路径）。今天跨源还读不到响应体
    #（本端不回 ACAO），可一旦 CORS 放宽或 Host 白名单被绕开，它就是**整个文件系统的
    # 枚举器**。所以与写副作用 GET 同款拒跨站，先把口子收在同一道闸门上。
    SENSITIVE_GET_PATHS = ("/api/browse",)

    @staticmethod
    def _origin_is_loopback(url: str) -> bool:
        """判断一个 URL/Origin 串是否指向环回地址（空串也算放行，由调用方决定）。"""
        if not url:
            return True
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
        except Exception:
            return False
        return host in ("127.0.0.1", "localhost", "::1", "[::1]")

    def _url_is_same_host(self, url: str) -> bool:
        """URL 的 host:port 是否与本次请求的 Host 头一致。

        `--lan` 下页面地址是 http://192.168.x.x:8765，浏览器给的 Origin / Referer
        天生不是环回；此时唯一站得住的判据就是「和你访问我的那个地址同源」。
        比较用 netloc 原样比：非默认端口浏览器一定带端口，Host 头也带，两边写法一致。
        """
        if not url:
            return True
        try:
            netloc = (urllib.parse.urlparse(url).netloc or "").lower()
        except Exception:
            return False
        if not netloc:
            return False
        return netloc == (self.headers.get("Host") or "").strip().lower()

    def _origin_trusted(self, url: str) -> bool:
        """Origin / Referer 是否可信：本机模式只认环回，LAN 模式认「同源」。

        2026-11-07（远程访问方案第 1 批）：LAN 下若继续只认环回，Safari / iOS
        不发 `Sec-Fetch-*`，只能落到 Referer 判据，而 Referer 就是局域网地址
        ⇒ 被判成跨站 ⇒ `/api/list`、`/api/stats`、`/api/thumb` 全部 403，
        表现是「手机能打开页面，但画廊永远空白」。Chromium 因为有第 ① 条
        `Sec-Fetch-Site` 先命中，反而看不出问题 —— 这也是它一直没被发现的原因。
        本机模式**保持原样**（只认环回），不放宽任何攻击面。
        """
        if not url:
            return True
        if STATE.get("lan"):
            return self._url_is_same_host(url)
        return self._origin_is_loopback(url)

    def _browser_fetch(self) -> bool:
        """这条请求带不带**浏览器指纹**（Fetch Metadata 头 / 浏览器 UA）？

        B8-P1-3（2026-10-10）：只在「Sec-Fetch-Site / Origin / Referer **三个头全缺**」
        这一个歧义点上用得上 —— 浏览器（`<img referrerpolicy="no-referrer">`、
        旧浏览器）一律当跨站；本机脚本（门禁 / CLI / 用户自己的工具）放行，因为
        它们本来就能直接读写文件系统，对它们做 CSRF 判定没有意义，只会把本地
        工具链打成一片 403。**只要有一个头能判，就轮不到它**（见调用方）。
        """
        if (self.headers.get("Sec-Fetch-Site") is not None
                or self.headers.get("Sec-Fetch-Mode") is not None):
            return True
        return "mozilla" in (self.headers.get("User-Agent") or "").lower()

    def _cross_site_write_get(self) -> bool:
        """True = 这是一条**跨站**对写副作用 GET 的请求，必须拒。

        B8-P1-3（2026-10-10）：原实现在「三个头**全缺**」时直接放行（fail-open）⇒
        `<img referrerpolicy="no-referrer">` 本就不带 Referer、旧浏览器也不发
        Sec-Fetch-Site，于是照样能把件取走并冻结。现在改成：**只要能判就判**
        （有头就按头判），三个都没有时才回退到浏览器指纹 —— 见 `_browser_fetch`。
        """
        # ① Sec-Fetch-Site（最可靠：<img> 也带）
        sfs = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sfs:
            return sfs not in ("same-origin", "same-site", "none")
        # ② Origin 在就按 Origin 判（非环回 = 跨站）
        origin = self.headers.get("Origin")
        if origin is not None:
            return not self._origin_trusted(origin)
        # ③ Referer 兜底（无 Sec-Fetch-Site 时）
        referer = self.headers.get("Referer")
        if referer:
            return not self._origin_trusted(referer)
        # ④ 三个头全缺：**只有这里**存在「放行还是拒绝」的分歧 ⇒ 交给浏览器指纹。
        return self._browser_fetch()

    def do_POST(self):
        if not self._host_ok():
            self.close_connection = True
            return self._json({"error": "forbidden host"}, 403)
        u = urllib.parse.urlparse(self.path)
        # ε-12（§6.3）：POST body 全局上限（各端点 rfile.read 前的统一闸门）。
        # 超限时**不读 body** 并断开连接 —— keep-alive 下残留 body 若不处理，
        # 会被下一次请求当成请求行解析出乱码；close_connection 让服务端直接关。
        if self.headers.get("Content-Length") is not None:
            try:
                _cl = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self.close_connection = True
                return self._json({"error": "bad content-length"}, 400)
            if _cl < 0 or _cl > MAX_BODY_BYTES:
                self.close_connection = True
                log("[http] 拒绝超大 POST body：%d 字节 (>%d) %s"
                    % (_cl, MAX_BODY_BYTES, u.path))
                return self._json(
                    {"error": "body 超过 %d MB 上限" % (MAX_BODY_BYTES >> 20)}, 413)
            # ε-14（§6.8）：body 在此**统一消费** —— 此前 /api/restart、/api/activate
            # 等不读 body 的路由会把 keep-alive 连接上的残留字节留给下一个请求，
            # 被当成请求行解析出乱码（HTTP/1.1 复用连接的必踩坑）。
            # 各分支改从 self._post_body 取，不再各自 rfile.read。
            self._post_body = self._read_body(_cl)
            if self._post_body is None:
                # B9-P2-7：声明长度大于实发字节 ⇒ 预算内没读满。半截 body 绝不能再
                # 留给下一个请求（会被当成请求行解析出乱码），直接断连。
                self.close_connection = True
                return self._json({"error": "读取请求体超时或长度不符"}, 400)
        else:
            self._post_body = b""
        # CSRF 防护：跨站请求必带 Origin 头，同源 POST 的 Origin 与 Host 一致
        if not self._same_origin():
            return self._json({"error": "forbidden"}, 403)
        # 对读 JSON body 的写操作端点强制 application/json，
        # 杜绝 text/plain 简单请求绕过 CORS 预检。
        #
        # B9-P2-2（2026-10-18）：改成**默认要求、显式豁免**。
        # 原先是白名单制（列 18 个），新增写端点忘了登记 ⇒ 悄悄少一层纵深防御，
        # 而且没有任何信号。现在反过来：**不在豁免清单里就必须是 JSON** ——
        # 漏登记的后果变成「前端立刻 403」（可见），而不是「少了第二道闸」。
        # 豁免清单只收两类：
        #   ① 完全不读 body（只用 query / 空 body）；
        #   ② 读 body 但不是 JSON —— 目前只有 /api/solo（收原始图片字节，
        #      Content-Type 由 api_solo() 自己按 SOLO_CTYPES 校验）。
        if u.path not in self.JSON_EXEMPT_PATHS:
            ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ct != "application/json":
                return self._json({"error": "forbidden"}, 403)
        qs = urllib.parse.parse_qs(u.query)
        # 配对是「用 6 位码换令牌」本身，必须允许**裸访问** —— 会用它的正是还没
        # 凭证的设备。只开这一条缝：其余端点照旧要令牌，且它只发只读令牌。
        # 本机（环回 + 环回 Host）不需要令牌，理由同 do_GET：桌面壳拿不到令牌。
        # 跨站写请求另有 _same_origin() 拦着，这里放行不会给恶意页面开口子。
        if u.path != "/api/lan/pair" and not self._auth_ok():
            return self._json({"error": "unauthorized"}, 401)
        # 远程（手机）只读：与 GET 同一张拒绝名单。
        if self._remote_readonly() and u.path not in self.LAN_POST_PATHS:
            return self._json({"error": "远程只读：这一步需要在电脑上操作",
                               "readonly": True}, 403)
        try:
            if u.path in ("/api/lora-links", "/api/lora-links/open"):
                if not self._local_direct():
                    return self._json({"error": "请在本机操作"}, 403)
                try:
                    if len(self._post_body) > 131072:
                        raise ValueError("请求过大")
                    data = json.loads(self._post_body)
                    if not isinstance(data, dict):
                        raise ValueError("请求格式不正确")
                    if u.path == "/api/lora-links":
                        return self._json({"items": lora_links.resolver.resolve_many(
                            data.get("items"), comfy_root(), DATA_DIR)})
                    url = data.get("url")
                    if not lora_links.valid_external_url(url):
                        raise ValueError("只允许打开 Civitai 模型或搜索页面")
                except (ValueError, TypeError):
                    return self._json({"error": "LoRA 请求格式不正确"}, 400)
                if not webbrowser.open(url):
                    return self._json({"error": "无法打开默认浏览器，请复制链接打开"}, 500)
                return self._json({"ok": True})
            if u.path == "/api/activate":
                # 单实例：由二次启动的本程序进程 POST 调用（GET 会被任意网页用
                # <img>/<link> 触发成骚扰性弹窗，故只接受 POST + 同源校验）
                port = self.server.server_address[1]
                open_url = f"http://127.0.0.1:{port}/"
                _t = threading.Timer(0.1, lambda: webbrowser.open(open_url))
                _t.daemon = True   # ε-13（§6.6）：非守护会拖住进程退出；Timer 构造器不收 daemon 关键字
                _t.start()
                log("[activate] 已唤起浏览器")
                return self._json({"ok": True})
            if u.path == "/api/solo":              # 单图查看：收一张外部图片（V2.10.7）
                return self.api_solo()
            if u.path == "/api/lan/pair":          # 另一台设备用 6 位码换取访问权
                try:
                    data = json.loads(self._post_body)
                    code = data.get("code")
                    if not isinstance(code, str):
                        raise ValueError
                except (ValueError, AttributeError):
                    return self._json({"error": "bad json"}, 400)
                ip = self.client_address[0]
                with _PAIR_LOCK:
                    if not STATE.get("lan"):
                        return self._json({"error": "手机访问未开启"}, 400)
                    ok, msg, left = _pair_verify(code, ip)
                    tok = STATE.get("token")
                if not ok:
                    log("[lan] 配对失败 %s：%s" % (ip, msg))
                    return self._json({"ok": False, "error": msg, "left": left}, 403)
                log("[lan] 设备配对成功：%s" % ip)
                return self._json({"ok": True, "url": "/"},
                                  cookie=f"cmv_token={tok}; Path=/; Max-Age=31536000; "
                                         "SameSite=Strict; HttpOnly")
            if u.path == "/api/lan/code":          # 本机：立刻换一个新的验证码
                if not STATE.get("lan"):
                    return self._json({"error": "手机访问未开启"}, 400)
                _pair_new()
                log("[lan] 已生成新的配对验证码")
                return self._json(_pair_status())
            if u.path == "/api/lan/toggle":        # 设置 · 数据页：开关「手机访问」
                try:
                    _b = json.loads((self._post_body or b"{}").decode("utf-8"))
                except Exception:
                    return self._json({"error": "bad json"}, 400)
                _on = bool((_b or {}).get("on"))
                saved = _lan_apply(_on)
                _lan_rebind_soon()
                if not saved:
                    msg = "无法保存局域网设置" if _on else "已断开远程访问，但无法保存关闭状态"
                    return self._json({"error": msg}, 500)
                log("[lan] 手机访问已%s" % ("开启" if _on else "关闭"))
                return self._json(_lan_status(readonly=self._remote_readonly(),
                                              local=self._local_direct()))
            if u.path == "/api/lan/rotate":        # 换令牌：所有已连设备立刻失效
                with _PAIR_LOCK:
                    if not STATE.get("lan"):
                        return self._json({"error": "手机访问未开启"}, 400)
                    if not _lan_apply(True, rotate=True):
                        return self._json({"error": "无法保存新令牌"}, 500)
                log("[lan] 令牌已重新生成")
                return self._json(_lan_status(readonly=self._remote_readonly(),
                                              local=self._local_direct()))
            if u.path == "/api/restart":
                # 一键重启：先回包，再由后台线程起新进程并退出自己
                # P1-34：起进程失败的结论在后端线程里才知道，前端可轮询 /api/info
                #        的 restart_failed 字段确认（正常重启时本进程已退出，页面重连）
                _RestartState["ok"] = None
                _RestartState["err"] = ""
                restart_self(self.server)
                return self._json({"ok": True, "msg": "正在重启…"})
            if u.path == "/api/root":
                body = self._post_body
                try:
                    raw = json.loads(body.decode("utf-8"))["path"]
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                # 2026-11-05 P2：空串 / 纯空白必须拒。`Path("").resolve()` 等于进程
                #   cwd（源码模式就是工程目录、打包后是程序目录），`is_dir()` 为真
                #   ⇒ 旧代码会静默把媒体根换成程序目录，界面上看图一片空白。
                if not isinstance(raw, str) or not raw.strip():
                    return self._json({"error": "路径为空"}, 400)
                try:
                    new = str(Path(raw).resolve())
                except (OSError, ValueError):
                    return self._json({"error": "路径无效"}, 400)
                if not Path(new).is_dir():
                    return self._json({"error": "目录不存在"}, 400)
                # P0-1（10-15 全代码审计）：此前只校验 is_dir() —— 于是 root 可以被登记成
                # `C:\`，inside_root 的 `root in q.parents` 随即对整卷恒真，
                # /api/delete 等端点等于对全盘放开。这里直接拒收卷根 / 共享根。
                if _is_volume_root(Path(new)):
                    return self._json({"error": "不能把磁盘根目录（如 C:\\）或网络共享根设为媒体根目录，"
                                                "请选择一个具体的文件夹"}, 400)
                with STATE_LOCK:
                    STATE["root"] = new
                with IDX_LOCK:
                    # P1-15：换根即换代。旧根的在跑线程未必立刻察觉（它只在循环里查
                    # STATE["root"]），换代后它即使写回也会被 _index_builder 的闸门拦下。
                    IDX_GEN[0] += 1
                    IDX_THREAD[0] = None
                remembered = True
                if REMEMBER_ROOTS:
                    # B1-3：换根本身是内存态、一定成功；但「记住它」要写盘。
                    # 写不进去必须说出来 —— 否则用户以为记住了、重启回到旧目录。
                    remembered = add_root(new)
                pruned = prune_favs(new)          # 清理该 root 下的悬空收藏
                prune_tags(new)                   # 同步清理悬空标签
                nsfw_prime(new)                   # NSFW 缓存是按根目录走的，换目录要换表
                resp = {"ok": True, "root": new, "pruned": pruned}
                if not remembered:
                    _ws = data_write_state()
                    resp["warning"] = ("目录已切换，但「常用目录 / 上次目录」未能写入数据"
                                       "目录（%s）—— 重启后会回到原来的目录。请检查数据"
                                       "目录是否只读 / 所在盘是否掉线。"
                                       % (_ws.get("error") or "写入失败"))
                    resp["data_write_error"] = _ws.get("error") or ""
                return self._json(resp)
            if u.path == "/api/root/remove":
                # 从「常用目录」移除一条（2026-11-11：此前只有 add_root，列表只进不出）。
                # ⚠️ **不动当前根**：它还指着 STATE["root"] / 索引 / NSFW 缓存表，删了也
                # 只是从 settings 抹掉，前端 refreshRoots 立刻把它补回菜单（见 nav.js 的
                # `list.unshift(S.root)`）—— 用户看到的就是「点了没反应」。
                # 要清掉它得先切到别的目录，这里如实拒绝而不是假装成功。
                body = self._post_body
                try:
                    raw = json.loads(body.decode("utf-8"))["path"]
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not isinstance(raw, str) or not raw.strip():
                    return self._json({"error": "路径为空"}, 400)
                if _root_key(raw) == _root_key(STATE.get("root") or ""):
                    return self._json(
                        {"error": "这个目录正在使用中，先切到别的目录再移除"}, 400)
                ok_rr, changed = remove_root(raw)
                if not ok_rr:
                    _ws = data_write_state()
                    return self._json(
                        {"ok": False, "data_write_error": _ws.get("error") or "",
                         "error": "没能写入数据目录，常用目录没有改动（%s）"
                                  % (_ws.get("error") or "写入失败")}, 500)
                return self._json({"ok": True, "removed": changed, "roots": load_roots()})
            if u.path == "/api/batch_export":
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    paths = d.get("paths") or []
                    dest = str(d.get("dest") or "")
                    if not isinstance(paths, list) or not paths or not dest.strip():
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                return self._json(batch_export(paths, dest))
            if u.path == "/api/open":
                p = Path(qs.get("p", [""])[0])
                if not p.is_file() or not inside_root(p):
                    return self._json({"error": "not found or outside root"}, 404)
                reveal_in_explorer(p)          # 打开并强制置前，避免弹到后台
                return self._json({"ok": True})
            if u.path == "/api/save_copy":
                # 卡片 / 大图右键「保存混淆副本（小番茄）」：算法在前端 Worker（PNG 无损），
                # 后端只按既有命名规则落盘到 `_混淆\`。请求体是 **PNG 原始字节**，路径与
                # 方向走请求头（百分号编码，中文名不会在 latin-1 头里炸）。
                # **只给本机**：副本写在跑服务的这台机器上，远端点它等于把文件默默留在
                # 用户磁盘、手机那头什么也拿不到（与「复制文件 / 另存为」同一条口径）。
                if not self._local_direct():
                    return self._json({"error": "混淆副本只能保存在运行本程序的电脑上"}, 403)
                raw = urllib.parse.unquote(self.headers.get("X-CMV-Src") or "")
                mode = (self.headers.get("X-CMV-Mode") or "encrypt").strip()
                if not raw.strip():
                    return self._json({"error": "bad request"}, 400)
                p = Path(raw)
                if not p.is_file() or not inside_root(p):
                    return self._json({"error": "not found or outside root"}, 404)
                r = save_copy_bytes(p, mode, self._post_body or b"")
                if r.get("ok"):
                    log("[混淆] %s %s → %s（%d KB · 前端 Worker 产物）"
                        % (mode, p.name, r["name"], (r["bytes"] + 1023) // 1024))
                    return self._json(r)
                return self._json(r, int(r.pop("code", 500)))
            if u.path == "/api/copy_file":
                # 大图右键「复制文件（含元数据）」：把磁盘上那一份放上剪贴板（CF_HDROP）。
                # **只给本机**：剪贴板属于运行服务端的这台机器，远端（手机/另一台电脑）
                # 点它只会把文件塞进主机的剪贴板，用户在**自己的**机器上粘贴什么都没有，
                # 属于「静默无效」—— 比直接拒绝更糟，所以这里显式 403 并让前端不显示该项。
                if not self._local_direct():
                    return self._json(
                        {"error": "复制文件只支持在运行本程序的电脑上操作"}, 403)
                try:
                    d = json.loads(self._post_body.decode("utf-8"))
                    raw = str(d.get("path") or "")
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not raw.strip():
                    return self._json({"error": "bad request"}, 400)
                p = Path(raw)
                if not p.is_file() or not inside_root(p):
                    return self._json({"error": "not found or outside root"}, 404)
                err = copy_files_to_clipboard([p])
                if err:
                    return self._json({"error": err}, 500)
                return self._json({"ok": True, "name": p.name})
            if u.path == "/api/save_as":
                # 大图右键「另存为原件（保留元数据）」：原生保存框选路径，字节原样复制。
                # **只给本机**：对话框由服务端这台机器弹出，远端用户看不到、也点不了，
                # 与「复制文件」同一条口径（前端也不显示这一项）。
                if not self._local_direct():
                    return self._json(
                        {"error": "另存为只支持在运行本程序的电脑上操作"}, 403)
                try:
                    d = json.loads(self._post_body.decode("utf-8"))
                    raw = str(d.get("path") or "")
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not raw.strip():
                    return self._json({"error": "bad request"}, 400)
                p = Path(raw)
                if not p.is_file() or not inside_root(p):
                    return self._json({"error": "not found or outside root"}, 404)
                r = save_image_as(p)
                if r.get("ok"):
                    return self._json(r)
                # 弹不出保存框（无 GUI / 调用失败）：让前端回落「浏览器原样下载」，
                # 不当成失败 —— 用户的诉求是「拿到这份原件」，换个通道照样满足。
                code = 200 if (r.get("canceled") or r.get("no_dialog")) else 500
                return self._json(r, code)
            if u.path == "/api/open_url":
                # 关于页「项目」入口：桌面壳是 iframe，页面拿不到窗口 API，交给本机
                # 后端用系统默认浏览器打开。只认枚举 key（ABOUT_LINKS），不接收 URL；
                # 只给本机 —— 远端点击会在主机屏幕上弹窗，等于静默无效（同 open_external）。
                if not self._local_direct():
                    return self._json({"error": "只支持在运行本程序的电脑上打开",
                                       "reason": "remote"}, 403)
                try:
                    d = json.loads(self._post_body or b"{}")
                    if not isinstance(d, dict):
                        raise ValueError
                    k = str(d.get("k") or "")
                    dry = bool(d.get("dry"))
                except (ValueError, TypeError):
                    return self._json({"error": "bad json"}, 400)
                url = ABOUT_LINKS.get(k)
                if not url:
                    return self._json({"error": "unknown link"}, 400)
                if dry:                       # 门禁 / 自检用：只回地址，不真开浏览器
                    return self._json({"ok": True, "url": url, "dry": True})
                try:
                    if not webbrowser.open(url):
                        return self._json(
                            {"error": "无法打开默认浏览器，请复制链接打开"}, 500)
                except Exception as ex:
                    return self._json(
                        {"error": "%s: %s" % (type(ex).__name__, ex)}, 500)
                return self._json({"ok": True, "url": url})
            if u.path == "/api/open_external":
                # 大图右键「用默认图片查看器打开」：本机拉起关联程序（远端点了会在
                # **主机**屏幕上弹窗，等于静默无效，故 403 + 前端不显示）。
                if not self._local_direct():
                    return self._json(
                        {"error": "用默认程序打开只支持在运行本程序的电脑上操作"}, 403)
                p = Path(qs.get("p", [""])[0])
                if not p.is_file() or not inside_root(p):
                    return self._json({"error": "not found or outside root"}, 404)
                r = open_external(p)
                if r.get("ok"):
                    return self._json(r)
                # 扩展名不在白名单 ⇒ 400（调用方传错）；拉不起来（无关联程序）⇒ 500
                return self._json(r, 400 if r.get("error", "").startswith("只能用") else 500)
            if u.path == "/api/delete":
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    if isinstance(d.get("paths"), list):       # 批量
                        raw = [str(Path(x).resolve()) for x in d["paths"] if x]
                    else:                                      # 单个（兼容旧调用）
                        raw = [str(Path(d["path"]).resolve())]
                    # 去重：同一路径传两次时第二次会因「文件不存在」被计入 failed，
                    # 前端会把已成功删除的文件误标成失败
                    targets = list(dict.fromkeys(raw))
                    # ⚠️ P1-1：这里原本是 [:500] 静默截断，踩过一个坑 —— 服务端只删前 500 条
                    #   却照常回 deleted=500/failed=[]，而前端按「全部路径 − failed」计算已删集合，
                    #   于是第 501 条以后的文件**磁盘还在、界面已消失**（实测 620 条残留 120 个）。
                    #   现在改为：不截断（本机回环请求，几千条也只多几秒）；确实超上限时
                    #   把被丢弃的路径并入 failed 如实上报，前端就不会把它们当成已删。
                    dropped = []
                    if len(targets) > DELETE_MAX_PATHS:
                        dropped = targets[DELETE_MAX_PATHS:]
                        targets = targets[:DELETE_MAX_PATHS]
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not targets:
                    return self._json({"error": "bad request"}, 400)
                # 删除去向：请求可显式指定（批量脚本 / 桥接），缺省用设置面板里的偏好
                # B9-P2-5（2026-10-18）：非法 mode 原先静默回落到默认值 —— 用户填
                # `systemm`（拼错）本意「进系统回收站」，实际进了应用内回收站，
                # 之后「去资源管理器还原」自然找不到。只有**缺省**才回落，写错就 400。
                mode = str(d.get("mode") or "").strip().lower()
                if mode and mode not in DELETE_MODES:
                    return self._json({"error": "mode 只能是 app 或 system"}, 400)
                if not mode:
                    mode = load_delete_mode()
                deleted, failed = 0, []
                dead = []              # 删除成功的全部路径（计数 + 目录统计失效用）
                purge_meta = []        # 需要清掉收藏/标签/标记的路径
                keys = {}              # 应用内回收站 key（写快照 / 供撤销还原）
                fellback = []          # 系统回收站不可用、退回应用内回收站的路径
                # O3（2026-10-28）：走应用内回收站的路径先攒起来，最后**一次**批量搬。
                # 原来每条都调 trash_move ⇒ 每条都要整份 index.json 的 load + save
                # （回收站 1 万条时单次 15ms，删 500 个 = 7.5s，还全程占着 TRASH_LOCK）。
                to_trash = []          # [(路径字符串, 是否系统回收站退回)]
                for t in targets:
                    p = Path(t)
                    # ε-14（§6.8）：路由层此前不做 inside_root —— 本机回环之外，
                    # DNS rebinding / 恶意页面（若同源校验被绕过）可借 POST 删任意盘文件。
                    # 逐条校验：根外的路径进 failed 如实回报，不静默吞。
                    if not inside_root(p):
                        failed.append(t)
                        continue
                    if mode == "system":
                        err = recycle_bin_delete(p)
                        if err is None:
                            # 进了系统回收站：不动收藏/标签/标记 —— 用户从资源管理器
                            # 还原后路径没变，元数据照样有效，清了反而丢数据
                            deleted += 1
                            dead.append(t)
                            with IDX_LOCK:               # 同步清理搜索索引
                                IDX["map"].pop(t, None)
                            continue
                        # 目标卷没有回收站（exFAT / 网络盘）或路径超长 → 退回应用内回收站。
                        # 让「删除」这个意图达成，比点了没反应好；响应里回报数量告知用户
                        to_trash.append((t, True))
                    else:
                        to_trash.append((t, False))
                if to_trash:
                    # 一次锁、按块落盘；返回 {路径: (错误, key)}，语义与逐条 trash_move 一致
                    moved = trash_move_many([t for t, _fb in to_trash])
                    for t, is_fb in to_trash:
                        err, _key = moved.get(t, ("移入回收站失败", None))
                        if err:
                            failed.append(t)
                            continue
                        keys[t] = _key
                        purge_meta.append(t)
                        if is_fb:
                            fellback.append(t)
                        deleted += 1
                        dead.append(t)
                        with IDX_LOCK:                   # 同步清理搜索索引
                            IDX["map"].pop(t, None)
                meta_failed = []                         # §6.1：元数据清理失败要带回去，别让「已删除」独占说法
                if purge_meta:                           # 收藏/标签/标记一次性批量清理，避免每文件全量读写
                    # ① 只取快照，不动原表（2026-09-24 外审 P1-1：原顺序是「先清表、后写快照」，
                    #    快照写盘失败时原表已清、回收站又没有快照 ⇒ 收藏/标签/标记真丢。
                    #    现改为先把快照持久化进回收站条目，成功才清原表；快照写失败则原表原地保留）
                    fav_snap, tag_snap, mark_snap = {}, {}, {}
                    with _store_lock(FAV_LOCK, "删除清理"):
                        favs = load_favs()
                        for t in purge_meta:
                            if t in favs:
                                fav_snap[t] = favs[t].get("cat") or "默认"
                    with _store_lock(TAGS_LOCK, "删除清理"):
                        tgs = load_tags()
                        for t in purge_meta:
                            if t in tgs:
                                tag_snap[t] = list(tgs[t])
                    with _store_lock(MARKS_LOCK, "删除清理"):
                        mk = load_marks()
                        for t in purge_meta:
                            if t in mk:
                                mark_snap[t] = mk[t].get("state")
                    snap_ok = True
                    if fav_snap or tag_snap or mark_snap:  # ② 快照先落盘，供还原时恢复
                        with _store_lock(TRASH_LOCK, "删除清理"):
                            idx = load_trash()
                            for t in purge_meta:
                                k = keys.get(t)
                                if not k or k not in idx:
                                    continue
                                if t in fav_snap:
                                    idx[k]["fav"] = fav_snap[t]
                                if t in tag_snap:
                                    idx[k]["tags"] = tag_snap[t]
                                if t in mark_snap:
                                    idx[k]["mark"] = mark_snap[t]
                            if not save_trash(idx):      # 快照没落盘 ⇒ 不清原表，元数据原地保留
                                snap_ok = False
                                meta_failed.append("回收站元数据快照")
                    if snap_ok:                          # ③ 快照已持久化才清原表
                        with _store_lock(FAV_LOCK, "删除清理"):
                            if fav_snap:
                                favs = load_favs()
                                for t in fav_snap:
                                    favs.pop(t, None)
                                if not save_favs(favs):  # §6.1：文件已删、收藏没清干净 ⇒ 必须说出来
                                    meta_failed.append("收藏")
                        with _store_lock(TAGS_LOCK, "删除清理"):
                            if tag_snap:
                                tgs = load_tags()
                                for t in tag_snap:
                                    tgs.pop(t, None)
                                if not save_tags(tgs):
                                    meta_failed.append("标签")
                        with _store_lock(MARKS_LOCK, "删除清理"):
                            if mark_snap:
                                mk = load_marks()
                                for t in mark_snap:
                                    mk.pop(t, None)
                                if not _save_json(MARKS_FILE, mk):
                                    meta_failed.append("标记")
                if deleted:
                    census_invalidate()                  # 目录数量变了，下次统计重扫
                failed = failed + dropped                # P1-1：被截断的路径如实计入未删除
                if len(targets) == 1 and failed:         # 单删失败：保持旧响应语义
                    return self._json({"ok": False, "error": failed[0]})
                resp = {"ok": True, "deleted": deleted, "failed": failed,
                        "mode": mode, "fallback": len(fellback),
                        "dropped": len(dropped)}
                if meta_failed:                      # §6.1：文件删了、元数据没跟上 ⇒ 如实带回
                    resp["meta_failed"] = meta_failed
                    resp["warning"] = ("文件已删除，但 " + "、".join(meta_failed)
                                       + " 未能写回（磁盘可能已满或被占用）")
                return self._json(resp)
            if u.path == "/api/trash_restore":
                body = self._post_body
                try:
                    key = str(json.loads(body.decode("utf-8"))["key"])
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                r = trash_restore(key)
                if r.get("ok"):
                    census_invalidate()               # 文件回到根目录，数量要重算
                    with IDX_LOCK:
                        IDX["built_at"] = 0.0         # 索引立即允许增量刷新
                return self._json(r)
            if u.path == "/api/trash_purge":
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8")) if body else {}
                    if not isinstance(d, dict):
                        raise ValueError
                    # P0-2：keys 必须是列表（可空=no-op）；「清空全部」必须显式 all:true。
                    # 缺 keys 字段不再等价于「删光一切」——那种写法等于把「前端漏传」当成用户意图。
                    if "keys" in d:
                        keys = d.get("keys")
                        if keys is None:
                            keys = []
                        if not isinstance(keys, list):
                            raise ValueError
                    else:
                        keys = []
                    all_ = bool(d.get("all"))
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                census_invalidate()                   # 彻底删除：文件不再存在
                return self._json(trash_purge(keys, all_=all_))
            if u.path == "/api/save_download":      # 下载到用户指定目录（设置页可配）
                # 与浏览器下载（/api/file?dl=1）并行的第二条路：用户在设置里配了
                # 「下载保存位置」就落盘到那儿（跨桌面端/web 行为一致、位置可记），
                # 没配则前端根本不会调这里。
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8")) if body else {}
                    if not isinstance(d, dict):
                        raise ValueError
                    src = Path(str(d["path"]))
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not src.is_file() or not inside_root(src):
                    return self._json({"error": "文件不存在或在根目录之外"}, 404)
                dest_root = str(load_settings().get("download_dir") or "").strip()
                if not dest_root:
                    return self._json({"error": "尚未设置下载目录"}, 400)
                destp = Path(dest_root)
                try:
                    destp.mkdir(parents=True, exist_ok=True)
                except OSError as ex:
                    return self._json({"error": f"下载目录无法创建：{ex}"}, 400)
                ext = src.suffix.lower()
                want_clean = bool(d.get("clean")) and ext in _STRIP_BY_EXT
                clean_ok, clean_why = False, ""
                try:
                    if want_clean and ext == ".mp4":
                        # B7-P1-2：大视频不再整份进内存 —— 先流式复制到临时名，
                        # 在目标文件上就地清零，抹完再按「确认与否」定最终名。
                        tmp = destp / (src.name + "." + _tmp_tag() + ".part")
                        try:
                            with open(tmp, "wb") as fh, open(src, "rb") as sf:
                                shutil.copyfileobj(sf, fh, _MP4_ZCHUNK)
                            ops, ok, why = _mp4_strip_plan(tmp)
                            if ok and ops:
                                _mp4_apply_plan(tmp, ops)
                                why = _mp4_residue_file(tmp)
                                ok = not why
                            clean_ok, clean_why = ok, (why or "")
                            name = (src.stem + "_clean" + src.suffix) if clean_ok else src.name
                            target = _move_into(destp, name, tmp)
                        except BaseException:
                            try:
                                tmp.unlink()
                            except OSError:
                                pass
                            raise
                        _copystat(src, target)
                    elif want_clean:
                        data = src.read_bytes()
                        # B5（2026-10-17）：原先 cleaned 只是「请求带了 clean 且格式
                        # 在白名单里」—— 剥离函数 fail-safe 吐回原字节时也报 true。
                        data, clean_ok, clean_why = strip_meta_checked(ext, data)
                        # 命名与浏览器下载那条路保持一致：**确认**抹过的才加 _clean 后缀
                        name = (src.stem + "_clean" + src.suffix) if clean_ok else src.name
                        # P2-5（2026-10-17）：原先 _unique_path + 写 .part + os.replace，
                        # 而 replace 会覆盖同名文件 —— 两次「另存为」同时指向一个目录时，
                        # 后到的把前一个顶掉，两边都报成功。改独占创建（与 batch_export 同款）。
                        target = _write_exclusive(destp, name, data)
                        _copystat(src, target)
                    else:
                        # 不剥离：同样走流式复制（原先 read_bytes 连裸复制都整份进内存，
                        # 一个 1GB 的 mp4 直接吃掉 1GB+）
                        target, fh = _open_exclusive(destp, src.name)
                        try:
                            with fh, open(src, "rb") as sf:
                                shutil.copyfileobj(sf, fh, _MP4_ZCHUNK)
                            _copystat(src, target)
                        except BaseException:
                            try:
                                target.unlink()
                            except OSError:
                                pass
                            raise
                except OSError as ex:
                    return self._json({"error": f"写入失败：{ex}"}, 500)
                return self._json({"ok": True, "dest": str(target), "name": target.name,
                                   "cleaned": clean_ok, "clean_error": clean_why})
            if u.path == "/api/prefs":
                # 界面偏好（目前只有删除去向）。放在服务端是因为「删到哪里」由后端执行，
                # 前端 localStorage 在别的窗口/桥接脚本里读不到，会各删各的。
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    if not isinstance(d, dict):
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                # B1-3（2026-10-09）：「值非法」与「写盘失败」必须分开报 ——
                # save_* 的 False 同时代表这两件事，此前一律报成「只能是 app 或 system」
                # /「只能是 0-3650 的整数」。数据目录不可写时用户按提示怎么改都不对，
                # 而真正的原因（磁盘写不进去）一个字都没说。改为**先校验、后写盘**。
                # B9-P1-2（2026-10-10）：再把「逐项写盘」收紧成**两趟** —— 原先一次请求里
                # 排在非法项**前面**的字段已经落盘了（实测 {"delete_mode":"system",
                # "trash_keep_days":99999} 报 400，可 delete_mode 已变成 system：用户以为
                # 这次什么都没改，删除去向却静默换掉，之后所有删除都不再进应用内回收站）。
                # 现在：第 1 趟只校验（一个字段都不落盘），全绿了第 2 趟才统一写盘。
                patch = {}
                if "delete_mode" in d:
                    dm = str(d["delete_mode"] or "").strip().lower()
                    if dm not in DELETE_MODES:
                        return self._json({"error": "delete_mode 只能是 app 或 system"}, 400)
                    patch["delete_mode"] = dm
                if "trash_keep_days" in d:
                    if not valid_keep_days(d["trash_keep_days"]):
                        return self._json({"error": "trash_keep_days 只能是 0-3650 的整数"
                                                    "（0=关闭自动清理）"}, 400)
                    patch["trash_keep_days"] = d["trash_keep_days"]
                if "nsfw_threshold" in d:
                    if not valid_nsfw_threshold(d["nsfw_threshold"]):
                        lo, hi = NSFW_THRESHOLD_RANGE
                        return self._json(
                            {"error": f"nsfw_threshold 只能是 {lo}~{hi} 之间的数"}, 400)
                    patch["nsfw_threshold"] = d["nsfw_threshold"]
                if "nsfw_model" in d:
                    if not valid_nsfw_model(str(d["nsfw_model"])):
                        return self._json({"error": "nsfw_model 不是可用档位"}, 400)
                    patch["nsfw_model"] = str(d["nsfw_model"])
                if "nsfw_enabled" in d:
                    patch["nsfw_enabled"] = bool(d["nsfw_enabled"])
                if "download_dir" in d:
                    # 空串 = 恢复「浏览器默认下载」；非空必须是绝对路径
                    # （否则后端写盘时才对不上，反馈太晚）
                    dd = str(d["download_dir"] or "").strip().strip('"')
                    if dd and not Path(dd).is_absolute():
                        return self._json({"error": "下载目录要填绝对路径（如 D:\\下载）"}, 400)
                    patch["download_dir"] = dd
                # ── 第 2 趟：校验全过，这才开始落盘（写盘失败一律 500 + 如实原因）──
                def _prefs_write_fail(what, _ex=None):
                    _ws = data_write_state()
                    return self._json(
                        {"ok": False, "data_write_error": _ws.get("error") or "",
                         "error": "%s没能写入数据目录，设置未生效（%s）；"
                                  "起始目录只读、或数据目录所在盘掉线时会这样"
                                  % (what, _ws.get("error") or "写入失败")}, 500)
                if "delete_mode" in patch and not save_delete_mode(patch["delete_mode"]):
                    return _prefs_write_fail("「删除去向」")
                if "trash_keep_days" in patch and not save_trash_keep_days(patch["trash_keep_days"]):
                    return _prefs_write_fail("「回收站保留天数」")
                if "nsfw_threshold" in patch and not save_nsfw_threshold(patch["nsfw_threshold"]):
                    return _prefs_write_fail("「NSFW 阈值」")
                if "nsfw_model" in patch and not save_nsfw_model(patch["nsfw_model"]):
                    return _prefs_write_fail("「NSFW 档位」")
                if "nsfw_enabled" in patch and not save_nsfw_enabled(patch["nsfw_enabled"]):
                    return _prefs_write_fail("「NSFW 总开关」")
                if "download_dir" in patch:
                    with _store_lock(SETTINGS_LOCK, "删除清理"):
                        s = load_settings()
                        s["download_dir"] = patch["download_dir"]
                        dd_ok = save_settings(s)
                    if not dd_ok:
                        return _prefs_write_fail("「下载目录」")
                return self._json({"ok": True, "delete_mode": load_delete_mode(),
                        "trash_keep_days": load_trash_keep_days(),
                                   "nsfw_enabled": nsfw_enabled(),
                                   "nsfw_threshold": load_nsfw_threshold(),
                                   "nsfw_model": load_nsfw_model(),
                                   "download_dir": load_settings().get("download_dir", "")})
            if u.path == "/api/reindex":           # 设置 · 数据页：丢弃索引缓存并重扫（不动原图）
                ok, why = force_reindex()
                if not ok:
                    # deleted:false 让前端能区分「没设置根目录」和「缓存删不掉」，
                    # 不再一律说「重建完成」（P2：旧实现删失败也回 ok）。
                    return self._json({"error": why, "deleted": False},
                                      400 if not STATE.get("root") else 500)
                return self._json({"ok": True, "deleted": True})
            if u.path == "/api/rescan":            # F26：强制重扫当前目录（丢缓存，不丢索引表）
                r = force_rescan(qs.get("rec", ["0"])[0] == "1")
                return self._json(r, 200 if r.get("ok") else 400)
            if u.path == "/api/cache_clear":       # 设置 · 数据页：按目标清缓存（目前只有缩略图）
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8")) if body else {}
                    if not isinstance(d, dict):
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                # B9-P2-6（2026-10-18）：缺 what 原先默认「清缩略图」—— 缺字段即执行
                # 破坏性动作（与 P0-1 的 trash_purge 同一类形态，只是后果可恢复：
                # 9700 张缩略图要全部重新生成）。「写错值」本来就有 400 兜着，
                # 现在「不写值」也一样，破坏性动作必须由调用方**明确指名**。
                if d.get("what") is None or str(d.get("what")).strip() == "":
                    return self._json({"error": "缺少 what（thumbs / solo）"}, 400)
                what = str(d.get("what")).strip().lower()
                if what == "thumbs":
                    n_th = clear_thumb_cache()
                    return self._json({"ok": True, "what": what, "removed": n_th,
                                       "failed": list(THUMB_CLEAR_FAILED)})
                if what == "solo":                 # 单图暂存副本（不动原图、不动库内文件）
                    n_solo = clear_solo_cache()
                    return self._json({"ok": True, "what": what, "removed": n_solo,
                                       "failed": list(SOLO_CLEAR_FAILED)})
                return self._json({"error": "unknown target"}, 400)
            if u.path == "/api/open_data":         # 设置 · 数据页：在资源管理器里打开数据目录
                try:
                    DATA_DIR.mkdir(parents=True, exist_ok=True)
                except OSError as ex:
                    return self._json({"error": f"{type(ex).__name__}: {ex}"}, 500)
                err = launch_with_default_app(DATA_DIR)
                if err:
                    return self._json({"error": err}, 500)
                return self._json({"ok": True})
            if u.path == "/api/datadir":           # F28 便携模式：切换数据目录（须重启生效）
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8")) if body else {}
                    if not isinstance(d, dict):
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                r = datadir_switch(d.get("mode"), bool(d.get("migrate")))
                if r.get("error"):
                    return self._json(r, 400)
                return self._json(r)
            if u.path == "/api/meta_batch":
                body = self._post_body
                try:
                    paths = json.loads(body.decode("utf-8")).get("paths") or []
                    if not isinstance(paths, list):
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                _wait_index()           # 索引就绪则整批命中内存；正在建则最多等几秒，避免逐张读文件
                return self._json(meta_batch(paths))
            if u.path == "/api/dims":
                # 只回宽高（原比例铺满布局用）。与 meta_batch 的区别：不解析、不落盘、
                # 不带模型/采样器，索引没有的路径直接缺席 —— 但绝不等索引。
                body = self._post_body
                try:
                    paths = json.loads(body.decode("utf-8")).get("paths") or []
                    if not isinstance(paths, list):
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                out = {}
                _denied = 0
                with IDX_LOCK:
                    _m = IDX["map"]
                for p in paths[:600]:            # 上限：单次最多 600 条（前端只补已渲染的，
                    # 正常就几十条）。不设限时冷启动会被要求一次读一万多个文件头，
                    # 首屏之外的磁盘全被这一个请求吃掉，侧栏 / 缩略图全排队。
                    # head=True：这个接口就是给「索引还没有尺寸」的图补数的
                    # （冷启动首帧 / 刚出的新图 / webp），必须能读文件头，
                    # 否则前端补过来的路径一条也命中不了。
                    # A03（2026-11-05 审计复核）：此前**不做 root 校验** —— 传任意
                    # 绝对路径进来，后端就去 open 它的文件头，等于把「根外文件是否
                    # 存在 + 宽高」做成了探针（/api/file、/api/thumb、/api/meta 都校验，
                    # 只有这里漏）。逐条跳过而非整批 400：这是个批量补数接口，
                    # 前端一批几十条，不该因为一条脏路径让整屏失去尺寸。
                    try:
                        if not inside_root(Path(str(p))):
                            _denied += 1
                            continue
                    except (OSError, ValueError):
                        _denied += 1
                        continue
                    w, h = dims_of(p, _m, head=True)
                    if w and h:
                        out[str(p)] = [w, h]
                if _denied:
                    log("[dims] 跳过根外/非法路径 %d 条" % _denied)
                return self._json(out)
            if u.path == "/api/hidden":           # 隐藏文件夹：add / remove / clear
                # 它改的是**本机图库的可见范围**（服务端行为），远程只读模式下不许动。
                if self._remote_readonly():
                    return self._json({"error": "远程只读模式下不能修改隐藏名单"}, 403)
                try:
                    d = json.loads(self._post_body.decode("utf-8"))
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not isinstance(d, dict):
                    return self._json({"error": "bad request"}, 400)
                op = str(d.get("op") or "").strip().lower()
                if op == "add":
                    r = hidden_add(str(d.get("path") or ""))
                elif op == "remove":
                    r = hidden_remove(str(d.get("path") or ""))
                elif op == "clear":
                    r = hidden_clear()
                else:
                    return self._json({"error": "unknown op"}, 400)
                # 只有真改了才重扫：重复隐藏 / 移除一条本来就不在名单里的目录，
                # 不该触发一次全库遍历。
                r["reindex"] = _hidden_reindex() if (r.get("ok") and r.get("changed")) else False
                return self._json(r)
            if u.path == "/api/fav":
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                op = d.get("op")
                # B9-P2-3（2026-10-18）：未知 op 原先**静默落到 toggle_fav** ——
                # 调用方写 `{"op":"remove"}`（想取消）实际做的是「切换」，
                # 本来已收藏的会被取消、本来没收藏的会被加上，方向完全相反且无信号。
                # 与 /api/tags 的 400 口径对齐：先认枚举，再执行。
                if op not in (None, "", "toggle", "add", "remove",
                              "set_cat", "add_cat", "del_cat", "rename_cat"):
                    return self._json({"error": "unknown op"}, 400)
                if op == "add_cat":                      # 预建分类（0 条也保留）
                    cat = str(d.get("cat") or "")
                    # B9-P2-9（2026-10-18）：分类名此前不限长 —— 5000 字的分类名会原样
                    # 写进 settings.json 并把左栏分类区撑爆。空名/纯空白仍走
                    # add_preset_cat 的 ok:false（保住既有幂等语义），超限单独给 400。
                    if len(_norm_cat(cat)) > CAT_NAME_MAX:
                        return self._json(
                            {"error": "分类名最多 %d 个字符" % CAT_NAME_MAX}, 400)
                    ok = add_preset_cat(cat)
                    return self._json({"ok": ok, "cats": fav_cats()})
                if op == "del_cat":                      # 删除分类（空分类直接删 / 带去向）
                    # 2026-11-08：新增 mode。缺省仍是 "reject"（只删空分类）——
                    # 旧的 /api/fav {"op":"del_cat"} 调用行为一字不变。
                    mode = str(d.get("mode") or "reject").strip().lower()
                    if mode not in DEL_CAT_MODES:
                        return self._json({"error": "mode 只能是 reject / move / unfav"}, 400)
                    r = delete_fav_cat(str(d.get("cat") or ""), mode)
                    r["cats"] = fav_cats()
                    return self._json(r)
                if op == "rename_cat":                   # 分类改名（撞名即合并）
                    old = str(d.get("cat") or "")
                    new = str(d.get("to") or "")
                    # 长度在校验函数里也挡了一道；这里先给 400，与 add_cat 同口径
                    if len(_norm_cat(new)) > CAT_NAME_MAX:
                        return self._json(
                            {"error": "分类名最多 %d 个字符" % CAT_NAME_MAX}, 400)
                    r = rename_fav_cat(old, new)
                    r["cats"] = fav_cats()
                    return self._json(r)
                try:
                    target = str(d["path"])
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not target:
                    return self._json({"error": "bad request"}, 400)
                tp = Path(target).resolve()
                if not tp.is_file() or not inside_root(tp):
                    return self._json({"error": "not found or outside root"}, 404)
                # 2026-11-05 P2：分类名走**与 add_cat 同一套**校验。此前只有 add_cat
                #   挡长度，set_cat / toggle / add / remove 都是原样写盘 ⇒ 5000 字分类名
                #   或含 `\n` 的分类名直接进 favs.json 并撑爆左栏。
                cat = _norm_cat(str(d.get("cat") or ""))
                if len(cat) > CAT_NAME_MAX:
                    return self._json(
                        {"error": "分类名最多 %d 个字符" % CAT_NAME_MAX}, 400)
                if any(ord(ch) < 32 or ord(ch) == 127 for ch in cat):
                    return self._json({"error": "分类名不能包含换行等控制字符"}, 400)
                cat = cat or "默认"
                if op == "set_cat":
                    r = set_fav_cat(str(Path(target).resolve()), cat)
                    if r is None:
                        return self._json({"error": "未收藏该文件"}, 404)
                    return self._json({"ok": True, "fav": True, "favcat": cat})
                # B9-P2-3：add / remove 走**显式语义**，其余（None/""/toggle）才是切换。
                # 白名单已在上面挡住未知 op —— 否则「写错 op」会被当成 toggle，
                # 「写对 op」又等于 toggle，等于这条修复白做。
                if op in ("add", "remove"):
                    fav = set_fav(str(tp), cat, op == "add")
                else:
                    fav = toggle_fav(str(tp), cat)
                favs = load_favs()
                cat = favs.get(str(Path(target).resolve()), {}).get("cat")
                return self._json({"ok": True, "fav": fav, "favcat": cat})
            if u.path == "/api/recent":             # 记录最近使用（前端在预览/下载/复制/打开时调用）
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not isinstance(d, dict):
                    return self._json({"error": "bad request"}, 400)
                op = str(d.get("op") or "").strip().lower()
                if op == "clear":
                    return self._json(recent_clear())
                # 空串必须先挡住：`Path('').resolve()` 等于进程 cwd（源码模式是工程目录），
                # 是个真实存在的路径 —— 旧代码会当合法路径一路走下去，报出与用户输入
                # 毫无关系的 404 / removed=false。与 /api/root 的「路径为空」同口径。
                raw = d.get("path")
                if not isinstance(raw, str) or not raw.strip():
                    return self._json({"error": "bad request"}, 400)
                try:
                    target = str(Path(raw).resolve())
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if op == "remove":
                    # 只摘记录、不碰文件 ⇒ 不做 is_file / inside_root 校验（见 recent_remove）
                    return self._json(recent_remove(target))
                if not Path(target).is_file() or not inside_root(Path(target)):
                    return self._json({"error": "not found or outside root"}, 404)
                return self._json(recent_add(target, str(d.get("action") or "preview")))
            if u.path == "/api/mark":               # 已标记：设置/取消（state 为空=取消）
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                try:
                    target = str(Path(str(d["path"])).resolve())
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                state = str(d.get("state") or "")
                # ε-14（§6.8）：取消标记（state 空）此前跳过校验 —— 可对任意路径
                # 清记录。统一要求 root 内，与设置分支同口径。
                if not Path(target).is_file() or not inside_root(Path(target)):
                    return self._json({"error": "not found or outside root"}, 404)
                # B9-P2-4（2026-10-18）：非法 state 原先回 HTTP 200 + ok:false ——
                # 按状态码判成败的客户端（curl / 桥接脚本 / 未来的重构）会把它当成功。
                # 与 /api/nsfw/label、/api/tags 的 400 口径对齐。
                r = mark_set(target, state)
                return self._json(r, 200 if r.get("ok") else 400)
            if u.path == "/api/tags":
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                op = d.get("op")
                try:
                    target = str(Path(d["path"]).resolve())
                except Exception:
                    target = ""
                if op in ("add", "remove", "set"):
                    if not target:
                        return self._json({"error": "bad request"}, 400)
                    p = Path(target)
                    if not p.is_file() or not inside_root(p):
                        return self._json({"error": "not found or outside root"}, 404)
                if op == "add":
                    tag = str(d.get("tag") or "")
                    if not _norm_tag(tag):
                        return self._json({"error": "无效的标签名"}, 400)
                    cur = tags_add(target, tag)
                    return self._json({"ok": True, "tags": cur, "tags_all": tag_stats()})
                if op == "add_many":
                    # P1-18：批量贴标签走单请求，避免 N×M 次串行往返与失真计数
                    paths = d.get("paths")
                    if not isinstance(paths, list) or not paths:
                        return self._json({"error": "bad request"}, 400)
                    tags = d.get("tags")
                    if not isinstance(tags, list) or not tags:
                        return self._json({"error": "bad request"}, 400)
                    clean_p = []
                    for p in paths:
                        try:
                            rp = str(Path(str(p)).resolve())
                        except Exception:
                            continue
                        fp = Path(rp)
                        if fp.is_file() and inside_root(fp):
                            clean_p.append(rp)
                    if not clean_p:
                        return self._json({"error": "not found or outside root"}, 404)
                    r = tags_add_many(clean_p, tags)
                    # `ok` 只作成功标志；计数走 r["pairs"]（见 tags_add_many
                    # 的 docstring）。覆盖 ok 不再吃掉任何数字。
                    r["ok"] = True
                    return self._json(r)
                if op == "remove":
                    tag = str(d.get("tag") or "")
                    cur = tags_remove(target, tag)
                    return self._json({"ok": True, "tags": cur, "tags_all": tag_stats()})
                if op == "set":
                    cur = tags_set(target, d.get("tags") or [])
                    return self._json({"ok": True, "tags": cur, "tags_all": tag_stats()})
                if op == "rename":
                    r = tag_rename(str(d.get("from") or ""), str(d.get("to") or ""))
                    if not r.get("ok"):
                        return self._json(r, 400)
                    r["tags_all"] = tag_stats()
                    return self._json(r)
                if op == "delete":
                    r = tag_delete(str(d.get("tag") or ""))
                    if not r.get("ok"):
                        return self._json(r, 400)
                    r["tags_all"] = tag_stats()
                    return self._json(r)
                if op == "prune":
                    # R13（2026-11-15）：左栏「N 个标签已失效」的清理出口。
                    # 只删 tags.json 里的记录，不动任何文件；root 不可读 / 触发安全阀
                    # 一律 409 + error 文案，前端原样 toast（api() 见到 error 就抛）。
                    r = tag_prune_view()
                    if not r.get("ok"):
                        return self._json(r, 409)
                    return self._json(r)
                return self._json({"error": "unknown op"}, 400)
            if u.path == "/api/send":
                body = self._post_body
                try:
                    target = str(Path(json.loads(body.decode("utf-8"))["path"]).resolve())
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                p = Path(target)
                if not p.is_file() or not inside_root(p):
                    return self._json({"error": "not found or outside root"}, 404)
                if p.suffix.lower() != ".png":
                    return self._json({"error": "仅支持 PNG（含内嵌 prompt 的格式）"}, 400)
                raw = png_meta(p).get("prompt")
                if not raw:
                    return self._json({"error": "该 PNG 没有内嵌 prompt，无法回传"}, 400)
                try:
                    prompt = json.loads(raw)
                except ValueError:
                    return self._json({"error": "内嵌 prompt 不是合法 JSON"}, 400)
                return self._json(send_to_comfy(prompt))
            if u.path == "/api/export":
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    target = str(Path(d["path"]).resolve())
                    tpl = d.get("template") or None
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                p = Path(target)
                if not p.is_file() or not inside_root(p):
                    return self._json({"error": "not found or outside root"}, 404)
                r = export_workflow(p, tpl)
                if r.get("ok"):
                    base = Path(p).stem.rstrip("_") or Path(p).stem
                    if r["source"] == "png":
                        name = base + "_内嵌"
                    elif r["source"] == "api":
                        # API 图（只有 prompt、没有 UI 工作流）—— 与 bridge_push 的命名同口径
                        name = base + "_API工作流"
                    elif tpl:
                        t = tpl if not tpl.endswith(".json") else tpl[:-5]
                        name = base + "_" + t
                    else:
                        name = base
                    r["name"] = name + ".json"
                return self._json(r)
            if u.path == "/api/export_save":       # 导出工作流 JSON 到用户选定路径（原生另存为框）
                # 与 /api/export 的分工：/api/export 只把 JSON 回传前端（旧浏览器下载用），
                # 而桌面壳（Tauri/WebView2）没有下载事件接管 —— <a download> 点了不落盘，
                # 用户以为导出了其实磁盘上没有。这里由后端弹系统「另存为」框并写盘，
                # 桌面端 / web 行为一致，保存位置也可控。
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    target = str(Path(d["path"]).resolve())
                    tpl = d.get("template") or None
                    explicit = str(d.get("dest") or "").strip()   # 给了就直写（自动化测试用）
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                p = Path(target)
                if not p.is_file() or not inside_root(p):
                    return self._json({"error": "not found or outside root"}, 404)
                if explicit:                        # A02：直写通道先过目的地白名单
                    _ok_dest, _why = _export_dest_allowed(explicit)
                    if not _ok_dest:
                        log("[export_save] 拒绝 dest=%r：%s" % (explicit, _why))
                        return self._json({"ok": False, "error": _why}, 400)
                r = export_workflow(p, tpl)
                if not r.get("ok"):
                    resp = {"ok": False, "error": r.get("error", "导出失败")}
                    # 未内嵌 UI 工作流且没给模板 ⇒ 让前端弹模板选择器再重试
                    if not tpl and "模板" in str(r.get("error", "")):
                        resp["need_template"] = True
                    return self._json(resp)
                # B2 族：合成过程中「有参数没写进去」要随成功响应回传（前端 toast 提示）
                _extra = {"warnings": r["warnings"]} if r.get("warnings") else {}
                # 建议文件名（与 /api/export 同规则：内嵌加 _内嵌，API 图加 _API工作流，
                # 模板合成加 _<模板名>）
                base = Path(p).stem.rstrip("_") or Path(p).stem
                if r["source"] == "png":
                    name = base + "_内嵌"
                elif r["source"] == "api":
                    name = base + "_API工作流"
                elif tpl:
                    t = tpl if not tpl.endswith(".json") else tpl[:-5]
                    name = base + "_" + t
                else:
                    name = base
                name += ".json"
                if explicit:
                    dest = explicit
                else:
                    # 默认落在原图所在目录（导出到图片旁边，最好找）
                    dest = _ask_save_path(name, str(p.parent))
                    if dest is None:                  # 弹不出框（非 Windows / 调用失败）⇒ 自动落盘
                        # P2-5（2026-10-17）：自动取名这条路并发时会与别的导出撞名，
                        # 独占创建后谁也不覆盖谁；显式路径仍按用户意图覆盖（保存框已确认过）
                        try:
                            dp = _write_exclusive(p.parent, name,
                                                  r["json"].encode("utf-8"))
                        except OSError as ex:
                            return self._json({"ok": False, "error": f"写入失败：{ex}"}, 500)
                        return self._json(dict({"ok": True, "path": str(dp), "name": dp.name}, **_extra))
                    elif not dest:                    # 用户取消：不写、不报错
                        return self._json({"ok": False, "canceled": True})
                dp = Path(dest)
                try:
                    # 先写 .part 再原子换名：中途失败不留半截文件（与 save_download 同款）
                    tmp = dp.with_name(f"{dp.name}.{_tmp_tag()}.part")
                    tmp.write_text(r["json"], encoding="utf-8")
                    os.replace(tmp, dp)
                except OSError as ex:
                    return self._json({"ok": False, "error": f"写入失败：{ex}"}, 500)
                return self._json(dict({"ok": True, "path": str(dp), "name": dp.name}, **_extra))
            if u.path == "/api/open_in_comfy":
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    target = str(Path(d["path"]).resolve())
                    tpl = d.get("template") or None
                    mode = str(d.get("mode") or "window")
                    port = str(d.get("port") or "")
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                p = Path(target)
                if not p.is_file() or not inside_root(p):
                    return self._json({"error": "not found or outside root"}, 404)
                if mode == "pull":       # 送给你已经开着的那个 ComfyUI 页面（无需新窗口）
                    return self._json(bridge_push(p, tpl, port))
                r = export_workflow(p, tpl)
                if not r.get("ok"):
                    return self._json(r)
                _res = open_comfy_page(
                    json.loads(r["json"]),
                    name=((p.stem.rstrip("_") or p.stem) + "_API工作流.json"
                          if r.get("source") == "api" else ""))
                if isinstance(_res, dict) and r.get("warnings"):
                    _res.setdefault("warnings", []).extend(r["warnings"])
                return self._json(_res)
            if u.path == "/api/bridge/install":     # 把桥接脚本装进 ComfyUI custom_nodes
                return self._json(bridge_install())
            if u.path == "/api/root/migrate":       # 换根迁移：设置页手动兜底（同步执行）
                # 自动迁移靠指纹命中率判断「是不是搬家」，目录结构变过的场景它不敢动；
                # 这里用户显式指定旧根 —— 人说迁就迁，跳过门槛，逐条验证照做。
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    old_root = str((d or {}).get("old_root") or "").strip()
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not old_root or not os.path.isdir(old_root):
                    return self._json({"error": "旧目录不存在或不可读"}, 400)
                root = STATE.get("root")
                if not root:
                    return self._json({"error": "尚未设置根目录"}, 400)
                # P3（V2.12.1）：新旧同路径是请求本身的问题，路由层直接 400，
                # 不必进 _migrate_run 兜一圈再拿 500 表达（审计探针 S4 记录在案）。
                if os.path.normcase(os.path.normpath(old_root)) == \
                        os.path.normcase(os.path.normpath(root)):
                    return self._json({"error": "新旧路径相同，无需迁移"}, 400)
                # P29（V2.12.1）：手动迁移与自动迁移共用 running 守卫 —— 两路并发
                # 改同一批表，后写赢前写，谁落盘的都可能是半成品。
                with _MIGRATE_LOCK:
                    if _MIGRATE_STATE["running"]:
                        return self._json({"error": "迁移正在进行中，请稍后再试"}, 409)
                    _MIGRATE_STATE["running"] = True
                try:
                    r = _migrate_run(root, old_root=old_root, manual=True)
                finally:
                    with _MIGRATE_LOCK:
                        _MIGRATE_STATE["running"] = False
                if r.get("ok") and r.get("migrated"):
                    log(f"[migrate] 手动迁移完成: {r}")
                    # P13 同款：设置页同步等到了结果，列表也得立刻见到分数
                    with NSFW_LOCK:
                        _building = bool(NSFW["building"])
                    if not _building:
                        nsfw_prime(root)
                return self._json(r, 200 if r.get("ok") else 500)
            if u.path == "/api/nsfw/scan":          # 内容安全：开始检测 / 重新检测
                # force=1 → 先丢派生缓存重扫（人工纠正表不动）；缺省做增量，秒级完成。
                # 参数走查询串而不是 body：这个按钮不该因为一个 Content-Type 就被 403 掉。
                r = nsfw_start(force=qs.get("force", ["0"])[0] == "1")
                return self._json(r, 200 if r.get("ok") else 400)
            if u.path == "/api/nsfw/label":         # 内容安全：人工纠正单张判定
                # 模型误判是必然的（插画/画风/构图都能骗过它），所以人工覆盖不是
                # 可选装饰，而是这个功能能不能长期用下去的关键 —— 纠正过就是纠正过。
                body = self._post_body
                try:
                    d = json.loads(body.decode("utf-8"))
                    if not isinstance(d, dict):
                        raise ValueError
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                cv = d.get("clear")
                # B9-P2-1（2026-10-18）：原先 `if d.get("clear")` 是真值判定 ——
                # `{"clear":"false"}` / `{"clear":"no"}` 这些**非空字符串**在 Python 里
                # 都是真 ⇒ 直接清空整张人工纠正表（用户资产，改一条都要重标）。
                # 现在只有字面 `true` 才清；其余非布尔值一律 400，不再替调用方猜意图。
                if cv is not None and not isinstance(cv, bool):
                    return self._json({"error": "clear 只能是 true / false"}, 400)
                if cv is True:                      # 清空全部人工纠正（设置页）
                    return self._json(nsfw_manual_clear())
                try:
                    p = str(Path(d["path"]).resolve())
                    lab = d.get("label")
                    if lab is not None:
                        lab = str(lab).strip().lower()
                        if lab == "":
                            lab = None
                except Exception:
                    return self._json({"error": "bad request"}, 400)
                if not p:
                    return self._json({"error": "bad request"}, 400)
                # P0-3（2026-10-08 全代码审计）：本端点原先是域内**唯一**不做路径校验的
                # 路径型写端点 —— 任意绝对路径都能被写进「人工纠正表」（键污染），
                # 与 recycle_bin_delete / 各 strip 端点相比明显缺一道闸。
                # 纠正表的键本就必须是根内的一个真实文件，多校验不误伤：
                # 越界 / 目录 / 不存在的路径一律 400，绝不落盘。
                if not inside_root(Path(p)):
                    return self._json({"error": "路径在根目录之外，拒绝写入纠正表"}, 400)
                if not os.path.isfile(p):
                    return self._json({"error": "文件不存在，拒绝写入纠正表"}, 400)
                r = set_nsfw_manual(p, lab)
                return self._json(r, 200 if r.get("ok") else 400)
            return self._json({"error": "unknown endpoint"}, 404)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # ε-14（§6.8）：补 ConnectionAbortedError —— 客户端（下载器/杀软中间人）
            # 强杀连接时 Windows 抛的是它而不是 ConnectionReset，漏捕会刷 ERROR 日志。
            pass
        except DataLockTimeout as ex:
            # 第 24 级：跨进程数据锁超时（另一实例长时间持有）。
            # 明确回 409 + 可读文案 —— 绝不静默丢更新，也不让它落进 500 脱敏兜底。
            return self._json({"error": str(ex)}, 409)
        except Exception as ex:
            # §6.1：服务端要留痕；B8-P2-6：对外**不回显**异常串（含服务器绝对路径）。
            # do_GET / do_POST 两处兜底同款 —— 统一走 _fail500，不再各写一套。
            self._fail500(ex)


class QuietServer(ThreadingHTTPServer):
    """静默处理客户端中断（浏览器刷新/取消加载属正常现象），不打印堆栈。

    4.3-12（2026-11-05 审计）：`ThreadingMixIn` 在 `daemon_threads=True` 时
    `server_close()` **不 join** 在途请求，而 restart_self 紧接着就 `os._exit(0)`
    ⇒ 正在写的请求被腰斩（写盘是 tmp+replace，不会留半截文件，但请求方看到断连）。
    这里自己记一个在途计数，重启时给它一个短宽限。
    """
    _active = 0
    _active_lock = threading.Lock()

    def process_request(self, request, client_address):
        with self._active_lock:
            self._active += 1
        try:
            super().process_request(request, client_address)
        except Exception:
            with self._active_lock:          # 线程没起成：立刻把账减回去
                self._active -= 1
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._active_lock:
                self._active -= 1

    def handle_error(self, request, client_address):
        pass


# ---------------------------------------------------------------- 设置面板支撑（V1 批次 2）


# 批次 F · F5：目录占用 TTL 短缓存。
# `sysinfo()` 一次要算三个目录，其中缩略图目录实测 **12956 个文件**（单目录就 120 ms），
# 而设置面板每打开一次都全量遍历一次。界面显示的是「占用快照」，秒级滞后无感。
# **清除 / GC 之后必须主动失效**（`_dir_usage_invalidate`），否则「点完数字不变」
# 会被用户当成「没清成功」—— 这是本缓存唯一需要人工布线的地方。
_DIR_USAGE = {}
_DIR_USAGE_LOCK = threading.Lock()
DIR_USAGE_TTL = 30.0


def _dir_usage_invalidate(d: Path = None):
    """失效某个目录（不传则全部）的占用缓存。删 / 清缓存后必须调。"""
    with _DIR_USAGE_LOCK:
        if d is None:
            _DIR_USAGE.clear()
        else:
            _DIR_USAGE.pop(str(d), None)


def _dir_usage(d: Path) -> dict:
    """目录占用：文件数与总字节。这几个目录都是平铺的，不递归子目录。

    批次 F（F5）：带 `DIR_USAGE_TTL` 短缓存，理由见上方注释。
    """
    k = str(d)
    now = time.time()
    with _DIR_USAGE_LOCK:
        hit = _DIR_USAGE.get(k)
        if hit is not None and now - hit[0] < DIR_USAGE_TTL:
            return dict(hit[1])
    n, b = 0, 0
    try:
        if d.is_dir():
            for f in d.iterdir():
                try:
                    if f.is_file():
                        n += 1
                        b += f.stat().st_size
                except OSError:
                    continue
    except OSError:
        pass
    res = {"count": n, "bytes": b}
    with _DIR_USAGE_LOCK:
        _DIR_USAGE[k] = (now, res)
    return dict(res)


# 迁移清单：CMV 自己的数据。edge-profile 刻意不在列 —— 那是浏览器配置（可再生、体积大），
# 搬到新位置只会让「切换」变得又慢又占地方。
DATA_FILES = ("settings.json", "favorites.json", "tags.json",
              "recent.json", "marks.json", "prompt_index.json")
DATA_SUBDIRS = ("thumbs", "trash")


def datadir_for(mode: str) -> Path:
    """某个模式对应的标准落点（不代表当前已生效的那个）。

    ε-13（§6.6）：便携模式走 `_portable_flag_dir()` —— 与 `_resolve_data_dir()`
    同一套标记 JSON 解析，口径不再分叉（详见那边注释）。
    """
    if mode == "portable":
        if PORTABLE_FLAG.is_file():
            return _portable_flag_dir() or (_exe_dir() / PORTABLE_DATA_NAME)
        return _exe_dir() / PORTABLE_DATA_NAME
    return _user_data_dir()


def _migrate_data(src: Path, dst: Path, conflict_backup: bool = True) -> dict:
    """把源数据目录里 CMV 的条目复制到目标。逐条捕异常。

    `conflict_backup=False`（2026-11-05，A07 补同步专用）：目标端同名但内容不同时
    **直接覆盖，不生成 `.cmv-conflict-*` 备份**。理由：补同步写的是本轮迁移在
    收尾时追上的最新版本，目标端那份是本轮自己刚拷过去的中间态、不是「用户既有数据」；
    再备份一遍只会让新目录里堆一串 `favorites.json.cmv-conflict-<ts>` 垃圾，
    而真正需要保住的旧数据在**第一次**迁移时已经备份过了。

    ε-11（§6.4）：同名文件分两种情形，不再一律静默跳过 ——
      · 大小 + mtime 都一致 ⇒ 视为同一份，跳过（来回切换幂等、不产生垃圾文件）；
      · 内容不同 ⇒ 目标端旧文件先改名备份为 `<名>.cmv-conflict-<ts>` 再写入源端这份。
        「不覆盖用户数据」的底线不变（P0-2）：旧数据改名保留、可见可恢复，
        而不是像以前那样原地跳过 —— 来回切换后新目录里跑的还是旧数据，观感等同丢数据。
    逐条捕异常的理由同批量删除 —— 一个文件坏掉不该让整次迁移失败。

    ⚠️ P0-2：失败**必须上报**。「逐条捕异常」+「返回值里没有失败数」这个组合踩过一个坑：
      exFAT/满盘/被占用导致 copy2 失败时，后端只 log 一句（无控制台 exe 下这句直接丢弃），
      返回值只有 {files, skipped} ⇒ 前端看到 files>0 就弹「已切换…复制 N 个文件」。
      重启后发现 favorites.json / settings.json 没过去，表现就是「数据全丢了」，
      而真身还躺在旧目录 —— 用户不会回去找。所以这里显式累计 failed 与失败清单。
    """
    n_f = n_skip = n_b = n_conf = 0
    failed = []
    _ts = str(int(time.time()))

    def _one(s: Path, d: Path):
        nonlocal n_f, n_skip, n_b, n_conf
        if d.exists():
            try:
                ss, ds = s.stat(), d.stat()
                if ds.st_size == ss.st_size:
                    # ε-11：判「同一份」小文件（各 JSON，≤1MB）直接比内容 ——
                    # 同名不同内容常是同秒写入，size+mtime 会误判成相同而静默跳过；
                    # 大文件（缩略图缓存等）退回 mtime 口径（内容比对太贵，且缓存可再生）。
                    if ss.st_size <= (1 << 20):
                        with open(str(s), "rb") as _a, open(str(d), "rb") as _b:
                            same = _a.read() == _b.read()
                    else:
                        same = int(ds.st_mtime) == int(ss.st_mtime)
                    if same:
                        n_skip += 1                    # 同一份内容：跳过（幂等）
                        return
            except OSError:
                pass
            if conflict_backup:
                bak = d.with_name(d.name + ".cmv-conflict-" + _ts)
                try:
                    d.rename(bak)                  # 备份目标端旧文件，绝不覆盖
                    n_conf += 1
                except OSError as ex:
                    failed.append({"name": d.name,
                                   "error": f"同名旧文件备份失败 {type(ex).__name__}: {ex}"})
                    log(f"[warn] 迁移备份 {d} 失败: {ex}")
                    return
        try:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(s), str(d))
            n_f += 1
            n_b += d.stat().st_size
        except OSError as ex:
            failed.append({"name": d.name,
                           "error": f"{type(ex).__name__}: {ex}"})
            log(f"[warn] 迁移 {s} 失败: {ex}")

    for name in DATA_FILES:
        s = src / name
        if s.is_file():
            _one(s, dst / name)
    for name in DATA_SUBDIRS:
        s = src / name
        if not s.is_dir():
            continue
        try:
            for f in s.iterdir():
                if f.is_file():
                    _one(f, dst / name / f.name)
        except OSError as ex:
            failed.append({"name": name, "error": f"{type(ex).__name__}: {ex}"})
            log(f"[warn] 遍历 {s} 失败: {ex}")
    return {"files": n_f, "skipped": n_skip, "bytes": n_b,
            "conflicts": n_conf,
            "failed": len(failed), "failed_items": failed[:20],
            "dirs": len([x for x in DATA_SUBDIRS if (dst / x).is_dir()])}


def datadir_switch(mode: str, migrate: bool) -> dict:
    """切换数据目录并落盘标记（F28）。返回给前端的摘要。

    只写标记、不改内存里的 DATA_DIR —— 那 8 个常量在启动时算好，运行中改不了，
    所以如实返回 restart_required，由前端引导重启。宁可明说，也不要让用户
    「切了但当前会话还是老目录」却毫无提示。
    """
    mode = str(mode or "").strip().lower()
    if mode not in ("user", "portable"):
        return {"error": "mode 只能是 user 或 portable"}
    src, dst = DATA_DIR, datadir_for(mode)
    try:
        same = dst.resolve() == src.resolve()
    except OSError:
        same = str(dst) == str(src)
    if same:
        return {"ok": True, "same": True, "mode": mode, "dir": str(dst),
                "restart_required": False,
                "migrated": {"files": 0, "skipped": 0, "bytes": 0,
                             "failed": 0, "failed_items": [], "dirs": 0}}
    # 可写性先探一下：便携模式落在程序目录，装在 Program Files 下会失败，
    # 这时候要给出可读的原因，而不是留一个半途而废的状态。
    try:
        dst.mkdir(parents=True, exist_ok=True)
        probe = dst / ".cmv-write-test"
        probe.write_text("x", "utf-8")
        probe.unlink()
    except OSError as ex:
        return {"error": f"目标目录不可写：{dst}（{type(ex).__name__}: {ex}）"}

    if migrate:
        # ε-12（§6.3）：迁移挪后台线程 —— 立即返回 migrating，前端轮询
        # /api/datadir_status。便携标记必须等迁移**完成后**才写（在
        # _datadir_finalize 里），否则用户中途重启会拿到一份没搬完的新目录。
        with _DD_MIG_LOCK:
            if _DD_MIG["state"] == "running":
                return {"error": "已有一次数据目录迁移在进行中，请等它完成", "busy": True}
            _DD_MIG["state"] = "running"
            _DD_MIG["result"] = None
            _DD_MIG["mode"] = mode

        def _job():
            try:
                # A07：resync_src=src ⇒ 收尾时把迁移期间写进旧目录的改动补过去
                resp = _datadir_finalize(mode, src, dst, _migrate_data(src, dst),
                                         resync_src=src)
            except Exception as ex:          # 迁移线程不许静默死：异常也如实落槽
                import traceback
                log("[datadir] 迁移线程异常: " + traceback.format_exc(limit=3))
                resp = {"error": f"迁移线程异常：{type(ex).__name__}: {ex}"}
            with _DD_MIG_LOCK:
                _DD_MIG["state"] = "error" if resp.get("error") else "done"
                _DD_MIG["result"] = resp

        threading.Thread(target=_job, name="datadir-migrate", daemon=True).start()
        return {"migrating": True, "mode": mode, "dir": str(dst), "from": str(src),
                "restart_required": True}
    return _datadir_finalize(mode, src, dst,
                             {"files": 0, "skipped": 0, "bytes": 0,
                              "failed": 0, "failed_items": [], "dirs": 0})

def _datadir_finalize(mode: str, src: Path, dst: Path, copied: dict,
                      resync_src: Path = None) -> dict:
    """ε-12（§6.3）：迁移完成后的收尾 —— 写/删便携标记并构造响应。

    从 datadir_switch 拆出来：同步路径（不迁移）与后台迁移线程共用。

    A07（2026-11-05 审计复核）：`resync_src` 给了就在写标记**之前**再同步一次。
    迁移跑在后台线程（几秒到几分钟），而 `DATA_DIR` 在运行期不变 —— 期间用户
    改的收藏 / 标签 / 人工标注 / 设置全都写进**旧目录**，迁移线程早就扫过那些文件了。
    迁移完成 → 写便携标记 → 用户重启 → `DATA_DIR` 指向新目录 ⇒ 这段时间的改动
    整体消失（新目录里只有迁移开始那一刻的快照）。
    `_migrate_data` 本身是幂等的（同内容跳过、不同内容备份后覆盖），所以迁移收尾时
    再跑一遍就是天然增量同步。窗口被压到「补同步 → 写标记」之间这几毫秒。
    """
    if resync_src is not None:
        try:
            tail = _migrate_data(resync_src, dst, conflict_backup=False)
        except Exception as ex:              # 补同步失败不许把整次迁移判成失败
            tail = {"files": 0, "skipped": 0, "bytes": 0, "conflicts": 0,
                    "failed": 1, "failed_items": [{"name": "（迁移期间的增量同步）",
                                                   "error": f"{type(ex).__name__}: {ex}"}]}
        copied = {
            "files": int(copied.get("files") or 0) + int(tail.get("files") or 0),
            "skipped": int(copied.get("skipped") or 0) + int(tail.get("skipped") or 0),
            "bytes": int(copied.get("bytes") or 0) + int(tail.get("bytes") or 0),
            "conflicts": int(copied.get("conflicts") or 0) + int(tail.get("conflicts") or 0),
            "failed": int(copied.get("failed") or 0) + int(tail.get("failed") or 0),
            "failed_items": (list(copied.get("failed_items") or [])
                             + list(tail.get("failed_items") or []))[:20],
            "dirs": copied.get("dirs") or 0,
            "resynced": int(tail.get("files") or 0),   # 迁移期间新写入、本次补过去的条数
        }
    if mode == "portable":
        try:
            tmp = PORTABLE_FLAG.with_suffix(".tmp")
            tmp.write_text(json.dumps({"dir": str(dst)}, ensure_ascii=False, indent=1), "utf-8")
            tmp.replace(PORTABLE_FLAG)
        except OSError as ex:
            return {"error": f"便携标记写入失败：{PORTABLE_FLAG}（{type(ex).__name__}: {ex}）",
                    "migrated": copied}
    else:
        try:
            if PORTABLE_FLAG.exists():
                PORTABLE_FLAG.unlink()
        except OSError as ex:
            return {"error": f"便携标记删除失败：{PORTABLE_FLAG}（{type(ex).__name__}: {ex}）",
                    "migrated": copied}
    log(f"[datadir] 切换为 {mode}：{src} -> {dst}（迁移 {copied['files']} 个文件"
        f"，跳过 {copied['skipped']} 个已存在，失败 {copied.get('failed', 0)} 个）")
    # P0-2：有文件没搬过去时，别让前端报「一切正常」—— 带上 partial 让 UI 改用警告色
    if copied.get("failed"):
        names = "、".join(x["name"] for x in copied.get("failed_items", [])[:5])
        return {"ok": True, "mode": mode, "dir": str(dst), "from": str(src),
                "migrated": copied, "restart_required": True, "partial": True,
                "warning": (f"有 {copied['failed']} 个文件未能复制到新目录"
                            f"（{names}）。这些数据仍留在原目录：{src}")}
    return {"ok": True, "mode": mode, "dir": str(dst), "from": str(src),
            "migrated": copied, "restart_required": True}


# ε-12（§6.3）：datadir 后台迁移任务槽。迁移可能搬几个 G（缩略图缓存 + 索引），
# 原先在 HTTP 请求线程里同步 copy2 —— 期间该线程被独占几十秒到几分钟，
# 前端 fetch 一直挂着、服务端还少一个线程处理其它请求。改为后台线程执行，
# 前端轮询 /api/datadir_status 到 done/error。
_DD_MIG = {"state": "idle", "result": None, "mode": None}
_DD_MIG_LOCK = threading.Lock()


def datadir_status() -> dict:
    with _DD_MIG_LOCK:
        return {"state": _DD_MIG["state"], "mode": _DD_MIG["mode"],
                "result": _DD_MIG["result"]}


def sysinfo() -> dict:
    """设置面板「数据 / 关于」页的只读快照。

    刻意做成一个端点、而不是让前端拼：占用统计要 stat 磁盘，交给后端一次算完；
    顺带把「数据目录」这种只有后端知道的绝对路径一并带出去（前端没法自己算
    %LOCALAPPDATA%，桌面壳与浏览器模式下还可能不同）。
    """
    with IDX_LOCK:
        idx_n = len(IDX["map"])
        idx_root = IDX["root"]
        idx_built = IDX["built_at"]
        idx_done = int(IDX["done"])
        idx_total = int(IDX["total"])
        idx_building = bool(IDX["building"])
    # F26：目录指纹短缓存现状（扫描结果缓存 F25 尚未实现，如实报「未启用」，
    # 面板宁可显示真实状态，也不要写死一句「已启用」骗用户）
    with _SIG_LOCK:
        _sig_key, _sig_at = _SIG["key"], float(_SIG["at"])
    idx_bytes = 0
    try:
        if IDX_FILE.is_file():
            idx_bytes = IDX_FILE.stat().st_size
    except OSError:
        pass
    return {
        "ok": True,
        "version": APP_VERSION,
        # 关于页的项目入口：地址的唯一来源（前端只展示与复制，不自造）
        "links": dict(ABOUT_LINKS),
        "has_pil": HAS_PIL,
        "data_dir": str(DATA_DIR),
        "settings_file": str(SETTINGS_FILE),
        # F28 数据目录位置：mode 是**当前进程实际在用**的；pending=True 表示磁盘上的
        # 选择已经变了、但这个进程还是老目录（切换后未重启），前端据此提示「需重启生效」。
        "datadir": {
            "dir": str(DATA_DIR),
            "mode": DATA_MODE,
            "exe_dir": str(_exe_dir()),
            "flag": str(PORTABLE_FLAG),
            "flag_exists": PORTABLE_FLAG.is_file(),
            "pending": (("portable" if PORTABLE_FLAG.is_file() else "user") != DATA_MODE),
            "targets": {"user": str(_user_data_dir()),
                        "portable": str(_exe_dir() / PORTABLE_DATA_NAME)},
        },
        "thumbs": dict(_dir_usage(THUMB_DIR), dir=str(THUMB_DIR)),
        # B1-3：数据目录写入状态（与 /api/info 同源）。设置页「数据」页每次刷新都会
        # 重新读一次，用户在那里能直接看到「写不进去」的红字告警。
        "data_write": data_write_state(),
        # P1-3：回收站自动清理是可配的 —— 界面必须能回答「它会不会自己删」
        # B6-1：orphans = 目录里不在索引中的文件数（占用数字与列表对不上时，答案在这）
        "trash": dict(_dir_usage(TRASH_DIR), dir=str(TRASH_DIR),
                      keep_days=load_trash_keep_days(),
                      orphans=len(trash_orphans())),
        # 单图暂存：打开过的外部图片副本（哈希命名、上限 20 张，V2.10.7）
        "solo": dict(_dir_usage(SOLO_DIR), dir=str(SOLO_DIR)),
        "index": {"count": idx_n, "bytes": idx_bytes, "file": str(IDX_FILE),
                  "root": idx_root, "built_at": int(idx_built or 0),
                  # F26 索引进度：done/total 由 _index_builder 逐个文件更新，building 表在跑
                  "progress": {"done": idx_done, "total": idx_total,
                               "building": idx_building,
                               "pct": (int(idx_done * 100 / idx_total)
                                       if idx_total else 0)}},
        # F26 扫描缓存：result_cache=False 是**事实**而非占位 —— F25 落地后改 True
        "scan": {"result_cache": False,
                 "sig_ttl": SIG_TTL,
                 "sig_cached": _sig_key is not None,
                 "sig_path": (_sig_key[0] if _sig_key else ""),
                 "sig_recursive": (bool(_sig_key[1]) if _sig_key else False),
                 "sig_age": (int(time.time() - _sig_at) if _sig_at else -1)},
    }


THUMB_CLEAR_FAILED = []     # 上一次清缩略图时删不掉的文件名（F3-P2-4，供端点如实上报）


def clear_thumb_cache() -> int:
    """清空缩略图缓存（只删缓存目录里的文件，不动任何原图）。返回删除个数。

    F3-P2-4（2026-10-19）：删不掉的文件原来被 `except OSError: continue` 静默跳过，
    只把成功数返回给前端 ⇒ 9700 个缓存文件里有几个被占用/只读时，界面照样报
    「已清除 N 个文件」，而设置页的占用字节数不降反升，用户没有任何解释。
    现在把失败项收集到 `THUMB_CLEAR_FAILED`，/api/cache_clear 一并回给前端。
    """
    n = 0
    failed = []
    try:
        if THUMB_DIR.is_dir():
            for f in THUMB_DIR.iterdir():
                try:
                    if f.is_file():
                        f.unlink()
                        n += 1
                except OSError:
                    failed.append(f.name)
    except OSError as ex:
        log(f"[thumb] 清缓存时列目录失败：{type(ex).__name__}: {ex}")
    if failed:
        log("[thumb] 有 %d 个缩略图未能删除（被占用或只读）：%s"
            % (len(failed), "、".join(failed[:5])))
    THUMB_CLEAR_FAILED[:] = failed
    _dir_usage_invalidate(THUMB_DIR)     # F5：清完不失效 ⇒ 设置页数字不变，像没清成功
    return n


def force_reindex():
    """丢弃内存索引与磁盘缓存，重新触发一次完整扫描（不动原图）。

    「重建」就是要真的从头来：只重置 built_at 的话 _index_builder 仍会按 mtime+size
    复用旧条目、磁盘缓存也还在，用户点了会疑惑「怎么一秒就完事」。所以这里连同
    IDX_FILE 一起丢掉。代价是重建期间提示词搜索为空 —— 前端已配确认弹窗与
    底栏进度（V3 状态中枢），不是静默行为。

    返回 `(ok, 失败原因)`。2026-11-05 P2：旧实现 `IDX_FILE.unlink()` 失败被 `pass`，
    随后照常 ensure_index() —— 而 `_index_builder` 开跑时会读回那份**没删掉**的
    缓存、按 mtime+size 全部复用 ⇒ 用户点「重建」一秒「完成」，实际一条都没重解析
    （除 PARSER_VER 升级外没有别的出口）。删不掉就必须如实说，且**什么都别重置**。
    """
    root = STATE.get("root")
    if not root:
        return False, "尚未设置根目录，无法重建索引"
    with IDX_LOCK:
        IDX_GEN[0] += 1               # 先换代：在跑的旧线程自此不得再写回 IDX/IDX_FILE
        IDX_THREAD[0] = None          # 允许 ensure_index 起新线程
    try:
        if IDX_FILE.is_file():
            IDX_FILE.unlink()
    except OSError as ex:
        # 内存索引保持可用（旧线程已被换代作废，ensure_index 下次仍可重启构建）。
        return False, ("索引缓存删除失败（%s: %s）。该文件可能正被其他程序或"
                       "另一个 CMV 实例占用，请关闭后重试。" % (type(ex).__name__, ex))
    with IDX_LOCK:
        IDX["map"] = {}
        IDX["done"] = 0
        IDX["total"] = 0
        IDX["built_at"] = 0.0
    census_invalidate()        # B7：重建索引同样意味着「用户认为图库变了」⇒ 目录数一并重算
    ensure_index()
    return True, ""


def force_rescan(recursive: bool = False) -> dict:
    """强制重扫当前目录：给「目录明明变了、界面却没动」一个立刻生效的出口。

    要一并丢掉两处缓存，缺一个都会表现成「点了没反应」：
      ① 目录指纹短缓存 `_SIG` —— 否则 SIG_TTL(1.5s) 内拿到的还是旧指纹，
         前端自动刷新比对后认为「没变化」，把这次点击整个吞掉；
      ② 索引增量节流位 `IDX["built_at"]` —— 否则 ensure_index 在 REINDEX_INTERVAL(30s)
         窗口内直接 return，刚出的图仍然搜不到（这正是 F25 缓存最容易踩的坑，先备好阀）。
    第三处在前端：`lastSig` 得由调用方重载目录时覆盖，服务端管不着。

    刻意**不动** `IDX["map"]` 与 `IDX_FILE` —— 那是「重建索引」的职责，两者代价差一个
    数量级：重扫只重新走一趟目录算个新指纹，重建要把每张图重新解析一遍。
    """
    root = STATE.get("root")
    if not root:
        return {"ok": False, "error": "尚未设置根目录，无法重扫"}
    with _SIG_LOCK:
        _SIG.update({"key": None, "at": 0.0, "sig": ""})
    with IDX_LOCK:
        IDX["built_at"] = 0.0                 # 下一趟 ensure_index 立即做增量校验
    # B7（2026-10-17）：这是「目录明明变了、界面却没动」的人工出口，`_CENSUS` 若还在
    # 45s TTL 内，左栏总数照样最长 45 秒不变 —— 用户点完看到数字没动，会认为按钮失灵。
    census_invalidate()
    p = Path(root)
    sig = dir_signature(p, recursive)
    n, exts = 0, (IMAGE_EXTS | VIDEO_EXTS)
    try:
        with os.scandir(p) as it:             # 只数当层媒体文件，递归计数由指纹负责
            for e in it:
                try:
                    if e.is_file() and os.path.splitext(e.name)[1].lower() in exts:
                        n += 1
                except OSError:
                    continue
    except OSError:
        pass
    return {"ok": True, "root": root, "sig": sig, "files": n,
            "recursive": bool(recursive)}


def _bg_guard(name, fn, *args):
    """后台线程的统一兜底（B6-P2-3）。

    `threading.Thread(target=trash_gc)` 这类写法一旦函数抛异常，线程就**静默死亡**：
    控制台留一段 traceback，界面上什么都不会变 —— 自动清理整体失效，回收站
    无限增长，用户只会觉得「占用数字怎么一直涨」。这里至少把它写进日志。
    """
    try:
        return fn(*args)
    except Exception as ex:
        log(f"[warn] 后台任务 {name} 异常退出: {type(ex).__name__}: {ex}")
        return None


def _gc_data_tmp(min_age: float = 300.0) -> int:
    """清数据目录顶层残留的 `*.tmp`（B4-P2-3）。

    为什么需要：`_save_idx_cache` 写的是 20MB 级 JSON，进程被杀/断电时 `tmp.replace`
    可能根本没执行 —— 只留下半截 tmp。此前只有 `THUMB_DIR` 有 GC，数据目录没有，
    于是一个 20MB 的 `prompt_index.<pid>.<tid>.tmp` 会永久躺在那里，还会被
    「数据目录占用」算进文件数。

    ⚠️ 必须带 `min_age`：同一台机器上可能还有另一个实例正在写自己的 tmp
    （`_tmp_tag()` 带 PID 所以不撞名，但会同时存在）。只清「5 分钟没动过」的，
    正在写的那份 mtime 一直在更新。
    """
    n = 0
    now = time.time()
    try:
        files = list(DATA_DIR.glob("*.tmp"))
    except OSError:
        return 0
    for f in files:
        try:
            if now - f.stat().st_mtime < min_age:
                continue
            f.unlink()
            n += 1
        except OSError:
            continue
    if n:
        log(f"[gc] 清理数据目录残留临时文件 {n} 个（崩溃/强杀留下的半截写盘）")
    return n


def gc_thumbs(max_mb: int = 512, keep_days: int = 30):
    """缩略图缓存 GC：清残留 .tmp、删超龄条目，总量超限按最旧优先裁剪。"""
    try:
        if not THUMB_DIR.is_dir():
            return
        for f in THUMB_DIR.glob("*.tmp"):
            try:
                f.unlink()
            except OSError:
                pass
        infos, total = [], 0
        for f in THUMB_DIR.glob("*.jpg"):
            try:
                st = f.stat()
            except OSError:
                continue
            infos.append((st.st_mtime, st.st_size, f))
            total += st.st_size
        cutoff = time.time() - keep_days * 86400
        for mt, sz, f in infos:
            if mt < cutoff:
                try:
                    f.unlink()
                    total -= sz
                except OSError:
                    pass
        limit = max_mb * 1024 * 1024
        if total > limit:
            for mt, sz, f in sorted(infos):
                if total <= limit:
                    break
                try:
                    f.unlink()
                    total -= sz
                except OSError:
                    pass
    except OSError:
        pass
    finally:
        _dir_usage_invalidate(THUMB_DIR)     # F5：GC 可能删了东西，占用数字要重算


THUMB_GC_HOURS = 6.0        # 缩略图 GC 周期（小时）；改配置项前先想清楚「谁来叫醒它」


def thumb_gc_loop(interval: float = THUMB_GC_HOURS * 3600.0):
    """P6（2026-11-15）：缩略图 GC 改成定期跑。

    旧写法只在启动时调一次 `gc_thumbs()`。对「开着不关」的用法（看图看一整天、
    或桌面版常驻托盘）等于**从不清理**：`max_mb` 上限只在下次重启时才生效，
    缩略图目录能一路涨到几个 GB，用户只能靠重启让它回落。

    现在启动清一次，之后每 6 小时一次。间隔取 6 小时的理由：单张缩略图几十 KB，
    512MB 上限对应上万张，正常使用每小时新增远小于这个量级；太频繁反而每次都要
    遍历整个目录（上万次 stat）造成无谓 IO。
    """
    while True:
        _bg_guard("thumb_gc", gc_thumbs)
        time.sleep(interval)


# ── 落盘日志（P1-39）────────────────────────────────────────────────────
# 打包成无控制台 exe 时 sys.stdout is None，log() 完全静默 ⇒ 「用户看到白屏，
# 磁盘上零痕迹」，排障只能靠猜。这里同时写一份到 LOG_FILE，并装 excepthook，
# 让未捕获异常也留痕。日志失败**绝不能**影响主流程（所有写入都吞异常）。
LOG_LOCK = threading.Lock()
LOG_FILE = None              # 由 init_logging() 定，未初始化时只 print
_STDOUT_IS_SINK = [False]    # True = stdout 已换成 _StdoutLogSink（无控制台 exe）


def log_path() -> Path:
    """日志落点：exe 同目录的 cmv-data\\logs\\cmv.log（与数据目录同处，便携）。"""
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
    return base / "cmv-data" / "logs" / "cmv.log"


# ---- stdout 日志的非阻塞出口（B8-P2-8，2026-10-18）-----------------------
# 由来：此前 log() 直接 `print(..., flush=True)`。只要拉起后端的父进程（桌面壳 /
#   构建脚本 / 任何 stdout=PIPE 却不读的启动器）不消费 stdout，管道写满 64KB 后
#   **每一条会记日志的请求**都卡死在 print 里 —— 而 2xx 不记日志，所以现场表现是
#   「正常浏览毫无症状，一遇到缺图/404 就永远转圈」，占用的是 ThreadingHTTPServer
#   的工作线程，攒够就整个界面「点什么都没反应」。
# 现在 stdout 走有界队列 + 独立写线程：队列满 ⇒ 丢弃并计数，主流程永不等 stdout。
# ⚠️ 文件日志仍是**同步**写 —— 文件写不会阻塞在管道上，且它才是排障的权威副本。
_LOG_Q = queue.Queue(maxsize=4000)
_LOG_Q_DROP = [0]


def _log_pump():
    """唯一的 stdout 写入者。阻塞只发生在这里，不拖累任何请求线程。"""
    while True:
        line = _LOG_Q.get()
        try:
            if line is None:
                return
            print(line, flush=True)
        except Exception:          # stdout 被关 / 无控制台 exe 里为 None
            pass
        finally:
            try:
                _LOG_Q.task_done()
            except Exception:
                pass


_LOG_THREAD = threading.Thread(target=_log_pump, name="cmv-log-stdout", daemon=True)
_LOG_THREAD.start()


def _log_stdout(line: str):
    """把一行日志投进 stdout 队列；投不进去只计数，绝不阻塞调用方。"""
    if _STDOUT_IS_SINK[0]:
        return          # 无控制台：stdout 兜底类会原样落盘，再排一份就是双份
    if _LOG_Q_DROP[0]:
        n = _LOG_Q_DROP[0]
        _LOG_Q_DROP[0] = 0
        line = "%s\n[log] 另有 %d 条日志因 stdout 阻塞未输出（文件日志完整）" % (line, n)
    try:
        _LOG_Q.put_nowait(line)
    except queue.Full:
        _LOG_Q_DROP[0] += 1


# ---- 日志里的请求权脱敏（B8-P2-7，2026-10-18）----------------------------
# LAN 模式的访问令牌走 `?token=xxx`，而 log_message 记的是**原始请求行**。
# 异常兜底早就 `split("?")[0]` 剥掉了，主路径（log_message）却没剥 ⇒ 4xx/5xx 一落
# 日志，令牌就明文写进 cmv.log 与轮转备份 .log.1。这里补上主路径，两处同款。
_REQ_QUERY_RE = re.compile(r"\?[^\"\s]*")


def _strip_query_for_log(msg: str) -> str:
    """剥 query（令牌）＋把裸 CR 转义（4.3-14，2026-11-05 审计）。

    请求行由 `readline` 按 `\\n` 切，行尾可以留一个裸 `\\r`（raw socket 可构造）。
    直接写进 cmv.log 后，部分查看器把 `\\r` 当换行 ⇒ 能伪造一条日志条目。
    转成可见的 `\\\\r`：既保留取证信息，又不会被当成换行。
    """
    return (_REQ_QUERY_RE.sub("", msg or "")).replace("\r", "\\r")


def _append_log_file(line: str):
    """把一行（已带时间戳）追加进日志文件，带 2MB 单文件轮转。

    抽出来给两处用：`log()` 与无控制台环境的 stdout 兜底类。
    调用方必须**自己在 LOG_LOCK 外**做好「这一行不是从泵线程回来的」保证
    （见 `_StdoutLogSink` 的注释）—— 本函数不加重入闸，避免把锁语义搞混。
    """
    if LOG_FILE is None:
        return
    try:
        with LOG_LOCK:
            # 单文件上限 2MB：超了轮转成 .1（只保留一代，避免无限增长）
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > 2 * 1024 * 1024:
                bak = LOG_FILE.with_suffix(".log.1")
                try:
                    if bak.exists():
                        bak.unlink()
                    LOG_FILE.rename(bak)
                except Exception:
                    # 4.3-13（2026-11-05 审计）：原先裸 pass ⇒ 改名被别的进程占着时
                    # 日志会一直涨到把盘写满。退化成「只留最后 1MB」的就地截断 ——
                    # 丢的是最老的日志，总比把用户磁盘吃光强。
                    try:
                        with io.open(LOG_FILE, "rb") as _r:
                            _r.seek(-1024 * 1024, os.SEEK_END)
                            _keep = _r.read()
                        with io.open(LOG_FILE, "wb") as _w:
                            _w.write(_keep)
                    except Exception:
                        pass
            with io.open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass                 # 日志永远不能反过来弄崩主流程


# 重入闸：标记「当前线程正在把一行日志送往 stdout」。泵线程 print → stdout 兜底类
# → log() 这条链上，第二道 log() 不再回流 stdout，从机制上掐断回声环
# （2026-10-20 用户报障「test8 启动特别缓慢」的根因，见 _StdoutLogSink 注释）。
_LOG_TO_STDOUT = threading.local()


def log(msg: str):
    line = "%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    if not getattr(_LOG_TO_STDOUT, "busy", False):
        _LOG_TO_STDOUT.busy = True
        try:
            _log_stdout(line)
        finally:
            _LOG_TO_STDOUT.busy = False
    _append_log_file(line)


def _excepthook(etype, value, tb):
    """未捕获异常落盘（P1-39）。默认钩子是往 stderr 打，无控制台时全丢。"""
    try:
        txt = "".join(traceback.format_exception(etype, value, tb))
    except Exception:
        txt = "%s: %s" % (etype, value)
    log("[FATAL] 未捕获异常：\n" + txt)
    try:
        sys.__excepthook__(etype, value, tb)
    except Exception:
        pass


class _StdoutLogSink:
    """无控制台 exe（PyInstaller windowed）的 stdout/stderr 兜底。

    ⚠️ 2026-10-20 血案：这里原来直接 `log(s)`。而 `log()` 会经 `_log_stdout()`
    把同一行投进队列，泵线程 `print(line)` 又写回本类 ⇒ **日志回声环**：
    每次 log() 自我放大一次，实测 **1 秒 149 KB / 单行 1415 个时间戳**，
    且长期占着 `LOG_LOCK` 与磁盘（真实现场：一次启动写出 2.1 MB 轮转日志，
    后端启动全程被拖慢，用户体感「test8 启动特别缓慢」）。
    本类**绝不调 log()**：直接追加到日志文件（与 log() 同一把锁、同一种轮转），
    不再回流泵线程。重入闸 `_LOG_TO_STDOUT` 是第二道保险。
    """

    def write(self, s):
        s = s or ""
        if not s.strip():
            return
        _append_log_file("%s  %s" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                     s.rstrip("\n")))

    def flush(self):
        pass


def init_logging():
    """装日志落点 + excepthook + stdout/stderr 兜底。main() 最开始调用。"""
    global LOG_FILE
    try:
        p = log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        LOG_FILE = p
    except Exception:
        LOG_FILE = None
    try:
        sys.excepthook = _excepthook
    except Exception:
        pass
    # 无控制台 exe：把 stdout/stderr 接到日志，避免 print 静默失败
    if sys.stdout is None or sys.stderr is None:
        dummy = _StdoutLogSink()
        if sys.stdout is None:
            sys.stdout = dummy
        if sys.stderr is None:
            sys.stderr = dummy
        _STDOUT_IS_SINK[0] = True
    log("[%s] 日志已启用：%s" % (APP_TITLE, LOG_FILE))


_RestartState = {"ok": None, "err": ""}     # P1-34：最近一次重启的起进程结果
_TRAY_ICON = None                            # ε-13（§6.6）：托盘句柄全局登记，
                                             # restart_self 的 os._exit(0) 前要用它清图标


# ── PyInstaller onefile：解包目录（%TEMP%\_MEIxxxx）的残留治理（V2.12.6）──────
# onefile 的 exe 实际跑**两个进程**：bootloader 父进程把资源解包到 %TEMP%\_MEIxxxx，
# 再启动真正跑 Python 的子进程。**「删除那个目录」这个动作握在父进程手里，且只在
# 子进程正常退出之后才执行** —— 所以凡走了「被强杀」的路径，目录就永久留下
# （每份 ~30 MB，实测堆积 173 份 ≈ 5.2 GB，把 C 盘吃到只剩 1.3 G）。
# 本项目自己的两条成因都已定位：
#   ① 桌面壳退出时 `taskkill /PID x /T /F` 连 bootloader 父进程一并杀掉
#      ⇒ 永远走不到删除那一步（修在 Rust 壳里，见 tauri-app/src-tauri/src/main.rs）；
#   ② 「重启程序」起新进程时**没清 PyInstaller 的解包环境变量** ⇒ 新 bootloader
#      看到「已解包」的标记，既不再解包、也不负责删 ⇒ 旧目录无人清理、永久残留
#      （修在这里：_restart_env）。
# 而 ① 那类强杀路径**防不胜防**（任务管理器 / 崩溃 / 关机 / 蓝屏），指望「每次都优雅
# 退出」不现实。所以再加一道兜底：**每次启动时清扫旧目录**（_sweep_stale_meipass）——
# 「谁进来谁打扫」，无论怎么被杀，堆积上限恒为「当前正在跑的那一个」，下次启动即清。
_MEI_PREFIX = "_MEI"                        # PyInstaller onefile 解包目录名前缀
_MEI_MARKER = "mei_owner.txt"               # 随包分发的归属标记（见 ComfyMediaViewer.spec）
_MEI_MARKER_TEXT = "ComfyMediaViewer-onefile-bundle"    # ⚠️ 与 mei_owner.txt 内容、spec 三处同步
_PROC_START_TS = time.time()                # 本进程启动时刻（比它新的 _MEI 一概不动，防并发互删）


def _is_our_mei_dir(path) -> bool:
    """判断 path 是不是「本程序解包出来的」_MEI 目录 —— 靠随包分发的归属标记。

    只看目录名不够：同一台机器上任何别的 PyInstaller onefile 程序（甚至别的
    版本的 CMV）都会留下同前缀的目录，删了就是毁别人正在用的程序。
    """
    try:
        f = Path(path) / _MEI_MARKER
        return f.is_file() and f.read_text("utf-8", "replace").strip() == _MEI_MARKER_TEXT
    except Exception:
        return False


def _sweep_stale_meipass(verbose=True, temp_root=None, current=None, started=None):
    """清掉本程序遗留在 %TEMP% 里的旧解包目录，返回 (删除数, 跳过数)。

    六条判据**缺一不可**（任一条不过就不动它）：
      ① 名字以 `_MEI` 开头  ② 是目录
      ③ 目录内带本程序归属标记（免得误删别的 PyInstaller 程序）
      ④ 不是当前进程正在使用的那个（`sys._MEIPASS`）
      ⑤ 最后修改时间早于本次进程启动（防两个实例并发启动时互删）
      ⑥ **重命名探测成功**（2026-11-05，A05）：上面五条都成立才轮到它。
         Windows 不允许重命名「含已打开/已映射文件」的目录 ⇒ rename 成功即证明
         此刻没有任何进程在用。这条替代了原来「靠 rmtree 撞 dll 锁来兜底」——
         rmtree 是**边删边抛**，真撞上只会把另一个实例的资源挖掉一块，不是保险。
    删除失败**一律静默跳过**：不动别人的目录，下次启动再来。

    只在打包运行（frozen）时动手：源码直启根本没有 _MEIPASS，也绝不允许去动
    用户的 %TEMP%（那只会误伤别的程序）。
    参数 temp_root / current / started 只为单元测试可注入；正常调用一律走默认。
    """
    if not getattr(sys, "frozen", False):
        return (0, 0)
    try:
        base = Path(temp_root) if temp_root else Path(tempfile.gettempdir())
        cur = Path(current) if current else Path(getattr(sys, "_MEIPASS", "") or "")
        t0 = _PROC_START_TS if started is None else started
        if not base.is_dir():
            return (0, 0)
        cur_key = os.path.normcase(os.path.realpath(str(cur))) if str(cur) else ""
        names = sorted(os.listdir(str(base)))
    except Exception as ex:
        if verbose:
            log(f"[mei-sweep] 扫描解包目录失败（忽略）：{ex}")     # 清理绝不能拖累启动
        return (0, 0)

    removed = kept = 0
    for name in names:
        if not name.startswith(_MEI_PREFIX):                    # ① 前缀
            continue
        p = base / name
        try:
            if not p.is_dir():                                  # ② 必须是目录
                continue
            key = os.path.normcase(os.path.realpath(str(p)))
            if cur_key and key == cur_key:                      # ④ 当前正在跑的那份
                continue
            if not _is_our_mei_dir(p):                          # ③ 归属标记
                continue
            if p.stat().st_mtime > t0:                          # ⑤ 比本次启动还新
                continue
            # A05（2026-11-05 审计复核）：判据 ⑤ 只挡住「本次启动之后才建」的目录，
            # 挡不住**先于本进程启动、仍在运行**的另一个实例 —— 它的目录 mtime 早于 t0，
            # 会被当成残留。而 `shutil.rmtree` 遇到被映射的 dll 是**删到一半才抛**
            # （能删的先删掉），等于把另一个实例的资源挖掉一块，它随后会在毫无征兆的
            # 地方崩（缺 dll / 缺资源）。「靠 dll 独占锁当保险」只保证不报错，不保证不损坏。
            #
            # 正解是先做一次**原子占用探测**：Windows 不允许重命名「含已打开/已映射
            # 文件」的目录 ⇒ rename 成功就等于「此刻没有任何进程在用」。先改名再删，
            # 失败就整目录原样留着，不存在部分删除这种中间态。
            probe = p.with_name(p.name + ".sweep-" + str(os.getpid()))
            try:
                os.rename(str(p), str(probe))
            except OSError:
                kept += 1                                       # 有人在用 ⇒ 一个字节都不动
                continue
            try:
                shutil.rmtree(str(probe))
                removed += 1
            except Exception:
                # rename 都成功了却删不掉（极少：改名后瞬间被杀软扫到）—— 尽量还原名字，
                # 还原不了也只能留着，绝不回退成「直接删原目录」。
                try:
                    os.rename(str(probe), str(p))
                except OSError:
                    pass
                kept += 1
                continue
        except Exception:
            kept += 1                                           # 被占用/权限不足 ⇒ 下次再来
            continue
    if verbose and (removed or kept):
        log(f"[mei-sweep] 清理旧解包目录：删除 {removed} 个，跳过 {kept} 个（被占用或非本程序）")
    return (removed, kept)


def _restart_env():
    """「重启程序」起新进程用的环境变量：**剔除 PyInstaller 的解包残留变量**。

    这是 `_MEI` 永久残留的成因 ②（实测 173 个）。PyInstaller onefile 的 bootloader
    靠环境变量向子进程传递「已经解包到哪里」：老版本用 `_MEIPASS2`，新版本用
    `_PYI_APPLICATION_HOME_DIR` / `_PYI_ARCHIVE_FILE` / `_PYI_PARENT_PROCESS_LEVEL`
    这一族。直接 `os.environ.copy()` 传给新进程的后果是：新 bootloader 一看这些变量
    还在，就认定「已经解包过了」，于是**既不解包到新目录、也不在退出时删目录** ——
    而旧目录本来该由旧 bootloader 删，旧 bootloader 又被 `os._exit(0)` 带走了
    ⇒ 那个目录从此没有任何人负责，永久留在 %TEMP%。

    剔掉之后，新进程就是一次**干净的解包**：它自己那棵树负责自己那份目录，
    生命周期闭合。（清理旧目录另有 _sweep_stale_meipass 兜底。）
    """
    env = os.environ.copy()
    for k in [k for k in env if k == "_MEIPASS2" or k.startswith("_PYI_")]:
        env.pop(k, None)
    return env


def restart_self(httpd=None, delay: float = 0.4, in_shell: bool = None):
    """原地重启：起一个新进程接管，然后退出自己（网页上的「重启程序」按钮用）。

    不用 os.execv——它在 Windows 上不给参数加引号，路径里带空格（本项目目录名就带）
    会把参数拆断，新进程直接起不来。改用 subprocess（会正确加引号）+ 先关监听
    socket，让新进程能绑回同一端口；--no-browser 强制追加，避免又弹一个标签页
    （用户当前页面本来就会自动重连）。

    P1-34：起新进程失败时**必须把监听还回来**并明确报错。旧实现先
    server_close() 再 Popen，Popen 抛 OSError 时只 log() + return ⇒ 端口没了、
    进程还在、用户无任何提示（「点了重启，界面就死了」）。现在失败则重新绑定
    同一端口继续服务，并把原因写进日志与 HTTP 响应。

    P1-35：桌面壳里**不能**用 DETACHED_PROCESS。壳退出时按登记的 sidecar pid
    `taskkill /T` 杀树，而脱离父进程的新后端 pid ≠ 壳登记的 pid ⇒ 壳一关，
    重启出来的后端成了**常驻孤儿**；下次启动还会被 _probe_own_instance 认成
    「已有实例」直接退出（「程序打不开了，但进程列表里有一个」）。
    检测到处于壳中（--app-dir 或环境变量）时改用普通子进程，保持父子关系。
    """
    if in_shell is None:
        in_shell = bool(_APP_DIR_OVERRIDE) or bool(os.environ.get("CMV_IN_SHELL"))

    def _go():
        time.sleep(delay)                       # 先让 HTTP 响应发出去
        if getattr(sys, "frozen", False):       # 打包成 exe：argv[0] 就是 exe 本身
            args = [sys.executable] + sys.argv[1:]
        else:
            args = [sys.executable, os.path.abspath(sys.argv[0])] + sys.argv[1:]
        if "--no-browser" not in args:
            args.append("--no-browser")
        log("[restart] 正在重启：" + " ".join(args))
        # ⚠️ 顺序很关键（P1-34）：**先起新进程，成功了才关监听**。
        #    旧实现反着来（先 server_close 再 Popen），Popen 一失败就两头空：
        #    端口已经没了、进程还活着、用户点了重启界面就死。
        #    另外 socketserver 的 server_close() 会**永久关掉套接字**，
        #    之后再 server_bind()/server_activate() 必然 WinError 10038
        #    （在非套接字上操作）—— 回滚根本救不回来。所以只能靠「延后关闭」
        #    来保证失败时监听毫发无伤。
        try:
            # P1-35：壳内不脱离（保持父子关系，壳才能按 pid 杀树）；
            #        独立运行才用 DETACHED_PROCESS 脱离父进程。
            flags = 0 if in_shell else (0x00000008 if _IS_WIN else 0)
            # V2.12.6：env 必须过一遍 _restart_env() —— 直接继承 os.environ 会把
            #         `_MEIPASS2` / `_PYI_*` 带给新进程，新 bootloader 于是认为
            #         「已解包」，既不解包也不删目录，旧 _MEI 从此永久残留（成因 ②）。
            _proc = subprocess.Popen(
                args, close_fds=True, cwd=os.getcwd(), creationflags=flags,
                env=_restart_env(),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
        except OSError as ex:
            # P1-34：新进程没起来 —— 监听**压根没关过**，直接继续服务
            log(f"[restart] 启动新进程失败：{ex}；监听未受影响，程序继续运行")
            _RestartState["ok"] = False
            _RestartState["err"] = str(ex)
            return
        # A06（2026-11-05 审计复核）：`Popen` 成功 ≠ 新进程能接管。它可能在几百毫秒内
        # 就因为「端口没抢到 / 单实例探测判定已有实例 / 缺依赖」自己退出，而我们还照着
        # `Popen` 的返回值关监听 + `os._exit(0)` ⇒ 旧进程退了、新进程也死了，
        # 程序彻底没了（用户看到的是「点了重启，窗口和托盘一起消失」）。
        # 先观察一小段时间：活着才继续关监听。重启本来就是用户主动操作，
        # 多等 1.5 秒远好过「重启完程序不见了」。
        _deadline = time.time() + 1.5
        while time.time() < _deadline and _proc.poll() is None:
            time.sleep(0.05)
        if _proc.poll() is not None:
            log(f"[restart] 新进程启动后立即退出（退出码 {_proc.returncode}）；"
                f"监听未受影响，程序继续运行")
            _RestartState["ok"] = False
            _RestartState["err"] = f"新进程启动后立即退出（退出码 {_proc.returncode}）"
            return
        # 新进程已就位，现在才释放端口（否则新进程只能顺延到下一个端口）
        try:
            if httpd is not None:
                httpd.server_close()
                # 4.3-12：daemon 线程不会被 join ⇒ 给在途请求一个短宽限再退出。
                # 最多 1.2s；没有在途请求时立即通过（常见情况）。
                _dl = time.time() + 1.2
                while time.time() < _dl and getattr(httpd, "_active", 0) > 0:
                    time.sleep(0.05)
        except Exception:
            pass
        _RestartState["ok"] = True
        _RestartState["err"] = ""
        # P1-3：重启路径同样要回收调试 Edge。新进程会另起一个空闲端口，
        # 旧的留在后台既占端口又继续暴露调试口（且旧 profile 的 singleton 锁
        # 会让新进程的 Edge 附着后立刻退出）。
        try:
            _close_comfy_browser("重启")
        except Exception:
            pass
        # ε-13（§6.6）：os._exit 会跳过 main 的 finally（那里才有 tray_icon.stop()），
        # 托盘图标残留在通知区直到鼠标扫过才消失 —— 「重启后残留旧图标」。
        # os._exit 不触发 atexit，所以必须在这里显式清掉。
        if _TRAY_ICON is not None:
            try:
                _TRAY_ICON.stop()
            except Exception:
                pass
        # 壳内：新后端继承父进程，壳退出时会连它一起杀；本进程正常退出即可
        os._exit(0)

    threading.Thread(target=_go, daemon=True).start()


# ── Windows：把「资源管理器 / 外部程序」窗口带到前台 ────────────────────
# 本程序以无控制台方式运行（后台进程），由它启动的 explorer / 图片查看器
# 受 Windows「前台锁」限制，默认只会在任务栏闪烁而不弹出。这里在启动后
# 轮询找到新出现的顶层窗口，再用「合成 Alt 键 + AttachThreadInput」解除
# 前台锁并激活它，从而让窗口真正弹到最前面。
_IS_WIN = (sys.platform == "win32")
if _IS_WIN:
    import ctypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_long)

    _user32.EnumWindows.argtypes = [_WNDENUMPROC, ctypes.c_long]
    _user32.EnumWindows.restype = ctypes.c_bool
    _user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetClassNameW.restype = ctypes.c_int
    _user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    _user32.GetWindowTextW.restype = ctypes.c_int
    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.IsWindowVisible.restype = ctypes.c_bool
    _user32.IsIconic.argtypes = [ctypes.c_void_p]
    _user32.IsIconic.restype = ctypes.c_bool
    _user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.ShowWindow.restype = ctypes.c_bool
    _user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    _user32.SetForegroundWindow.restype = ctypes.c_bool
    _user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
    _user32.BringWindowToTop.restype = ctypes.c_bool
    _user32.GetForegroundWindow.argtypes = []
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    _user32.GetWindowThreadProcessId.restype = ctypes.c_ulong
    _user32.AttachThreadInput.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_bool]
    _user32.AttachThreadInput.restype = ctypes.c_bool
    _user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, ctypes.c_ulong, ctypes.c_void_p]
    _user32.keybd_event.restype = None
    _kernel32.GetCurrentThreadId.argtypes = []
    _kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
else:
    _user32 = None
    _kernel32 = None

_SKIP_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman",
                 "WorkerW", "Windows.UI.Core.CoreWindow", "EdgeUiInputTopWndClass",
                 "Shell_TrayWnd", "NotifyIconOverflowWindow", "tooltips_class32"}


def _snapshot_windows():
    """返回 {hwnd: (class_name, title)}，只含可见的顶层窗口。"""
    if not _IS_WIN:
        return {}
    out = {}

    @_WNDENUMPROC
    def _cb(hwnd, _lp):
        try:
            if not hwnd or not _user32.IsWindowVisible(hwnd):
                return True
            cbuf = ctypes.create_unicode_buffer(256)
            _user32.GetClassNameW(hwnd, cbuf, 256)
            tbuf = ctypes.create_unicode_buffer(512)
            _user32.GetWindowTextW(hwnd, tbuf, 512)
            out[int(hwnd)] = (cbuf.value, tbuf.value)
        except Exception:
            pass
        return True

    try:
        _user32.EnumWindows(_cb, 0)
    except Exception:
        pass
    return out


def _force_foreground(hwnd) -> bool:
    """强制把 hwnd 提到最前：先合成 Alt 解除前台锁，失败再挂接输入线程重试。"""
    if not _IS_WIN or not hwnd:
        return False
    try:
        _user32.ShowWindow(hwnd, 9 if _user32.IsIconic(hwnd) else 5)  # 9=SW_RESTORE 5=SW_SHOW
    except Exception:
        pass

    def _try_activate():
        try:
            _user32.keybd_event(0x12, 0, 0, None)            # Alt down
            try:
                _user32.SetForegroundWindow(hwnd)
                _user32.BringWindowToTop(hwnd)
            finally:
                _user32.keybd_event(0x12, 0, 0x0002, None)   # Alt up (KEYEVENTF_KEYUP)
        except Exception:
            return False
        time.sleep(0.03)
        try:
            fg = _user32.GetForegroundWindow()
            return bool(fg) and int(fg) == int(hwnd)
        except Exception:
            return False

    if _try_activate():
        return True
    try:                                                     # 兜底：挂接前台线程绕过前台锁
        fg = _user32.GetForegroundWindow()
        fg_tid = _user32.GetWindowThreadProcessId(fg, None) if fg else 0
        cur_tid = _kernel32.GetCurrentThreadId()
        attached = bool(fg_tid and fg_tid != cur_tid
                        and _user32.AttachThreadInput(cur_tid, fg_tid, True))
        try:
            return _try_activate()
        finally:
            if attached:
                _user32.AttachThreadInput(cur_tid, fg_tid, False)
    except Exception:
        return False


def _bring_new_window_to_front(before, cls=None, title_hint="", timeout=3.0):
    """在 before 快照之后新出现的窗口里挑一个置前。cls 为窗口类名过滤。"""
    if not _IS_WIN:
        return False
    target = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        for hwnd, (c, t) in _snapshot_windows().items():
            if hwnd in before or c in _SKIP_CLASSES:
                continue
            if cls and c != cls:
                continue
            if not cls:
                # ε-13（§6.6）：cls=None（系统默认程序打开）时此前只要求「有标题」——
                # 4 秒窗口期里弹出的**任何**有标题窗口（微信通知、别的程序）都会被
                # 抢到前台。现在必须有 title_hint（文件名）命中才认，宁可不置前
                # （顶多任务栏闪烁）也不抢无关窗口。
                if not title_hint or title_hint.lower() not in t.lower():
                    continue
            target = hwnd
            break
        if target:
            break
        time.sleep(0.15)
    if not target and cls and title_hint:
        # explorer 可能复用了已打开的窗口（不产生新 hwnd）：按标题（目录名）找
        for hwnd, (c, t) in _snapshot_windows().items():
            if c == cls and t.strip().startswith(title_hint):
                target = hwnd
                break
    if not target:
        log("（未定位到新窗口，目标可能仅在任务栏闪烁）")
        return False
    ok = _force_foreground(target)
    log(("（窗口已置前 hwnd=%s）" % target) if ok else "（窗口置前失败，仅在任务栏闪烁）")
    return ok


def reveal_in_explorer(path: Path) -> None:
    """资源管理器中选中文件，并把窗口带到前台。"""
    before = _snapshot_windows()
    subprocess.Popen(["explorer", "/select,", str(path)])
    threading.Thread(target=_bring_new_window_to_front,
                     args=(before, "CabinetWClass", path.parent.name or "", 3.0),
                     daemon=True).start()


# ---------------- 剪贴板：复制「文件」而不是「位图」 ----------------
# 2026-11-07（用户要求）：大图右键原先只有浏览器原生的「复制图像」—— 它把 <img> 已解码
# 的像素重新编码后放上剪贴板（Windows 上走 CF_DIB），PNG 里的 tEXt 块（ComfyUI 的
# workflow / prompt）就此全丢，发出去的图再也拖不回 ComfyUI 复现。
# 「复制文件」走的是资源管理器 Ctrl+C 那条路：CF_HDROP（fmt=15），粘贴到聊天窗口 /
# 文件夹里都是**磁盘上那份原字节**，元数据完好。
_CF_HDROP = 15
_GMEM_MOVEABLE = 0x0002


def copy_files_to_clipboard(paths) -> str:
    """把若干本地文件放进 Windows 剪贴板。成功返回空串，否则返回给用户看的原因。

    DROPFILES 布局（20 字节头 + UTF-16LE 路径列表，列表双 NUL 结尾）：
        DWORD pFiles(=20) / POINT pt / BOOL fNC(=0) / BOOL fWide(=1)
    fWide=1 才能带中文与空格 —— 用 A 版（ANSI）在这台机器的中文目录下会直接乱码。

    ⚠️ SetClipboardData 成功之后那段内存的所有权归系统，**绝不能** GlobalFree：
       否则粘贴方读到的是已释放内存（表现为乱码、粘贴为空，甚至目标程序崩）。
    ⚠️ GlobalAlloc/GlobalLock 在 x64 上必须显式声明 restype，否则 HGLOBAL/LPVOID
       被截成 32 位（ctypes 默认 c_int）—— 指针一截断就是段错误或写坏堆。
    """
    if not _IS_WIN:
        return "只有 Windows 支持把文件放进剪贴板"
    files = [str(Path(p)) for p in paths]
    if not files:
        return "没有要复制的文件"
    for f in files:
        if not os.path.isfile(f):
            return "文件已不存在：%s" % f
    try:
        from ctypes import wintypes as W
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [W.HWND]
        user32.OpenClipboard.restype = W.BOOL
        user32.CloseClipboard.restype = W.BOOL
        user32.EmptyClipboard.restype = W.BOOL
        user32.SetClipboardData.argtypes = [W.UINT, W.HANDLE]
        user32.SetClipboardData.restype = W.HANDLE
        kernel32.GlobalAlloc.argtypes = [W.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = W.HGLOBAL
        kernel32.GlobalLock.argtypes = [W.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [W.HGLOBAL]
        kernel32.GlobalUnlock.restype = W.BOOL
        kernel32.GlobalFree.argtypes = [W.HGLOBAL]

        blob = ("".join(f + "\0" for f in files) + "\0").encode("utf-16-le")
        data = struct.pack("<IiiII", 20, 0, 0, 0, 1) + blob
        h = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        if not h:
            return "分配剪贴板内存失败（%d 字节）" % len(data)
        ptr = kernel32.GlobalLock(h)
        if not ptr:
            kernel32.GlobalFree(h)
            return "锁定剪贴板内存失败"
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            kernel32.GlobalUnlock(h)
        # 别的程序（输入法、剪贴板管理器）可能正开着剪贴板，OpenClipboard 会失败 ——
        # 重试 ~600ms 再报错，比让用户「再点一次」友好。
        opened = False
        for _ in range(12):
            if user32.OpenClipboard(None):
                opened = True
                break
            time.sleep(0.05)
        if not opened:
            kernel32.GlobalFree(h)
            return "剪贴板正被其它程序占用，请稍后重试"
        try:
            if not user32.EmptyClipboard():
                return "清空剪贴板失败"
            if not user32.SetClipboardData(_CF_HDROP, h):
                return "写入剪贴板失败（错误码 %d）" % ctypes.get_last_error()
            h = None                    # 所有权已交给系统：绝不能 Free
            return ""
        finally:
            user32.CloseClipboard()
            if h:
                kernel32.GlobalFree(h)
    except Exception as ex:             # 非 Windows 环境下 ctypes.WinDLL 直接抛
        return "%s: %s" % (type(ex).__name__, ex)


def launch_with_default_app(path: Path) -> str:
    """用系统默认程序打开，并把新窗口带到前台。返回空串表示成功，否则为错误信息。"""
    before = _snapshot_windows()
    try:
        os.startfile(str(path))
    except (OSError, AttributeError) as ex:
        return f"{type(ex).__name__}: {ex}"
    threading.Thread(target=_bring_new_window_to_front,
                     args=(before, None, path.name, 4.0), daemon=True).start()
    # ε-13（§6.6）：cls=None 必须带 title_hint（文件名）—— 看图器的窗口标题
    # 普遍含文件名，凭它过滤才不会把 4 秒里弹出的无关窗口抢到前台。
    return ""


def open_external(p: Path) -> dict:
    """大图右键「用默认图片查看器打开」：交给系统关联程序，并把新窗口带到前台。

    只认图片 / 视频扩展名。这个入口的语义是「看这张图」，不是「在库里执行任意文件」——
    即便 root 下混进了 .exe / .bat / .lnk，也不给它一条「用关联程序拉起可执行文件」的路
    （虽然本机用户自己双击也是一样的效果，但接口不该主动提供这条捷径）。
    """
    if p.suffix.lower() not in (IMAGE_EXTS | VIDEO_EXTS):
        return {"ok": False, "error": "只能用默认程序打开图片 / 视频"}
    err = launch_with_default_app(p)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True}


def _make_server(host: str, preferred: int, handler):
    """按端口段直接尝试 bind，成功即返回 —— 避免「先探测空闲、再绑定」之间被抢的竞态。"""
    last = None
    for p in [preferred] + [preferred + i for i in range(1, 11)] + [0]:
        try:
            return QuietServer((host, p), handler)
        except OSError as ex:
            last = ex
    raise last if last else OSError("no available port")


def _port_listening(port: int) -> bool:
    """端口上是否真有监听者（纯本地 bind 试探，微秒级，绝不发网络包）。

    2026-10-20：启动慢的第二个来源。旧实现直接对 preferred..preferred+10 逐个
    `create_connection(timeout=0.4)`；在本机安全软件丢弃未知端口 SYN 的机器上，
    **每个空端口都要等满 0.4 秒** ⇒ 启动白白多花 4 秒多。
    bind 失败 = 有人占着（Windows 不允许无 SO_REUSEADDR 的重复绑定），
    只有被占的端口才值得连过去问一句「你是不是 CMV」。
    """
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", port))
        return False                 # 能绑上 ⇒ 没有监听者
    except OSError:
        return True                  # 绑不上 ⇒ 已被占用（含 0.0.0.0 监听）
    finally:
        try:
            if s:
                s.close()
        except OSError:
            pass


# ------------------------------------------------- 局域网 / 远程访问（2026-11-07）
# httpd 会被「开关手机访问」换绑，所以不能再让各处捕获 main() 里的局部变量。
_SRV = {"httpd": None, "host": "127.0.0.1", "port": 0, "switching": False}
_LAN_IP_CACHE = {"ip": "", "at": 0.0}


_LAN_NETWORKS = tuple(map(ipaddress.IPv4Network, (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
)))


def _is_lan_ip(value: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return any(addr in net for net in _LAN_NETWORKS)


def _lan_ip(force: bool = False) -> str:
    """优先选择局域网 IPv4 地址，忽略 VPN 常用的 198.18.0.0/15。"""
    now = time.monotonic()
    if not force and _LAN_IP_CACHE["at"] and now - _LAN_IP_CACHE["at"] < 30:
        return _LAN_IP_CACHE["ip"]
    ip = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            candidate = sock.getsockname()[0]
            if _is_lan_ip(candidate):
                ip = candidate
    except OSError:
        pass
    if not ip:
        try:
            addresses = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET,
                                           socket.SOCK_DGRAM)
            candidates = [info[4][0] for info in addresses if _is_lan_ip(info[4][0])]
            ip = next((addr for addr in candidates if not addr.startswith("169.254.")),
                      candidates[0] if candidates else "")
        except OSError:
            pass
    _LAN_IP_CACHE.update(ip=ip, at=now)
    return ip


def _lan_url() -> str:
    """当前局域网地址（**不带令牌**；没开手机访问或探测不到私网 IPv4 时为空串）。

    地址里不再拼 `?token=`：那个链接等于一份**一年期只读凭证** —— 转发到聊天、
    贴进笔记、留在浏览器历史里，拿到的人就能看整个图库。现在另一台设备走 6 位
    验证码（5 分钟有效、用一次换新）；本机走环回本来就免令牌（见 `_local_direct`）。
    校验层同步收紧成「只收 Cookie」(见 `_auth_ok`) ⇒ 已经发出去的旧链接会失效，
    需要重新配对一次（2026-11-09 与第三方合版时确认：接受）。
    """
    host = _lan_ip() if STATE.get("lan") else ""
    return "http://%s:%s/" % (host, _SRV["port"]) if host else ""


def _lan_status(readonly: bool = False, local: bool = True) -> dict:
    on = bool(STATE.get("lan"))
    lan_url = _lan_url()
    port = _SRV.get("port") or 0
    return {"on": on,
            "url": f"http://127.0.0.1:{port}/" if local else "/",
            "lan_url": lan_url,
            "port": port,
            "ip": _lan_ip() if on else "",
            "readonly": bool(readonly),
            # 保留字段：既有门禁（lan_* / v2133_*）读它判断「令牌是否下发给本机」。
            # 前端不展示、不进 URL；远端一律给空串，免得又被拿去拼链接。
            # 只在「手机访问开着 + 本机」时给：关掉之后这个令牌已经没有意义，
            # 还给出去只会被前端/门禁当成「还能用」。
            "token": (STATE.get("token") or "") if (local and on) else "",
            "code_url": "/api/lan/code" if on and local else ""}


def _lan_cookie_token(settings: dict) -> str:
    # 旧令牌曾随链接和日志暴露。切换到新键时仅轮换一次。
    tok = str(settings.get("lan_cookie_token") or "")
    return tok if re.fullmatch(r"[A-Za-z0-9_-]{12}", tok) else secrets.token_urlsafe(9)


def _lan_apply(on: bool, rotate: bool = False) -> bool:
    # 跨进程锁不能省（合版时第三方只用了 SETTINGS_LOCK）：两个实例 / 门禁并发
    # 同时改局域网设置时，「读 → 改 → 写」会被整表覆盖，丢的是全部设置。
    with _PAIR_LOCK, _store_lock(SETTINGS_LOCK, "局域网设置"):
        st = dict(load_settings())
        st["lan_on"] = bool(on)
        if on:
            tok = secrets.token_urlsafe(9) if rotate else _lan_cookie_token(st)
            st["lan_cookie_token"] = tok
            st.pop("lan_token", None)
        else:
            # 关闭不能等待磁盘写入，否则旧连接还能继续发请求。
            STATE["lan"] = False
        saved = _save_settings_locked(st)
        if on and not saved:
            return False
        if on:
            STATE.update(token=tok, lan=True)
        _PAIR.update(code="", exp=0.0, fails=0)
        _PAIR["ips"].clear()
        return saved


def _lan_rebind_soon(delay: float = 0.35) -> None:
    """让监听在 0.0.0.0 / 127.0.0.1 之间换绑。延迟是给当前响应留出发送时间。"""
    def _go():
        time.sleep(delay)
        with _PAIR_LOCK:
            h = _SRV["httpd"]
            host = "0.0.0.0" if STATE.get("lan") else "127.0.0.1"
            if h is None or _SRV["switching"] or _SRV["host"] == host:
                return
            _SRV["switching"] = True
        h.shutdown()          # 让主循环的 serve_forever 返回
    threading.Thread(target=_go, daemon=True).start()


def _shutdown_current() -> None:
    """托盘「退出」用：关的是**当前**这个 httpd，而不是创建托盘时捕获的那个。"""
    h = _SRV.get("httpd")
    if h is None:
        return
    threading.Thread(target=h.shutdown, daemon=True).start()


# ------------------------------------------------- 配对验证码（2026-11-07 第 5 批）
# 为什么删掉二维码：另一台**电脑**没有摄像头，扫不了；而 12 位随机令牌人眼又敲不动
# （0/O、1/l 还得辨认）。6 位数字两边都能用，代价是熵只有 10^6 —— 所以必须叠三层
# 约束，否则局域网里跑个脚本一顿连打就能进：
#   ① 5 分钟过期、**用一次立刻换新**（不可重放）；
#   ② 同一来源 60 秒内错 5 次 → 锁 120 秒（挡住单机连打）；
#   ③ 同一个码累计错 10 次 → 直接作废换新（挡住换 IP 的分布式猜）。
# 而且 6 位码换到的只是那把「远程只读」令牌：写操作按来源地址判，非环回一律拒。
_PAIR_TTL = 300.0
_PAIR_IP_FAIL = 5
_PAIR_IP_WIN = 60.0
_PAIR_IP_LOCK = 120.0
_PAIR_CODE_FAIL = 10
_PAIR = {"code": "", "exp": 0.0, "fails": 0, "ips": {}}
_PAIR_LOCK = threading.RLock()


def _pair_new() -> dict:
    """生成新码。randbelow ⇒ 无取模偏差；%06d ⇒ 允许 0 开头，永远 6 位。"""
    with _PAIR_LOCK:
        old = _PAIR["code"]
        code = "%06d" % secrets.randbelow(1000000)
        while code == old:
            code = "%06d" % secrets.randbelow(1000000)
        _PAIR.update(code=code, exp=time.monotonic() + _PAIR_TTL, fails=0)
        return _pair_status()


def _pair_status() -> dict:
    """当前码 + 剩余秒数。过期即换新（本机面板每次读取都拿到能用的码）。"""
    with _PAIR_LOCK:
        now = time.monotonic()
        if not _PAIR["code"] or now >= _PAIR["exp"]:
            return _pair_new()
        return {"code": _PAIR["code"],
                "left": max(0, int(round(_PAIR["exp"] - now))),
                "ttl": int(_PAIR_TTL)}


def _pair_note_fail() -> bool:
    """记一次错码。返回 True = 这个码已经作废并换新了。"""
    _PAIR["fails"] = int(_PAIR.get("fails") or 0) + 1
    if _PAIR["fails"] >= _PAIR_CODE_FAIL:
        _pair_new()
        return True
    return False


def _pair_prune(now: float) -> None:
    """清掉早已过期的来源记录，避免长跑进程里 dict 无限长。"""
    dead = [k for k, v in _PAIR["ips"].items()
            if now - float(v.get("at") or 0) > _PAIR_IP_WIN + _PAIR_IP_LOCK]
    for k in dead:
        _PAIR["ips"].pop(k, None)


def _pair_verify(got: str, ip: str):
    """校验一次 6 位码。返回 (ok, 给用户看的原因, 还能试几次)。"""
    with _PAIR_LOCK:
        now = time.monotonic()
        if not _PAIR["code"]:
            # 开机后还没人看过面板（没有码）。先备一个，再按「码不对」正常提示 ——
            # 否则第一次输错会被告知「已过期」，用户会以为自己看的是旧码。
            _pair_new()
        if now >= _PAIR["exp"]:
            _pair_new()
            return False, "验证码已过期，请在电脑上看新的", 0
        _pair_prune(now)
        rec = _PAIR["ips"].get(ip) or {"n": 0, "at": now, "until": 0.0}
        if float(rec.get("until") or 0) > now:
            return False, "尝试次数过多，请 %d 秒后再试" % int(rec["until"] - now + 1), 0
        if now - float(rec.get("at") or 0) > _PAIR_IP_WIN:
            rec = {"n": 0, "at": now, "until": 0.0}
        got = (got or "").strip()
        if len(got) == 6 and got.isascii() and got.isdigit() and secrets.compare_digest(got, _PAIR["code"]):
            _PAIR["ips"].pop(ip, None)
            _pair_new()                       # 单次有效：出门就换锁
            return True, "", 0
        rec["n"] = int(rec.get("n") or 0) + 1
        rec["at"] = now
        if rec["n"] >= _PAIR_IP_FAIL:
            rec["until"] = now + _PAIR_IP_LOCK
            rec["n"] = 0
            _PAIR["ips"][ip] = rec
            _pair_note_fail()
            return False, "尝试次数过多，请 %d 秒后再试" % int(_PAIR_IP_LOCK), 0
        _PAIR["ips"][ip] = rec
        if _pair_note_fail():
            return False, "验证码已作废，请在电脑上看新的", 0
        left = _PAIR_IP_FAIL - rec["n"]
        return False, "验证码不对，还可以试 %d 次" % left, left


_INSTANCE_MUTEX = [None]            # 持有的命名互斥量句柄（进程退出时由系统释放）


def _acquire_instance_mutex() -> bool:
    """进程级单实例锁（Windows 命名互斥量）。True = 本进程拿到了。

    4.3-10（2026-11-05 审计）：原来只靠「端口上有没有人监听」判单实例 —— 两个进程
    **同时**启动时都会探测到「没人监听」，于是各自往相邻端口 bind，两个实例都活着：
    同一个数据目录被两个进程并发写（收藏 / 标签 / 索引互相覆盖），而且用户开两个窗口
    看到两份不同的状态。

    名字带数据目录指纹：不同数据目录（两份便携版）互不影响。
    非 Windows / ctypes 不可用时返回 True（退回旧行为，绝不阻塞启动）。
    """
    if not _IS_WIN:
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        name = "Local\\ComfyMediaViewer." + hashlib.md5(
            os.path.normcase(str(DATA_DIR)).encode("utf-8", "replace")).hexdigest()[:16]
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.GetLastError.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k32.CreateMutexW(None, False, name)
        err = k32.GetLastError()
        if not h:
            return True                  # 建不出来：不阻塞启动
        if err == 183:                   # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            return False
        _INSTANCE_MUTEX[0] = h           # 故意不释放：句柄活到进程结束
        return True
    except Exception as ex:
        log(f"[warn] 单实例互斥量不可用，退回端口探测：{type(ex).__name__}: {ex}")
        return True


def _probe_own_instance(preferred: int, tries: int = 11):
    """探测 preferred 起的端口段上是否已有本程序实例在监听。

    判定依据：HTTP 响应的 Server 头以 MediaViewer/ 开头（404/403 也带）。
    命中返回端口号，否则返回 None。"""
    import socket
    for p in range(preferred, preferred + tries):
        if not _port_listening(p):
            continue                 # 没人监听：连过去只会白等超时（启动关键路径）
        s = None
        try:
            s = socket.create_connection(("127.0.0.1", p), timeout=0.2)
            # 读超时也是启动路径上的一笔账：本机 CMV 回一行头只需毫秒级，
            # 0.5 秒足够；留 1.5 秒只会让「端口刚释放、还挂在 TIME_WAIT 里」
            # 的情形白等 1.5 秒（实测过）。
            s.settimeout(0.5)
            s.sendall(b"GET /api/info HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
            data = b""
            while len(data) < 4096:
                c = s.recv(2048)
                if not c:
                    break
                data += c
        except OSError:
            continue
        finally:
            try:
                if s:
                    s.close()
            except OSError:
                pass
        if re.search(r"(?im)^server:\s*MediaViewer/", data.decode("latin-1", "ignore")):
            return p
    return None


def _tray_image():
    """托盘图标：优先打包资源（sys._MEIPASS）里的 app.ico，其次脚本同目录。"""
    from PIL import Image
    cands = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands.append(Path(meipass) / "app.ico")
    cands.append(Path(__file__).resolve().parent / "app.ico")
    for p in cands:
        try:
            if p.is_file():
                return Image.open(p)
        except Exception:
            continue
    return None


def _start_tray(httpd, url):
    """托盘图标线程：左键/双击=打开浏览器，右键菜单=打开/数据目录/退出。

    pystray 未安装或图标缺失时静默降级（不影响服务本身）。返回 icon 或 None。"""
    try:
        import pystray
    except ImportError:
        log("（pystray 未安装，托盘图标不可用）")
        return None
    img = _tray_image()
    if img is None:
        log("（托盘图标资源缺失，托盘不可用）")
        return None

    state = {"icon": None}

    def _open(item=None):
        webbrowser.open(url)

    def _open_data(item=None):
        d = DATA_DIR                     # F28：便携模式下托盘也要打开程序旁那个目录
        try:
            d.mkdir(parents=True, exist_ok=True)
            launch_with_default_app(d)     # Windows 资源管理器
        except OSError:
            pass

    def _quit(item=None):
        ic = state["icon"]
        if ic:
            try:
                ic.stop()
            except Exception:
                pass
        # 换绑后 httpd 会被替换，这里必须关「当前」那个，不能关创建托盘时捕获的旧对象
        _shutdown_current()

    menu = pystray.Menu(
        pystray.MenuItem("打开浏览器", _open, default=True),   # default=左键/双击
        pystray.MenuItem("打开数据目录", _open_data),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("退出", _quit),
    )
    icon = pystray.Icon("cmv", img, "ComfyMediaViewer", menu)
    state["icon"] = icon
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


def main():
    ap = argparse.ArgumentParser(description="ComfyUI 媒体浏览器")
    ap.add_argument("--root", default=None,
                    help="浏览的根目录（缺省时自动探测 ComfyUI output）")
    ap.add_argument("--port", type=int, default=8765, help="端口，默认 8765，被占用时自动后移")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--no-remember", action="store_true",
                    help="不把本次根目录写入用户「常用目录」（自动化脚本用，避免污染用户配置）")
    ap.add_argument("--lan", action="store_true",
                    help="局域网模式：监听 0.0.0.0，在本机设置中查看配对码")
    ap.add_argument("--app-dir", default=None,
                    help="程序目录（便携模式的标记与数据落点挂在这上面）。桌面壳把后端 exe "
                         "释放到别处运行时会传它，以还原用户真正放程序的那个目录；"
                         "普通直启不用传。环境变量 CMV_EXE_DIR 优先级更高")
    args = ap.parse_args()
    # P1-39：最早启用日志 —— 之后的任何崩溃（含 argparse 之后的路径）都会留痕
    init_logging()
    # B1-3（2026-10-09）：数据目录可写性自检。放在最前面 —— 便携模式被指向只读
    # U 盘 / 写保护 SD 卡 / 外接盘掉线时，之后所有 save_* 都会静默失败（只留日志），
    # 而日志恰好也写在那个写不进去的目录里。这里先探一次，界面才有话可说。
    try:
        probe_data_dir()
    except Exception:
        pass                    # 自检本身绝不能拖累启动（函数内部已吞 OSError）
    # V2.12.6：清扫上一次运行遗留的 onefile 解包目录（%TEMP%\_MEIxxxx）。
    # 2026-11-05：从「bind 之前」挪到「bind 之后」。旧位置在启动关键路径上同步
    # `rmtree` 一个 100 MB+ 的解包目录（每次启动都要删 1 个），实测能给冷启动
    # 多压 0.2~0.8 秒。清理本身与「谁能访问服务」毫无关系，晚几秒做完语义不变。
    # 顺带修掉审计 A05：旧顺序在**单实例检测之前**就动手，两个实例并发启动时
    # 有可能把对方正在使用的解包目录删掉（对方随即白屏）。
    global REMEMBER_ROOTS
    REMEMBER_ROOTS = not args.no_remember
    # --app-dir 在导入期已被 _preparse_app_dir() 读走（模块级常量要用），这里只是把
    # argparse 收到的值再确认一遍，防止有人用 add_argument 覆写或默认值漂移。
    global _APP_DIR_OVERRIDE
    if args.app_dir:
        _APP_DIR_OVERRIDE = args.app_dir

    # 手机访问的开关有两个来源：命令行 --lan，或设置里上次留下的开关（「记住设备」）。
    # 令牌同样从设置里取 —— 否则每次重启都换令牌，手机上存的 cookie 立刻失效，
    # 「记住设备」就成了一句空话。
    _st0 = load_settings()
    _lan_on = bool(args.lan) or bool(_st0.get("lan_on"))
    _lan_tok = _lan_cookie_token(_st0) if _lan_on else ""

    # —— 单实例：目标端口段上已有本程序实例 → 唤起它的浏览器，本进程退出 ——
    if not _lan_on:                 # 手机访问模式不参与单实例合并（端口要能被手机直连）
        # 4.3-10：先抢进程级互斥量。抢不到说明另一个进程正在启动或已在运行 ——
        # 等它把端口监听起来（最多 ~2.8s）再走下面的「唤起既有实例」分支，
        # 绝不允许「两个进程同时探测到没人监听」⇒ 各绑一个端口。
        _have_lock = _acquire_instance_mutex()
        prior = _probe_own_instance(args.port) if _have_lock else None
        if not _have_lock and prior is None:
            for _ in range(8):
                time.sleep(0.35)
                prior = _probe_own_instance(args.port)
                if prior:
                    break
        if not _have_lock and prior is None:
            log(f"[warn] 另一个 {APP_TITLE} 正在启动（单实例锁已被占用），本次不重复启动。"
                f"稍等几秒后刷新页面即可。")
            return
        if prior:
            woke = False
            if not args.no_browser:
                try:
                    _OPENER_NO_PROXY.open(
                        urllib.request.Request(
                            f"http://127.0.0.1:{prior}/api/activate", method="POST"),
                        timeout=3)
                    woke = True
                except OSError as ex:
                    # 2026-11-05 P2：旧代码 `except OSError: pass` 之后**无条件** log
                    # 「已唤起其窗口」并 return ⇒ 对方 500 / 超时 / 被安全软件拦时，
                    # 用户双击后什么都没有发生，日志还在说成功了。
                    # 降级：直接把浏览器指到那个实例的首页 —— activate 只是请它自己
                    # 开窗，失败不影响页面本身可用。
                    log(f"[warn] 唤起已有实例（端口 {prior}）失败：{type(ex).__name__}: {ex}")
                    try:
                        _OPENER_NO_PROXY.open(f"http://127.0.0.1:{prior}/", timeout=3)
                        woke = True
                        log(f"[{APP_TITLE}] 已用浏览器打开既有实例（端口 {prior}）。")
                    except OSError as ex2:
                        log(f"[warn] 打开既有实例页面也失败：{type(ex2).__name__}: {ex2}")
            else:
                woke = True          # --no-browser：本来就约定不弹窗
            if woke:
                log(f"[{APP_TITLE}] 已有实例在运行（端口 {prior}），本进程退出。")
            else:
                log(f"[warn] 已有实例在运行（端口 {prior}），但既无法唤起其窗口、也无法打开"
                    f"页面；请手动访问 http://127.0.0.1:{prior}/ 。"
                    f"本进程退出（继续启动会产生两个实例）。")
            return

    # 惰性探测：显式传了 --root 就不做全盘扫描，启动即开
    root = args.root or default_root()
    # P0-1（10-15 全代码审计）：卷根作 root ⇒ inside_root 对整卷恒真 ⇒ 任意文件可被删。
    # 命令行手滑（--root C:\）或旧配置残留都走这里，直接回退到安全默认并说明原因。
    if _is_volume_root(root):
        log(f"[root] 拒绝把磁盘根 {root!r} 作为媒体根目录 —— 那会让整卷文件都落在可操作范围内。")
        root = default_root()
        if _is_volume_root(root):        # 极端：连探测出来的默认都是卷根（cwd 在盘根且无常用目录）
            log(f"[root] 探测到的默认目录仍是卷根 {root!r}，请用 --root 显式指定一个具体文件夹。")
    with STATE_LOCK:
        STATE["root"] = str(Path(root).resolve())
        if _lan_on:
            STATE["lan"] = True
            STATE["token"] = _lan_tok
        if REMEMBER_ROOTS:
            add_root(STATE["root"])   # 记入常用目录（下次免传）；--no-remember 时跳过
        httpd = _make_server("0.0.0.0" if _lan_on else "127.0.0.1",
                             args.port, Handler)
        # LAN 换绑要能在运行期替换 httpd，所以登记到模块级而不是只留在局部变量
        _SRV.update(httpd=httpd, host=("0.0.0.0" if _lan_on else "127.0.0.1"),
                    port=httpd.server_address[1])
    if _lan_on and _st0.get("lan_cookie_token") != _lan_tok:
        with SETTINGS_LOCK:
            settings = dict(load_settings())
            settings.pop("lan_token", None)
            settings.update(lan_cookie_token=_lan_tok, lan_on=True)
            _save_settings_locked(settings)
    # 2026-10-27（代码梳理 O16）：悬空收藏清理挪到后台线程。
    # 它要对收藏表里的每条路径 os.stat（外接盘休眠时能卡住数秒），而原先是在
    # bind 之后、**主线程**上同步跑的 —— 表现就是「双击后一段时间没反应」。
    # 清理本身幂等、且全程持跨进程锁（_store_lock），晚几秒不影响任何语义。
    threading.Thread(target=_bg_guard, args=("prune_favs", prune_favs, STATE["root"]),
                     daemon=True).start()
    # P8（2026-11-15）：悬空标签同样在启动后清一次（延迟 25s，见函数说明）。
    # 与 prune_favs 同款处理：后台线程 + _bg_guard 兜底，异常只进日志。
    threading.Thread(target=_bg_guard,
                     args=("prune_tags", startup_prune_tags, STATE["root"]),
                     daemon=True).start()
    # 2026-11-05：onefile 解包目录清扫（V2.12.6）也挪到后台 —— 见上方说明。
    threading.Thread(target=_bg_guard, args=("mei_sweep", _sweep_stale_meipass),
                     daemon=True).start()
    _gc_data_tmp()                    # B4-P2-3：清数据目录残留的 *.tmp（缩略图那套只管 THUMB_DIR）
    # P6（2026-11-15）：改走循环（启动一次 + 每 6 小时），不再是「只清这一次」。
    threading.Thread(target=_bg_guard, args=("thumb_gc_loop", thumb_gc_loop),
                     daemon=True).start()
    threading.Thread(target=_bg_guard, args=("trash_gc", trash_gc),
                     daemon=True).start()                     # 后台清超龄回收站（B6-P2-3 兜底）
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    if _lan_on:
        # 本机浏览器仍走环回地址（cookie 就种在 127.0.0.1 上），手机走局域网地址。
        # 不带 `?token=`：令牌一旦进地址栏 / 日志，就等于把图库的一年期只读凭证
        # 抄在了容易被贴出去的地方（cmv.log 常随 bug 报告上传）。
        url = f"http://127.0.0.1:{port}/"
        lan_url = _lan_url()
        if lan_url:
            log(f"[{APP_TITLE}] 手机访问已开启（另一台设备打开 {lan_url}，再输面板上的 6 位验证码）")
        else:
            log("[lan] 没探测到局域网 IPv4 地址")
    log(f"[{APP_TITLE}] {url}  root={STATE['root']}  Pillow={'on' if HAS_PIL else 'off'}")
    log("按 Ctrl+C 退出，或用托盘图标退出。")
    if not args.no_browser:
        # ε-13（§6.6）：Timer 非守护 —— 启动 0.6s 内进程因故退出（单实例合并、
        # 端口异常）时，这个定时器会把进程拖住 0.6s 并且**反向弹出浏览器**，
        # 用户看到的是「程序都退了又自己开了个网页」。守护线程随主线程退出即消失。
        _t = threading.Timer(0.6, lambda: webbrowser.open(url))
        _t.daemon = True   # ε-13（§6.6）：非守护会拖住进程退出；Timer 构造器不收 daemon 关键字
        _t.start()
    tray_icon = None
    # 2026-11-07：手机访问模式下也要有托盘 —— 原先这里 `if not args.lan` 直接不建，
    # 结果是「开了手机访问就找不到退出入口」（桌面壳还没有窗口按钮可点）。
    tray_icon = _start_tray(httpd, url)
    if tray_icon is not None:
        globals()["_TRAY_ICON"] = tray_icon   # ε-13（§6.6）：登记给 restart_self 清理用
    try:
        # 2026-11-07：serve_forever 变成「可被换绑打断」的循环。
        # 开关手机访问要改 bind 地址（0.0.0.0 ⇄ 127.0.0.1），而 socket 一旦 bind
        # 就无法原地改；只能 shutdown → server_close → 用新地址重绑。
        # `_lan_rebind_soon()` 先置 switching 再 shutdown，这里据此决定重来还是退出。
        while True:
            try:
                httpd.serve_forever(poll_interval=0.2)
            except KeyboardInterrupt:
                log("bye.")
                break
            with _PAIR_LOCK:
                if not _SRV["switching"]:
                    break                  # 托盘「退出」/ 其他 shutdown ⇒ 正常收尾
                _host = "0.0.0.0" if STATE.get("lan") else "127.0.0.1"
                if _host != _SRV["host"]:
                    httpd.server_close()
                    try:
                        httpd = _make_server(_host, _SRV["port"], Handler)
                    except OSError as ex:
                        log(f"[lan] 换绑 {_host} 失败：{ex}；回退到本机监听")
                        _lan_apply(False)
                        _host = "127.0.0.1"
                        httpd = _make_server(_host, _SRV["port"], Handler)
                    _SRV.update(httpd=httpd, host=_host, port=httpd.server_address[1])
                    log(f"[lan] 监听已切换到 {_host}:{httpd.server_address[1]}")
                _SRV["switching"] = False
    finally:
        # P1-3：带 --remote-debugging-port 的独立 Edge 必须随程序退出回收，
        # 否则它会常驻后台、调试端口对任意本机进程开放。
        try:
            _close_comfy_browser("程序退出")
        except Exception:
            pass
        if tray_icon:
            try:
                tray_icon.stop()
            except Exception:
                pass


if __name__ == "__main__":
    main()
