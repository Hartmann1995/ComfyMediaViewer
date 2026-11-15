#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

//! ComfyMediaViewer 桌面壳（Tauri 2）
//!
//! 架构：Tauri 窗口 + 现有 Python 后端（PyInstaller exe）作为 sidecar。
//! - 单实例保护（ε-10 / 7-6）：第二实例在插件 init 阶段即退出，后端尚未 spawn
//! - 启动时探测空闲端口（从 8765 起依次尝试；全部被占则明确报错）
//! - spawn 后端 exe（--port <p> --no-browser，隐藏控制台窗口），并把它挂进一个
//!   「壳一消失就杀树」的 Job Object（第 22 级 P1-1：强杀 / 关机也不留孤儿）
//! - 轮询端口就绪后把 WebView 从启动页导航到 http://127.0.0.1:<p>
//!   （ε-10 / 7-7：就绪判定不再是「TCP 能连上」，而是 /api/info 响应带
//!    `Server: MediaViewer/` 头；第 22 级 P1-4 再加一条**一次性令牌**：
//!    壳 spawn 时经环境变量给后端一个随机串，后端在响应头里回显，
//!    壳逐字节比对 —— 端口被抢注也拿不到令牌，装不进 iframe）
//! - 后端就绪后持续监控存活（ε-10 / 7-9）：进程退出且端口上没有 CMV 服务时
//!   在壳页弹横幅并落盘 shell.log；若端口上仍有服务（后端自重启接管）则静默
//! - 应用退出或 panic 时终止后端进程树，不留孤儿进程

#[cfg(all(not(debug_assertions), dev))]
compile_error!("Desktop release builds require --features custom-protocol.");

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::os::windows::io::AsRawHandle;
use std::os::windows::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, WebviewWindow};

#[derive(Clone, Default, serde::Serialize)]
struct StartupStatus {
    url: Option<String>,
    error: Option<String>,
}
static STARTUP_STATUS: Mutex<StartupStatus> = Mutex::new(StartupStatus { url: None, error: None });

#[tauri::command]
fn desktop_startup_state() -> StartupStatus {
    STARTUP_STATUS.lock().unwrap_or_else(|e| e.into_inner()).clone()
}

#[tauri::command]
fn desktop_frontend_loaded() {
    trace("frontend: 应用 iframe 已完成加载");
}

fn native_startup_error(message: &str) {
    // 弹框之前先落盘：弹框里写的那句「详细记录：程序旁的 ComfyMediaViewer-startup.log」
    // 必须当场成立，不能等进程退出（失败路径常常直接 exit）。
    dump_boot_log(message);
    #[link(name = "user32")]
    extern "system" {
        fn MessageBoxW(hwnd: *mut std::ffi::c_void, text: *const u16, title: *const u16, flags: u32) -> i32;
    }
    let body: Vec<u16> = format!("{message}\n\n详细记录：程序旁的 ComfyMediaViewer-startup.log")
        .encode_utf16().chain(Some(0)).collect();
    let title: Vec<u16> = "ComfyMediaViewer 启动失败".encode_utf16().chain(Some(0)).collect();
    unsafe { MessageBoxW(std::ptr::null_mut(), body.as_ptr(), title.as_ptr(), 0x10); }
}


// Fail before creating WebView: a low-integrity EXE can read existing AppData
// files but cannot deploy its backend or write WebView state there. An HTML
// error is unreliable when that same restriction prevents WebView rendering.
fn probe_startup_directory(dir: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dir)?;
    let path = dir.join(format!(".cmv-startup-write-probe-{}-{}", std::process::id(), new_token()));
    let mut file = std::fs::OpenOptions::new().write(true).create_new(true).open(&path)?;
    let result = file.write_all(b"ComfyMediaViewer startup write check");
    drop(file);
    // This is exclusively the temporary file just created above, never user data.
    let cleanup = std::fs::remove_file(&path);
    result.and(cleanup)
}

fn check_startup_storage() -> Result<(), String> {
    let base = std::env::var_os("LOCALAPPDATA")
        .map(PathBuf::from).ok_or("无法定位 Windows 用户数据目录 LOCALAPPDATA")?;

    // 后端目录**强制**：写不进去就一定部署不了 sidecar，白屏是必然结局，早拦早说。
    let backend = base.join("ComfyMediaViewer").join("backend");
    probe_startup_directory(&backend).map_err(|error| format!(
        "启动前写入检查失败：{}\n原因：{error}\n\n当前程序：{}\n\n\
         请将发布 ZIP 重新解压到桌面上的新文件夹，再运行其中的 EXE。\n\
         不要只移动已解压的 EXE：它可能保留原目录的低完整性标记。\n\
         关闭后台进程不能解决目录写入权限问题。",
        backend.display(), std::env::current_exe().unwrap_or_default().display()
    ))?;

    // WebView 缓存目录**只告警**：这里写不进去时 WebView2 往往会自己换目录，
    // 强行拦下会变成「以前能开、现在直接报错」（组策略锁 %LOCALAPPDATA% 的场景）。
    let ui = base.join("cf.mediaviewer.desktop");
    if let Err(error) = probe_startup_directory(&ui) {
        append_shell_log(&format!(
            "[startup-write-probe] WebView 缓存目录不可写（不阻断）：{} — {error}",
            ui.display()
        ));
    } else {
        trace("startup-write-probe: 后端与 WebView 目录均可写");
    }
    Ok(())
}

/// 后端 exe 直接嵌入壳内（单文件绿色版），首次启动释放到 LOCALAPPDATA
const BACKEND_BYTES: &[u8] =
    include_bytes!("../binaries/ComfyMediaViewer-x86_64-pc-windows-msvc.exe");
/// 后端释放目录（与应用数据目录同根，子目录 backend 不与 json 数据冲突）
const BACKEND_EXE_NAME: &str = "ComfyMediaViewer.exe";
/// Win32 CREATE_NO_WINDOW：后台进程不弹控制台窗口
const CREATE_NO_WINDOW: u32 = 0x0800_0000;
/// 后端端口就绪等待上限（PyInstaller 自解压首次启动可能较慢）
const BACKEND_TIMEOUT: u64 = 60;
/// 端口探测起始值（与后端默认端口一致）
const PORT_START: u16 = 8765;
/// 端口探测尝试数量
const PORT_TRIES: u16 = 50;
/// 身份探测（/api/info）的读/写超时
const PROBE_TIMEOUT_MS: u64 = 1500;
/// 后端存活监控的轮询周期（7-9）
const MONITOR_POLL_MS: u64 = 2000;
/// 后端把壳给的一次性令牌回显在这个响应头里（P1-4）。
///
/// 用环境变量而非命令行传令牌：命令行在任务管理器 / WMI 里对所有同用户进程可见，
/// 环境变量要读得先拿到进程句柄（PROCESS_VM_READ），门槛高得多。
/// 后端自重启走 `_restart_env()`（`os.environ` 拷贝）⇒ 令牌自然继承给新进程；
/// 令牌本身也只在一个响应头里回显，跨源 JS 读不到（没开 Access-Control-Expose-Headers）。
const SHELL_TOKEN_ENV: &str = "CMV_SHELL_TOKEN";
const SHELL_TOKEN_HEADER: &str = "x-cmv-shell-token";
/// 释放出来的后端 exe 路径：杀进程前用它核对「这个 pid 还是不是我的后端」。
static BACKEND_EXE: OnceLock<PathBuf> = OnceLock::new();
/// 本次启动的一次性令牌（空 = 不做令牌校验，只用于单元测试路径）。
static SHELL_TOKEN: OnceLock<String> = OnceLock::new();
/// 「壳一消失就把整棵后端树杀掉」的 Job Object 句柄（转成 usize 存）。
/// 句柄**故意不主动关闭**：进程以任何方式结束（含任务管理器强杀、关机）时由内核
/// 关闭它，JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 随即终止整棵树 —— 这是 P1-1 的核心。
static BACKEND_JOB: OnceLock<usize> = OnceLock::new();

/// 最薄的 Win32 绑定：只声明本文件真正用到的那几个（不引 windows-sys，
/// 避免为 6 个调用点新增一整条依赖链，也让 Cargo.lock 保持不动）。
mod win32 {
    use std::ffi::c_void;

    pub type Handle = *mut c_void;

    #[repr(C)]
    #[derive(Default)]
    pub struct BasicLimit {
        pub per_process_user_time: i64,
        pub per_job_user_time: i64,
        pub limit_flags: u32,
        pub min_working_set: usize,
        pub max_working_set: usize,
        pub active_process_limit: u32,
        pub affinity: usize,
        pub priority_class: u32,
        pub scheduling_class: u32,
    }

    #[repr(C)]
    #[derive(Default)]
    pub struct IoCounters {
        pub read_ops: u64,
        pub write_ops: u64,
        pub other_ops: u64,
        pub read_bytes: u64,
        pub write_bytes: u64,
        pub other_bytes: u64,
    }

    #[repr(C)]
    #[derive(Default)]
    pub struct ExtendedLimit {
        pub basic: BasicLimit,
        pub io: IoCounters,
        pub process_memory_limit: usize,
        pub job_memory_limit: usize,
        pub peak_process_memory: usize,
        pub peak_job_memory: usize,
    }

    pub const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x0000_2000;
    pub const JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: i32 = 9;
    pub const PROCESS_QUERY_LIMITED_INFORMATION: u32 = 0x1000;

    #[link(name = "kernel32")]
    extern "system" {
        pub fn CreateJobObjectW(attrs: *mut c_void, name: *const u16) -> Handle;
        pub fn SetInformationJobObject(
            job: Handle,
            class: i32,
            info: *mut c_void,
            len: u32,
        ) -> i32;
        pub fn AssignProcessToJobObject(job: Handle, process: Handle) -> i32;
        pub fn OpenProcess(access: u32, inherit: i32, pid: u32) -> Handle;
        pub fn QueryFullProcessImageNameW(
            process: Handle,
            flags: u32,
            buf: *mut u16,
            size: *mut u32,
        ) -> i32;
        pub fn CloseHandle(obj: Handle) -> i32;
    }
}

/// 一次性令牌：SipHash 的键由 OS 随机源生成（`RandomState::new` 的固有行为），
/// 取两个独立实例拼成 128 位十六进制串。不引 rand 依赖，也不拿时间戳凑数。
fn new_token() -> String {
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};
    let mut out = String::with_capacity(32);
    for _ in 0..2 {
        let mut h = RandomState::new().build_hasher();
        h.write_u64(std::process::id() as u64);
        out.push_str(&format!("{:016x}", h.finish()));
    }
    out
}

/// 后端子进程句柄的全局托管；None = 后端未成功启动
struct BackendProc(Mutex<Option<Child>>);

/// 后端 pid（spawn 成功即登记）。panic hook 拿不到 Tauri state，
/// 用全局槽让「任何路径的 panic」都能杀掉后端（ε-10 / 7-9）。
static BACKEND_PID: OnceLock<u32> = OnceLock::new();

/// 后端端口（spawn 前登记）。P0-11 的退出兜底需要它：壳内「重启程序」= 后端自己起
/// 新进程接管（同端口），此时壳登记的 pid 已经死了，只能按端口反查新后端真实 pid。
static BACKEND_PORT: OnceLock<u16> = OnceLock::new();

/// 从 PORT_START 起找一个当前空闲的端口；全部被占返回 None（ε-10 / 7-7：
/// 旧实现返回 PORT_START 兜底，等于明知被占还用，后端必 bind 失败白等 60 秒）。
fn pick_port() -> Option<u16> {
    for i in 0..PORT_TRIES {
        let p = PORT_START + i;
        if TcpListener::bind(("127.0.0.1", p)).is_ok() {
            return Some(p);
        }
    }
    None
}

/// 磁盘上那份后端，跟壳里嵌的是不是同一份。
///
/// ⚠️ 判据必须是**内容**，不能是**大小**：PE 段对齐（FileAlignment）会把小幅差异吸收掉，
/// 两版后端**长度完全相同**是完全可能的。那时「比大小」会认定磁盘上那份已经是最新的，
/// 于是用户拿到新壳、却继续跑上一版后端 —— 表现为「更新完界面 / 功能没变化」，
/// 而且极难排查（版本号、文件时间、体积看着都对）。
/// 代价只是启动时多读一次（约 15 MB，走系统缓存，十几毫秒）。
fn backend_is_current(current: Option<&[u8]>, embedded: &[u8]) -> bool {
    matches!(current, Some(cur) if cur == embedded)
}

/// 释放嵌入的后端 exe：%LOCALAPPDATA%\ComfyMediaViewer\backend\ComfyMediaViewer.exe。
/// 内容与嵌入的不一致（或文件读不到）才重写。
///
/// 返回值第二项 = 本次是否真的重写了（true 表示换了新后端）。启动计时靠它区分
/// 「比一遍」与「写 45 MB」两种成本 —— 后者在有实时防护的机器上能差两个数量级。
fn ensure_backend_extracted() -> Result<(PathBuf, bool), String> {
    let base = std::env::var("LOCALAPPDATA")
        .map_err(|_| "无法定位 LOCALAPPDATA".to_string())?;
    let dir = PathBuf::from(base)
        .join("ComfyMediaViewer")
        .join("backend");
    std::fs::create_dir_all(&dir)
        .map_err(|e| format!("创建后端目录失败: {e}"))?;
    let target = dir.join(BACKEND_EXE_NAME);
    trace("ensure_backend: 开始读已部署后端");

    // 读不到（不存在 / 被占用 / 无权限）一律当作需要重写，交给出错路径去报。
    let current = std::fs::read(&target).ok();
    let redeploy = !backend_is_current(current.as_deref(), BACKEND_BYTES);
    trace(&format!(
        "ensure_backend: 比对完成（已读 {} 字节，需重写={}）",
        current.as_ref().map(|c| c.len()).unwrap_or(0),
        redeploy
    ));
    if redeploy {
        // P2-2：临时文件用**固定名**复用，所以上一轮失败留下的残骸必须先清掉 ——
        // 否则下一次 write 会覆盖它（还好），但失败分支返回时没人负责删，
        // 45 MB 的空壳就永久留在用户目录里，而且每次启动都要重写一遍（无收敛）。
        let tmp = dir.join(".backend.tmp");
        // 清残骸失败不致命（多半是上一轮的杀软扫描还没放手），紧接着的 write 会如实报错。
        if tmp.exists() {
            let _ = std::fs::remove_file(&tmp);
        }
        if let Err(e) = std::fs::write(&tmp, BACKEND_BYTES) {
            let _ = std::fs::remove_file(&tmp);
            return Err(format!("写入后端失败: {e}"));
        }
        trace("ensure_backend: 临时文件已写完");
        // Windows 上 rename 不能覆盖已存在文件，先移除旧版。
        //
        // P1-37：旧实现是 `let _ = std::fs::remove_file(&target);` —— 错误被吞掉。
        // 而 Windows 上删除**正在运行的映像**必然失败（Access is denied），于是
        // 下一步 rename 也必然失败，用户只看到笼统的「部署后端失败」，看不到真因。
        // 现在：删除失败时把原因**带进错误信息**，并明确提示是旧进程占用。
        if let Err(e) = std::fs::remove_file(&target) {
            if target.exists() {
                // 文件还在 ⇒ 删除确实失败了，后面 rename 必定也失败，直接报真因
                let _ = std::fs::remove_file(&tmp);
                return Err(format!(
                    "部署后端失败：无法替换旧版后端（{e}）。\
                     通常是有旧的 ComfyMediaViewer 进程仍在运行占用了该文件，\
                     请在任务管理器中结束它后重试。目标文件：{}",
                    target.display()
                ));
            }
            // 文件本来就不存在（首次释放）——正常路径，继续
        }
        if let Err(e) = std::fs::rename(&tmp, &target) {
            let _ = std::fs::remove_file(&tmp);
            return Err(format!(
                "部署后端失败: {e}（目标 {}）",
                target.display()
            ));
        }
        trace("ensure_backend: 重命名完成");
    }
    Ok((target, redeploy))
}

/// 桌面壳自己的 exe 所在目录 —— 也就是用户「放程序的地方」。
///
/// ⚠️ 这个值**只**用来传 `--app-dir`，让后端知道自己跑在壳里（`server.py` 的
/// `in_shell` ⇒ 重启时用普通子进程、保持父子关系，壳才杀得掉）。
///
/// 它**不会**把数据目录搬到程序目录旁边：后端的数据目录判据是「exe 同目录有没有
/// `portable.flag`」（`_resolve_data_dir`），而桌面版默认不投放那个标记 ⇒ 数据仍在
/// `%LOCALAPPDATA%\ComfyMediaViewer`。旧注释把这两件事混成一句「由壳告诉后端程序
/// 目录，避免数据落 C 盘」，与实际行为不符（P2-5）；要便携就把 `portable.flag`
/// 放到**壳 exe 同目录**，后端会按它把数据放进程序目录下的 cmv-data。
fn app_dir() -> Option<PathBuf> {
    std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
}

fn spawn_backend(port: u16) -> Result<Child, String> {
    let (path, redeploy) = ensure_backend_extracted()?;
    trace(&format!("spawn_backend: 磁盘文件就位（本次重写={redeploy}），准备 CreateProcess"));
    let _ = BACKEND_EXE.set(path.clone());
    let mut cmd = Command::new(&path);
    cmd.args(["--port", &port.to_string(), "--no-browser"]);
    if let Some(dir) = app_dir() {
        cmd.arg("--app-dir").arg(dir);
    }
    // P1-4：把本次启动的一次性令牌交给后端，由它在响应头里回显给壳核对。
    if let Some(tok) = SHELL_TOKEN.get() {
        cmd.env(SHELL_TOKEN_ENV, tok);
    }
    let child = cmd
        .creation_flags(CREATE_NO_WINDOW)
        .spawn()
        .map_err(|e| format!("后端启动失败: {e}"))?;
    // P1-1：挂进「壳一消失就杀树」的 Job。必须**紧跟 spawn** —— PyInstaller onefile
    // 是先解包再 fork 子进程，我们有几百毫秒的窗口；等它 fork 完再挂，孙进程就不会
    // 跟着进 Job（Job 只在进程创建时继承），兜底就漏了。
    attach_backend_to_job(&child);
    trace(&format!("spawn_backend: 子进程已创建 pid={}", child.id()));
    Ok(child)
}

/// 建（或复用）带 KILL_ON_JOB_CLOSE 的 Job，并把刚 spawn 的后端挂进去。
fn attach_backend_to_job(child: &Child) {
    let job = match BACKEND_JOB.get() {
        Some(h) => *h,
        None => match create_kill_on_close_job() {
            Some(h) => {
                let _ = BACKEND_JOB.set(h);
                h
            }
            None => {
                append_shell_log("[job] 创建 Job Object 失败，后端只能靠退出/panic 两条用户态路径清理");
                return;
            }
        },
    };
    let ok = unsafe {
        win32::AssignProcessToJobObject(
            job as win32::Handle,
            child.as_raw_handle() as win32::Handle,
        )
    };
    if ok == 0 {
        append_shell_log(&format!(
            "[job] 后端 pid={} 加入 Job Object 失败（{}），强杀壳时可能留孤儿",
            child.id(),
            std::io::Error::last_os_error()
        ));
    }
}

fn create_kill_on_close_job() -> Option<usize> {
    unsafe {
        let job = win32::CreateJobObjectW(std::ptr::null_mut(), std::ptr::null());
        if job.is_null() {
            return None;
        }
        let mut info = win32::ExtendedLimit::default();
        info.basic.limit_flags = win32::JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let ok = win32::SetInformationJobObject(
            job,
            win32::JOBOBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            &mut info as *mut _ as *mut std::ffi::c_void,
            std::mem::size_of::<win32::ExtendedLimit>() as u32,
        );
        if ok == 0 {
            let _ = win32::CloseHandle(job);
            return None;
        }
        // 句柄不关：它的生命周期 = 壳进程的生命周期，这就是兜底本身。
        Some(job as usize)
    }
}

/// ε-10 / 7-7：向端口发 `GET /api/info` 并核对响应头里的 `Server: MediaViewer/`。
///
/// 这是**身份**探测，不只是「端口活着」：TOCTOU 空窗里端口可能被别的程序抢注
/// （探测 bind 与后端 bind 之间），旧实现只看 TCP connect 成功，会把别人的服务
/// 塞进 iframe。判据与后端自己的 `_probe_own_instance` 完全一致（404/403 也带该头，
/// 所以即便 /api/info 因数据目录问题失败，身份仍可判定）。
///
/// ⚠️ `Server:` 头是**公开且可伪造**的（P1-4）：任何本机进程只要回一行同名头就能
/// 骗过这条判据。所以真正的门是第二道 —— 一次性令牌：壳 spawn 时经环境变量把随机串
/// 交给后端，后端在 `X-CMV-Shell-Token` 里原样回显，壳逐字节比对。抢注者不知道令牌，
/// 也就装不进 iframe。没有令牌（单元测试路径）时退化为只看 Server 头。
fn probe_backend_identity(port: u16) -> bool {
    match SHELL_TOKEN.get() {
        Some(tok) => probe_identity_with(port, tok),
        None => probe_identity_with(port, ""),
    }
}

/// P2-7 的共存探测：只看「这个端口上是不是一台 CMV」，**不**要求令牌 ——
/// 便携版独立 exe / 旧孤儿都不是本次启动 spawn 的，天然没有本次的令牌。
fn probe_is_cmv(port: u16) -> bool {
    let head = probe_head(port);
    head.contains("\r\nserver: mediaviewer/")
}

fn probe_identity_with(port: u16, token: &str) -> bool {
    let head = probe_head(port);
    if !head.contains("\r\nserver: mediaviewer/") {
        return false;
    }
    if token.is_empty() {
        return true;
    }
    head.contains(&format!("\r\n{}: {}", SHELL_TOKEN_HEADER, token))
}

/// 取响应头文本（小写化）；连不上 / 超时返回空串。
fn probe_head(port: u16) -> String {
    // ⚠️ 必须用 connect_timeout，不能用裸 connect：裸 connect 的超时由系统决定
    // （SYN 重传 ~2 秒起、可能到 20 秒），而这条路径在**启动关键路径**上。
    // 本机回环连通只需微秒级，150 ms 已经非常宽松；真的有人在听，
    // 握手一定在这个窗口内完成。
    let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port));
    let mut stream = match TcpStream::connect_timeout(&addr, Duration::from_millis(150)) {
        Ok(s) => s,
        Err(_) => return String::new(),
    };
    let _ = stream.set_read_timeout(Some(Duration::from_millis(PROBE_TIMEOUT_MS)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(PROBE_TIMEOUT_MS)));
    let req = b"GET /api/info HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n";
    if stream.write_all(req).is_err() {
        return String::new();
    }
    let mut head = Vec::new();
    let mut buf = [0u8; 512];
    while head.len() < 2048 {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => {
                // Server 头总在响应前部，见到空行（头结束）就可以停
                let chunk = &buf[..n];
                head.extend_from_slice(chunk);
                if head.windows(4).any(|w| w == b"\r\n\r\n") {
                    break;
                }
            }
            Err(_) => break,
        }
    }
    String::from_utf8_lossy(&head).to_ascii_lowercase()
}

/// 等后端的三种结局。旧实现只回 bool，于是「后端启动即崩」和「后端慢」在
/// 用户那里长得一模一样：都是转圈 60 秒后一句「未就绪」（P1-3）。
enum WaitOutcome {
    Ready,
    /// 子进程自己退出了（附退出码）—— 再等下去也不会有端口
    ChildExited(Option<i32>),
    Timeout,
}

/// 轮询直到后端端口就绪且身份匹配（ε-10 / 7-7：不再只看 TCP 可连）。
///
/// P1-3：每一轮都顺手看一眼子进程。后端因为 bind 失败 / 缺运行库 / 被杀软拦下
/// 而立刻退出时，`try_wait()` 会马上给出退出码，这里立即返回而不是对着空端口
/// 白等满 60 秒（用户看到的差别是「1 秒内报错」和「转圈一分钟」）。
fn wait_backend(children: &Mutex<Option<Child>>, port: u16, timeout_secs: u64) -> WaitOutcome {
    let timeout = Duration::from_secs(timeout_secs);
    let start = Instant::now();
    while start.elapsed() < timeout {
        if probe_backend_identity(port) {
            return WaitOutcome::Ready;
        }
        if let Ok(mut guard) = children.lock() {
            if let Some(c) = guard.as_mut() {
                if let Ok(Some(status)) = c.try_wait() {
                    return WaitOutcome::ChildExited(status.code());
                }
            }
        }
        // 2026-11-05：轮询间隔 300ms → 120ms。后端就绪时刻与「壳发现就绪」之间
        // 原本平均有 150ms 的固定延迟（最坏 300ms），用户感知就是「多等半拍」。
        // 本机回环 probe 一次是微秒级，120ms 对 CPU 毫无压力。
        std::thread::sleep(Duration::from_millis(120));
    }
    WaitOutcome::Timeout
}

/// P2-7：同区间是否已经有一台 CMV 在跑（便携版独立 exe / 上一轮留下的孤儿）。
///
/// 单实例插件只认「同一个 identifier 的壳」，认不出独立版 exe；而两个 PyInstaller
/// onefile 会互相清扫对方的 `_MEI*` 解包目录 ⇒ 其中一个中途崩。壳没法管住独立版，
/// 但至少要在 shell.log 里说清楚，别让用户对着「莫名其妙的 404 / 崩溃」猜。
/// 端口上是否真有监听者（本地 bind 试探，微秒级，不发任何网络包）。
///
/// 2026-10-20：`find_other_cmv` 曾经对 49 个端口直接 `TcpStream::connect`（**无超时**）。
/// 在安全软件 / 防火墙丢弃未知端口 SYN 的机器上，每个空端口要等一个 SYN 重传周期
/// （实测 ~2 秒）⇒ 启动白卡 **100 秒**（壳日志 1.1s → 101s 全耗在这）。
/// 先 bind 探一次就能把「根本没人监听」的端口全部跳过，只有被占的才值得连过去问身份。
fn port_in_use(port: u16) -> bool {
    TcpListener::bind(("127.0.0.1", port)).is_err()
}

fn find_other_cmv(skip: u16) -> Option<u16> {
    (PORT_START..PORT_START.saturating_add(PORT_TRIES))
        .filter(|p| *p != skip)
        .filter(|p| port_in_use(*p))
        .find(|p| probe_is_cmv(*p))
}

/// 按 pid 杀后端进程树（taskkill /T）。PyInstaller onefile 的引导进程是
/// 壳 spawn 的直接子进程，整棵树归它带，/T 可以一并收掉。
/// ε-10 / 7-9：抽出来共用 —— 正常退出（RunEvent::Exit）与 panic hook 同一条路。
///
/// 第 22 级两处加固：
/// - P2-1：杀之前先核对「这个 pid 现在还是不是我的后端」。pid 会被系统回收复用，
///   而 `/F /T` 是整棵树强杀 —— 对着一个复用了的 pid 下手就是杀掉别人的程序。
///   拿不到映像路径（已退出 / 无权限）时按「不是我的」处理，宁可漏杀不错杀。
/// - P1-2：`taskkill` 不再走 `CreateProcess` 的相对搜索（壳 exe 同目录、当前目录
///   都排在 System32 之前 ⇒ 放一个同名 exe 就能借壳执行），改用绝对路径；
///   `status.success()` 必须检查，失败要落日志并让调用方走 `child.kill()` 兜底。
fn kill_backend_tree(pid: u32) -> bool {
    if let Some(want) = BACKEND_EXE.get() {
        match process_image(pid) {
            Some(got) if same_path(&got, want) => {}
            Some(got) => {
                append_shell_log(&format!(
                    "[kill-skip] pid={pid} 的映像是 {}，不是本程序的后端，跳过（防 pid 复用误杀）",
                    got.display()
                ));
                return false;
            }
            None => {
                append_shell_log(&format!(
                    "[kill-skip] pid={pid} 已退出或不可查询，跳过（不猜 pid 去 taskkill）"
                ));
                return false;
            }
        }
    }
    match Command::new(taskkill_path())
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
    {
        Ok(o) if o.status.success() => true,
        Ok(o) => {
            append_shell_log(&format!(
                "[kill-failed] taskkill pid={pid} 失败（退出码 {:?}）",
                o.status.code()
            ));
            false
        }
        Err(e) => {
            append_shell_log(&format!("[kill-failed] taskkill 无法执行：{e}"));
            false
        }
    }
}

/// `%SystemRoot%\System32\taskkill.exe` 的绝对路径。
///
/// 不用裸 `taskkill`：`Command::new` 最终走 `CreateProcessW`，它的搜索顺序是
/// 「应用程序目录 → 当前目录 → System32 → …」——**在 System32 之前**，
/// 也就是说壳 exe 旁边或工作目录里放一个 taskkill.exe，就能被壳执行（P1-2）。
fn taskkill_path() -> PathBuf {
    match std::env::var("SystemRoot") {
        Ok(root) => PathBuf::from(root).join("System32").join("taskkill.exe"),
        Err(_) => PathBuf::from("taskkill.exe"),
    }
}

/// 查进程映像的完整路径。查询失败（进程已退出 / 权限不足）返回 None。
fn process_image(pid: u32) -> Option<PathBuf> {
    unsafe {
        let h = win32::OpenProcess(win32::PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if h.is_null() {
            return None;
        }
        let mut buf = [0u16; 1024];
        let mut n = buf.len() as u32;
        let ok = win32::QueryFullProcessImageNameW(h, 0, buf.as_mut_ptr(), &mut n);
        let _ = win32::CloseHandle(h);
        if ok == 0 || n == 0 {
            return None;
        }
        Some(PathBuf::from(String::from_utf16_lossy(
            &buf[..n as usize],
        )))
    }
}

/// 把路径折成「同一个文件的可比形式」—— 只在 `canonicalize` 拿不到时兜底用。
///
/// 需要抹平的差异（都真出现过，或一定会出现）：
///   · 大小写：Windows 不区分（`C:\Users` / `c:\users`）
///   · 分隔符：`\` 与 `/` 等价
///   · `\\?\`、`\\?\UNC\` 前缀（长路径与会话命名空间）
///   · 末尾分隔符、`.` 与 `..` 段
///   · 8.3 短名（`PROGRA~1`）—— **这条本条函数处理不了**，只能靠 `canonicalize`；
///     所以它只是兜底，不是主判据。
///
/// 只折叠词法、不碰磁盘：调用点可能拿到一个「已经不存在」的路径（旧后端被换掉），
/// 那里任何 I/O 都会失败，不能把「查不到」当成「不是同一个」。
fn norm_path_text(s: &str) -> String {
    let t = s.replace('/', "\\");
    // `\\?\UNC\srv\share` → `\\srv\share`；`\\?\C:\x` → `C:\x`
    let t = if let Some(rest) = t.strip_prefix(r"\\?\UNC\") {
        format!(r"\\{}", rest)
    } else if let Some(rest) = t.strip_prefix(r"\\?\") {
        rest.to_string()
    } else {
        t
    };
    let abs = t.starts_with('\\');
    let unc = t.starts_with(r"\\");          // UNC 根 `\\srv\share` 要保住两个前导反斜杠
    let mut parts: Vec<&str> = Vec::new();
    for seg in t.split('\\') {
        match seg {
            "" | "." => {}                       // 连续分隔符 / 当前目录
            ".." => {
                if parts.last().map_or(false, |x| *x != "..") {
                    parts.pop();
                } else if !abs {
                    parts.push("..");            // 相对路径的 `..` 要留着
                }
            }
            x => parts.push(x),
        }
    }
    let mut out = String::new();
    if unc {
        out.push_str(r"\\");                     // UNC 根保住两个前导反斜杠
    } else if abs {
        out.push('\\');
    }
    out.push_str(&parts.join("\\"));
    if out.len() == 2 && out.ends_with(':') {   // 卷根 `C:` → `C:\`
        out.push('\\');
    }
    out.make_ascii_lowercase();
    out
}

/// 判断两个路径是不是**同一个文件**。
///
/// X6（2026-10-28）：原来只有 `eq_ignore_ascii_case`。`want` 来自 `%LOCALAPPDATA%`
/// 拼接、`got` 来自 `QueryFullProcessImageNameW`，两条路径只要有一处写法不同
/// （junction / 8.3 短名 / `\\?\` / `..`），比较就为假 ⇒ `kill_backend_tree` 跳过杀进程
/// ⇒ 孤儿后端占着 backend exe ⇒ 换版时报「部署后端失败」，用户侧只看到「双击没反应」。
///
/// 先走 `canonicalize`（它按文件句柄取最终路径，能解开 junction/符号链接/短名），
/// 两侧都成功才用它；任一失败（路径已删、被独占、无权限）就退回词法折叠 ——
/// **绝不因为「查不到」就把两个路径判成不同**，那会让本来能杀掉的进程漏杀。
fn same_path(a: &Path, b: &Path) -> bool {
    if let (Ok(ca), Ok(cb)) = (std::fs::canonicalize(a), std::fs::canonicalize(b)) {
        return norm_path_text(&ca.to_string_lossy()) == norm_path_text(&cb.to_string_lossy());
    }
    norm_path_text(&a.to_string_lossy()) == norm_path_text(&b.to_string_lossy())
}

/// 从 `/api/info` 的响应体里取后端自报的 pid（形如 `"pid": 12345`）。
///
/// 解析刻意做得「宽松但严格」：只认 `"pid"` 后紧跟的十进制数字；任何一处不匹配都返回
/// None（例如后端版本较旧、没有 pid 字段）——调用方据此走「取不到就不杀」的保守分支，
/// 绝不猜一个 pid 去 taskkill（猜错就是误杀别人的进程）。
fn parse_pid_from_info(body: &str) -> Option<u32> {
    let i = body.find("\"pid\"")?;
    let rest = &body[i + "\"pid\"".len()..];
    let rest = rest.trim_start().strip_prefix(':')?;
    let rest = rest.trim_start();
    let digits: String = rest.chars().take_while(|c| c.is_ascii_digit()).collect();
    if digits.is_empty() {
        return None;
    }
    digits.parse::<u32>().ok()
}

/// P0-11：向后端问它自己的 pid（`GET /api/info`，取 `pid` 字段）。
fn query_backend_pid(port: u16) -> Option<u32> {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).ok()?;
    let _ = stream.set_read_timeout(Some(Duration::from_millis(PROBE_TIMEOUT_MS)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(PROBE_TIMEOUT_MS)));
    if stream
        .write_all(b"GET /api/info HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        .is_err()
    {
        return None;
    }
    let mut body = Vec::new();
    let mut buf = [0u8; 2048];
    while body.len() < 65536 {
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => body.extend_from_slice(&buf[..n]),
            Err(_) => break,
        }
    }
    parse_pid_from_info(&String::from_utf8_lossy(&body))
}

/// P0-11 的退出兜底：端口上仍有 CMV 服务（后端自重启接管了新进程）时，按端口反查
/// 真实 pid 再杀树。
///
/// 为什么必须有这一步：壳内「重启程序」起的新后端虽然也是普通子进程（见后端
/// `restart_self` 的 P1-35），但它的父进程（旧后端）随后 `os._exit(0)` 走了 ——
/// 壳登记的 pid 成了死号，`taskkill /PID <死号>` 必然失败且错误被吞。结果是新后端
/// 常驻成孤儿、并一直锁着 `%LOCALAPPDATA%\...\backend\ComfyMediaViewer.exe`，
/// 下次换版时 `remove_file` 报「部署后端失败」，用户以为打包坏了。
fn kill_backend_by_port() {
    let Some(&port) = BACKEND_PORT.get() else {
        return;
    };
    if !probe_backend_identity(port) {
        return; // 端口上没有 CMV 服务 ⇒ 后端已随子进程树一起退出，无需兜底
    }
    match query_backend_pid(port) {
        Some(pid) => {
            let ok = kill_backend_tree(pid);
            append_shell_log(&format!(
                "[exit] 兜底：端口 {port} 上仍是 CMV 服务（后端自重启接管），按端口反查 pid={pid} 并杀树（{}）",
                if ok { "成功" } else { "未成功，见上一条 kill-skip/kill-failed" }
            ));
        }
        None => append_shell_log(&format!(
            "[exit] 端口 {port} 上仍有 CMV 服务，但未能取到 pid（后端可能过旧），无法兜底杀进程"
        )),
    }
}

/// ε-10 / 7-9：后端死透（进程退出且端口上没有 CMV 服务）时的壳页提示。
///
/// 此时启动页已收起、iframe 里是应用页面 —— 在壳顶层注入一条置顶横幅
/// （壳页与 iframe 跨源，应用侧 JS 死不了也改不了这条横幅；Tauri 顶层 eval 可靠）。
/// 同时落盘 shell.log，保证「后端是什么时候死的」事后可查。
fn notify_backend_down(win: &WebviewWindow, port: u16) {
    append_shell_log(&format!(
        "[backend-down] 后端进程已退出且端口 {port} 上无 CMV 服务，已在壳页提示用户重启"
    ));
    let dom = "(function(){ \
        if (document.getElementById('backendDownBanner')) return; \
        var b = document.createElement('div'); \
        b.id = 'backendDownBanner'; \
        b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;\
background:#b3261e;color:#fff;font:13px/1.6 system-ui,sans-serif;\
padding:8px 16px;text-align:center;box-sizing:border-box;'; \
        b.textContent = '\\u26a0 后端进程已退出，页面功能不可用 \\u2014\\u2014 请重启程序'; \
        (document.body || document.documentElement).appendChild(b); \
    })()";
    let _ = win.eval(dom);
}

/// 在启动页上报告错误（启动页始终可访问，用 eval 注入文本）。
///
/// P1-36：旧实现 `window.__bootError && window.__bootError('…')` 会**短路吞掉**消息，
/// 而且 `eval` 的返回值被 `let _ =` 丢掉 ⇒ 调用方**永远不知道消息没送出去**。
/// spawn_backend 失败发生在 Builder 之前，此时壳页脚本可能尚未解析，
/// `window.__bootError` 还是 undefined ⇒ 消息永久丢失，用户只看到遮罩永久转圈。
///
/// 现在：① 轮询等待 `__bootError` 就绪（最多 ~5s）再调；② 若始终就绪不了，
/// 退化为**直接改 DOM** 把错误写到启动页上（不依赖任何脚本）；③ 把结果记进
/// `SHELL_ERR` 并落盘一份日志，保证「消息有没有送到」可查。
fn report_splash_error(win: &WebviewWindow, msg: &str) {
    STARTUP_STATUS.lock().unwrap_or_else(|e| e.into_inner()).error = Some(msg.to_string());
    let esc = js_single_quoted(msg);
    // 重试投递：壳页可能尚未解析完，eval 会失败；最多重试约 5s
    //
    // A18（2026-11-05 审计复核）：`win.eval()` 的 `Result` 只表示「这段脚本被接受
    // 执行」，**不表示 DOM 真的被改到了**。旧脚本里每个 `if (b)` / `if (e)` 都可以
    // 全部落空，末尾照样 `return 'OK'` ⇒ 首次即 break，用户屏幕上什么都没有。
    // 拿不到脚本返回值（Tauri 的 eval 只回 `()`），所以改成**不依赖壳页结构**：
    // 找不到 `#bootErr` 就自己建一个挂上去，错误至少一定能显示出来。
    for _ in 0..50 {
        let dom = format!(
            "(() => {{ \
                 var b = document.getElementById('boot'); \
                 if (b) b.classList.add('failed'); \
                 var e = document.getElementById('bootErr'); \
                 if (!e) {{ \
                     e = document.createElement('div'); \
                     e.id = 'bootErr'; \
                     e.style.cssText = 'margin:18px auto 0;max-width:560px;padding:14px 18px;border-radius:10px;background:rgba(220,60,60,.14);color:#ffb4b4;font:13px/1.6 ui-monospace,monospace;white-space:pre-wrap;word-break:break-word;text-align:left;'; \
                     var host = document.getElementById('boot') || document.body; \
                     if (host) host.appendChild(e); \
                 }} \
                 if (e) {{ e.textContent = '{}'; e.classList.add('show'); }} \
                 var t = document.getElementById('bootStage'); \
                 if (t) t.textContent = '启动失败'; \
                 return (e && e.textContent) ? 'OK' : 'NO-TARGET'; }})()",
            esc
        );
        if win.eval(&dom).is_ok() {
            break;
        }
        std::thread::sleep(Duration::from_millis(100));
    }
    // 落盘留痕，便于事后核对「消息有没有送到」；界面报错同样算「没顺利启动」
    append_shell_log(&format!("[splash-error] {msg}"));
    dump_boot_log(msg);
}

/// 把任意文本塞进 JS **单引号**字符串字面量（P2-4）。
///
/// 只转义 `\` 和 `'` 是不够的：裸换行（`\n` / `\r`）在单引号字面量里是语法错误，
/// U+2028 / U+2029 在旧解析器里也算行终止符 ⇒ `win.eval` 直接返回 Err，
/// 重试 50 次（5 秒）后放弃，用户只见遮罩一直转 —— 正是 P1-36 想根治的症状复发。
/// 现在按白名单走：控制字符与行分隔符一律转成 `\uXXXX`。
fn js_single_quoted(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    for ch in s.chars() {
        match ch {
            '\\' => out.push_str("\\\\"),
            '\'' => out.push_str("\\'"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{2028}' => out.push_str("\\u2028"),
            '\u{2029}' => out.push_str("\\u2029"),
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\u{:04x}", c as u32))
            }
            c => out.push(c),
        }
    }
    out
}

/// 壳自己的日志（P1-36 配套）。
///
/// 落盘策略（2026-11-10 改「正常启动也会在 exe 旁留文件」）：
///   · 本次启动的日志先在内存里攒一份尾部（`BOOT_TAIL`），几十行，开销可忽略；
///   · 常驻那份**只写** `%LOCALAPPDATA%\ComfyMediaViewer\shell.log`，超过 256 KB 轮转一代；
///   · 程序旁的 `ComfyMediaViewer-startup.log` **只在不顺利时**由 `dump_boot_log()` 写出 ——
///     便携 exe 旁边不该在正常启动后凭空多一个文件，但白屏时又必须留得下证据。
const SHELL_LOG_MAX: u64 = 256 * 1024;
const BOOT_TAIL_MAX: usize = 400;

static BOOT_TAIL: std::sync::Mutex<Vec<String>> = std::sync::Mutex::new(Vec::new());
static BOOT_DUMPED: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

fn shell_log_path() -> Option<PathBuf> {
    let base = std::env::var_os("LOCALAPPDATA")?;
    let dir = PathBuf::from(base).join("ComfyMediaViewer");
    let _ = std::fs::create_dir_all(&dir);
    Some(dir.join("shell.log"))
}

/// 超过上限就把 shell.log 挪成 shell.log.1（只留一代，旧的直接覆盖）。
/// 不这么做的话，每双击一次都追几行，一年就是几十 MB，用户还找不到在哪。
fn rotate_shell_log(path: &std::path::Path) {
    let too_big = std::fs::metadata(path).map(|m| m.len() > SHELL_LOG_MAX).unwrap_or(false);
    if !too_big {
        return;
    }
    let bak = path.with_extension("log.1");
    let _ = std::fs::remove_file(&bak);
    let _ = std::fs::rename(path, &bak);
}

fn append_shell_log(line: &str) {
    if let Ok(mut tail) = BOOT_TAIL.lock() {
        if tail.len() >= BOOT_TAIL_MAX {
            tail.remove(0);
        }
        tail.push(line.to_string());
    }
    let Some(path) = shell_log_path() else { return };
    rotate_shell_log(&path);
    let timestamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs()).unwrap_or(0);
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&path) {
        let _ = writeln!(f, "[unix={timestamp} pid={}] {line}", std::process::id());
    }
}

/// 把**本次启动**的完整日志写到程序旁，只在真出事时调。
///
/// 幂等：一个进程只写一次（首个失败点最有信息量，后面的多半是它的连锁反应）。
/// 程序目录不可写（Program Files / 只读介质）时不留空文件，把线索转到 AppData 那份。
fn dump_boot_log(reason: &str) {
    use std::sync::atomic::Ordering;
    if BOOT_DUMPED.swap(true, Ordering::SeqCst) {
        return;
    }
    let Some(dir) = app_dir() else { return };
    let path = dir.join("ComfyMediaViewer-startup.log");
    let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(&path) else {
        append_shell_log(&format!(
            "[boot-log] 程序目录不可写，完整启动日志见 shell.log（原因：{reason}）"
        ));
        return;
    };
    let timestamp = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs()).unwrap_or(0);
    let _ = writeln!(
        f,
        "\n==== unix={timestamp} pid={} 启动未顺利完成：{reason} ====",
        std::process::id()
    );
    if let Ok(tail) = BOOT_TAIL.lock() {
        for l in tail.iter() {
            let _ = writeln!(f, "{l}");
        }
    }
}

/// 进程起点。启动慢的排查必须能分段落账，否则「壳加载 / 释放后端 / 后端自启 /
/// 等就绪」四段在日志里是同一个黑盒（2026-10-20 实测吃过这个亏）。
static BOOT_T0: std::sync::OnceLock<std::time::Instant> = std::sync::OnceLock::new();

fn boot_mark() -> u128 {
    BOOT_T0.get_or_init(std::time::Instant::now).elapsed().as_millis()
}

/// 打一行启动阶段日志（带毫秒偏移）。只在启动路径上用，开销可忽略。
fn trace(stage: &str) {
    append_shell_log(&format!("[boot] +{}ms {}", boot_mark(), stage));
}

fn main() {
    // 启动计时起点。放在第一行：后面每一段都要能对上账。
    let _ = BOOT_T0.set(std::time::Instant::now());
    trace(&format!("main 进入，pid={}，production={}", std::process::id(), !tauri::is_dev()));
    trace(&format!("exe={:?}; LOCALAPPDATA={:?}", std::env::current_exe(), std::env::var_os("LOCALAPPDATA")));

    if let Err(error) = check_startup_storage() {
        append_shell_log(&format!("[startup-storage-error] {error}"));
        native_startup_error(&error);
        std::process::exit(1);
    }
    // P1-4：本次启动的一次性令牌。必须在任何 spawn / 探测之前就位 —— 探测要拿它
    // 核对后端回显，spawn 要把它交给后端。
    let _ = SHELL_TOKEN.set(new_token());

    // ε-10 / 7-9：panic 路径清理。unwind 不走 RunEvent::Exit，后端会成孤儿；
    // hook 里拿不到 Tauri state，用 BACKEND_PID 全局槽兜底（未 spawn 过则 no-op）。
    // 注意：panic 期间再 spawn taskkill 在 Windows 上可行（纯 CreateProcess）。
    std::panic::set_hook(Box::new(|info| {
        append_shell_log(&format!("[panic] {info}"));
        if let Some(pid) = BACKEND_PID.get() {
            let _ = kill_backend_tree(*pid);
        }
        // P0-11：panic 前若发生过「后端自重启接管」，登记的 pid 已失效 ——
        // 同样按端口兜底，避免带一个孤儿后端一起崩掉。
        kill_backend_by_port();
        native_startup_error(&format!("桌面初始化异常：{info}"));
    }));

    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![desktop_startup_state, desktop_frontend_loaded])
        // ε-10 / 7-6：单实例保护。⚠️ 必须是第一个注册的插件 —— 第二实例在插件
        // init 阶段（build 内）就检测到已有实例并 exit(0)，此时 setup 还没跑、
        // 后端尚未 spawn ⇒ 不会出现两个壳各起一份后端、写同一份 cmv-data。
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            trace("收到重复启动请求，激活已有窗口");
            // 又双击了一次：把已有窗口带到前台，而不是让用户面对「点了没反应」
            if let Some(w) = app.get_webview_window("main") {
                let _ = w.show();
                let _ = w.unminimize();
                let _ = w.set_focus();
            }
        }))
        .manage(BackendProc(Mutex::new(None)))
        .setup(move |app| {
            trace("setup 进入");
            let profile = app.path().app_local_data_dir()?;
            trace(&format!("window: 开始创建 WebView，缓存目录={}", profile.display()));
            let config = app.config().app.windows.iter().find(|w| w.label == "main")
                .ok_or("main window config not found")?.clone();
            let win = tauri::WebviewWindowBuilder::from_config(app, &config)?
                .data_directory(profile)
                .on_page_load(|_, payload| {
                    trace(&format!("webview: {:?} {}", payload.event(), payload.url()));
                })
                .build()?;
            trace("window: 主窗口创建完成");
            let handle = app.handle().clone();

            // 后台线程：端口选择 → spawn → 等就绪 → 导航 → 持续监控，不阻塞 UI 线程
            std::thread::spawn(move || {
                trace("后台线程启动");
                // ε-10 / 7-7：端口全忙直接报错（旧实现回退 PORT_START 必然白等）
                let Some(port) = pick_port() else {
                    report_splash_error(
                        &win,
                        &format!("端口 {PORT_START} 起连续 {PORT_TRIES} 个全部被占用，无法启动后端"),
                    );
                    return;
                };
                // P0-11：登记端口。退出时若端口上仍有 CMV 服务（后端自重启接管），
                // 壳登记的 pid 已失效，只能靠这个端口反查新后端 pid 再杀。
                let _ = BACKEND_PORT.set(port);
                trace(&format!("端口选定 {port}"));
                // P2-7：同区间已有别的 CMV 在跑（便携版独立 exe / 旧孤儿）时先留痕。
                // 两个 onefile 会互相清理解包目录，出了事至少日志里能对上号。
                if let Some(other) = find_other_cmv(port) {
                    append_shell_log(&format!(
                        "[instance] 端口 {other} 上已有另一个 CMV 后端（可能是便携版独立程序或上一轮残留），\
                         两个 onefile 实例会互相清理解包目录，建议只保留一个"
                    ));
                }
                // ε-10 / 7-6：spawn 已被单实例闸门护住（在 plugin init 之后才发生）
                let child = match spawn_backend(port) {
                    Ok(c) => c,
                    Err(e) => {
                        report_splash_error(&win, &e);
                        return;
                    }
                };
                let _ = BACKEND_PID.set(child.id());
                let state = handle.state::<BackendProc>();
                match state.0.lock() {
                    Ok(mut g) => *g = Some(child),
                    Err(_) => return,
                }

                match wait_backend(&state.0, port, BACKEND_TIMEOUT) {
                    WaitOutcome::Ready => {
                        trace("后端就绪，开始导航 iframe");
                        STARTUP_STATUS.lock().unwrap_or_else(|e| e.into_inner()).url =
                            Some(format!("http://127.0.0.1:{port}"));
                        // 后端就绪后把应用加载进标题栏下方的 iframe（保持 Tauri 顶层页，IPC 可靠）。
                        // P2-3：可信 origin 由**壳**定（`__cmvOrigin`），不能让壳页自己从
                        // `frame.src` 反推 —— 那样「期望值」和「实际值」同源同变，闸门等于没闸。
                        // 两条赋值放同一句 eval 里，保证先有常量再导航（顺序不会被打乱）。
                        // 壳页主动读取持久启动状态，避免一次性 eval 早于 DOM 就绪而丢失。
                    }
                    // P1-3：后端起来了又立刻死（bind 失败 / 缺运行库 / 被杀软拦）——
                    // 立刻报，并把退出码与日志位置写清楚，不再白等满 60 秒。
                    WaitOutcome::ChildExited(code) => {
                        report_splash_error(
                            &win,
                            &format!(
                                "后端进程启动后立即退出（退出码 {}），端口 {port} 未就绪。\
                                 详细原因见 %LOCALAPPDATA%\\ComfyMediaViewer\\backend\\cmv-data\\logs\\cmv.log",
                                code.map(|c| c.to_string()).unwrap_or_else(|| "未知".into())
                            ),
                        );
                        return;
                    }
                    WaitOutcome::Timeout => {
                        report_splash_error(
                            &win,
                            &format!("后端在 {BACKEND_TIMEOUT} 秒内未就绪（端口 {port}）"),
                        );
                        return;
                    }
                }

                // ε-10 / 7-9：就绪 ≠ 一劳永逸 —— 持续监控后端存活。
                // try_wait 短暂持锁，不与 RunEvent::Exit 的清理互锁。
                //
                // P0-1（第 22 级补）：旧实现在「进程退出但端口仍有服务」时直接 return，
                // 监控线程就此终结 —— 自重启后的新后端再崩，壳页不会再弹横幅。
                // 现在只是**换判据**：子进程没了就改看端口，端口也没了才判定后端死亡。
                let mut child_dead = false;
                loop {
                    std::thread::sleep(Duration::from_millis(MONITOR_POLL_MS));
                    if child_dead {
                        if !probe_backend_identity(port) {
                            notify_backend_down(&win, port);
                            return;
                        }
                        continue;
                    }
                    let exited = {
                        let st = handle.state::<BackendProc>();
                        let mut guard = match st.0.lock() {
                            Ok(g) => g,
                            Err(_) => return,
                        };
                        match guard.as_mut() {
                            None => return, // 句柄已被清走（理论不可达，兜底）
                            Some(c) => match c.try_wait() {
                                Ok(Some(status)) => {
                                    append_shell_log(&format!("[backend-exit] pid={} status={status} code={:?}", c.id(), status.code()));
                                    true
                                },
                                Ok(None) => false,
                                Err(error) => {
                                    append_shell_log(&format!("[backend-wait-error] pid={} {error}", c.id()));
                                    true
                                },
                            },
                        }
                    };
                    if exited {
                        // P1-35：壳内「重启程序」= 后端自己起新进程后退出（同端口接管）。
                        // 此时端口上仍有 CMV 服务 ⇒ 静默记录，改为按端口继续监控。
                        if probe_backend_identity(port) {
                            append_shell_log(&format!(
                                "[backend-monitor] 后端进程退出，但端口 {port} 上仍有 CMV 服务（自重启接管），转入按端口监控"
                            ));
                            child_dead = true;
                        } else {
                            notify_backend_down(&win, port);
                            return;
                        }
                    }
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .unwrap_or_else(|error| {
            append_shell_log(&format!("[build-error] {error}"));
            native_startup_error(&format!("无法创建桌面窗口：{error}"));
            std::process::exit(1);
        })
        .run(|app, event| {
            match &event {
                RunEvent::ExitRequested { code, .. } => append_shell_log(&format!("[exit-requested] code={code:?}")),
                RunEvent::WindowEvent { label, event: tauri::WindowEvent::CloseRequested { .. }, .. } =>
                    append_shell_log(&format!("[window-close-requested] {label}")),
                RunEvent::WindowEvent { label, event: tauri::WindowEvent::Destroyed, .. } =>
                    append_shell_log(&format!("[window-destroyed] {label}")),
                _ => {},
            }
            if let RunEvent::Exit = event {
                append_shell_log("[exit] 桌面事件循环退出，开始清理后端");
                // 退出时终止后端进程树，避免孤儿进程
                // （PyInstaller onefile 引导进程会 fork 出真正的服务进程，
                //   直接 kill 只能杀掉引导进程，须 taskkill /T 杀整棵树）
                let state = app.state::<BackendProc>();
                let mut guard = match state.0.lock() {
                    Ok(g) => g,
                    Err(_) => {
                        // 锁中毒：跳过「按登记 pid 杀」，但仍走端口兜底（兜底不碰这把锁）
                        kill_backend_by_port();
                        return;
                    }
                };
                if let Some(child) = guard.as_mut() {
                    // P2-1：先看它是不是还活着。已经退出的子进程，它的 pid 可能早已被
                    // 系统回收分配给别的程序 —— 这时候 `/T /F` 就是拿别人的进程树开刀。
                    let alive = matches!(child.try_wait(), Ok(None));
                    if alive {
                        let pid = child.id();
                        // P1-2：taskkill 失败时用 Child::kill() 兜底（句柄现成，不依赖
                        // 外部 exe 是否可执行），两条都失败才落日志认账。
                        if !kill_backend_tree(pid) {
                            match child.kill() {
                                Ok(_) => append_shell_log(&format!(
                                    "[exit] taskkill pid={pid} 失败，已用 child.kill() 兜底"
                                )),
                                Err(e) => append_shell_log(&format!(
                                    "[exit] 后端 pid={pid} 两条清理路径都失败：{e}"
                                )),
                            }
                        }
                    }
                    let _ = child.wait();
                }
                drop(guard); // 先放锁，再走兜底（兜底要发 HTTP，别阻塞监控线程）
                // P0-11：壳内「重启程序」后后端已换进程，上面杀的是**死号**。若端口上
                // 仍有 CMV 服务，按端口反查真实 pid 再杀，避免新后端常驻成孤儿。
                kill_backend_by_port();
            }
        });
}


#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn startup_probe_preserves_existing_files() {
        let root = std::env::temp_dir().join(format!("cmv_probe_{}", new_token()));
        std::fs::create_dir_all(&root).unwrap();
        let existing = root.join("existing.txt");
        std::fs::write(&existing, b"keep").unwrap();
        probe_startup_directory(&root).unwrap();
        assert_eq!(std::fs::read(&existing).unwrap(), b"keep");
        assert_eq!(std::fs::read_dir(&root).unwrap().count(), 1);
        std::fs::remove_file(existing).unwrap();
        std::fs::remove_dir(root).unwrap();
    }

    #[test]
    fn startup_probe_reports_unwritable_destination() {
        let file = std::env::temp_dir().join(format!("cmv_probe_file_{}", new_token()));
        std::fs::write(&file, b"keep").unwrap();
        assert!(probe_startup_directory(&file).is_err());
        assert_eq!(std::fs::read(&file).unwrap(), b"keep");
        std::fs::remove_file(file).unwrap();
    }

    #[test]
    fn missing_needs_write() {
        assert!(!backend_is_current(None, b"abcd"));
    }

    #[test]
    fn identical_skips() {
        assert!(backend_is_current(Some(b"abcd"), b"abcd"));
    }

    /// 这条就是修掉的那个坑：长度一样、内容不同。
    /// 旧判据（比长度）会把它判成「已是最新」，于是永远不更新。
    #[test]
    fn same_len_different_content_needs_write() {
        assert!(!backend_is_current(Some(b"abcd"), b"abce"));
    }

    /// ε-10 / 7-7：端口全忙必须返回 None（旧实现回退 PORT_START = 明知被占还用）。
    #[test]
    fn pick_port_returns_none_when_all_busy() {
        // 占住整条探测链上**当前空闲**的端口；bind 失败的端口说明已被系统真实服务
        // 占用（本机 8765..8814 区间不一定干净）—— 那个端口本来就不空闲，
        // 跳过即可。这里绝不能 unwrap：测试断言的是 pick_port 的行为，不是机器干净。
        let listeners: Vec<TcpListener> = (0..PORT_TRIES)
            .filter_map(|i| TcpListener::bind(("127.0.0.1", PORT_START + i)).ok())
            .collect();
        assert!(pick_port().is_none());
        drop(listeners);
    }

    /// ε-10 / 7-7：没有监听者的端口身份探测必须为假。
    #[test]
    fn probe_fails_on_dead_port() {
        // 找一个当前空闲端口（bind 探测后立即 drop）
        let l = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let p = l.local_addr().unwrap().port();
        drop(l);
        assert!(!probe_backend_identity(p));
    }

    /// ε-10 / 7-7：身份判据认的是 `Server: MediaViewer/` 头（大小写不敏感）。
    #[test]
    fn probe_identity_matches_server_header() {
        let l = TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let p = l.local_addr().unwrap().port();
        std::thread::spawn(move || {
            if let Ok((mut s, _)) = l.accept() {
                let mut buf = [0u8; 256];
                let _ = s.read(&mut buf);
                let _ = s.write_all(
                    b"HTTP/1.0 200 OK\r\nServer: MediaViewer/2.12.7\r\n\r\n{}",
                );
            }
        });
        assert!(probe_backend_identity(p));
    }

    /// P0-11：`/api/info` 的 pid 解析必须「宽松但严格」—— 认得出真实响应里的
    /// `"pid": N`（含 JSON 的空格），也必须在字段缺失 / 畸形时返回 None。
    /// 这条判据的意义：取不到 pid 时调用方走「不杀」的保守分支，
    /// **宁可不杀，也绝不猜一个 pid 去 taskkill（猜错就是误杀别人的进程）**。
    #[test]
    fn parse_pid_from_info_ok_and_none() {
        assert_eq!(
            parse_pid_from_info(r#"{"root":"C:\\out","has_pil":true,"pid": 4321,"title":"t"}"#),
            Some(4321)
        );
        assert_eq!(parse_pid_from_info(r#"{"pid":7}"#), Some(7));
        assert_eq!(parse_pid_from_info(r#"{"version":"2.12.9"}"#), None);
        assert_eq!(parse_pid_from_info(""), None);
        assert_eq!(parse_pid_from_info(r#"{"pid":"abc"}"#), None);
    }

    /// P1-4：只有 `Server:` 头、没有（或错了）令牌的响应不算数 ——
    /// 这条判据就是「端口抢注装不进 iframe」的全部依据。
    #[test]
    fn probe_requires_token_when_configured() {
        fn serve(headers: &'static str) -> u16 {
            let l = TcpListener::bind(("127.0.0.1", 0)).unwrap();
            let p = l.local_addr().unwrap().port();
            std::thread::spawn(move || {
                if let Ok((mut s, _)) = l.accept() {
                    let mut buf = [0u8; 256];
                    let _ = s.read(&mut buf);
                    let _ = s.write_all(headers.as_bytes());
                }
            });
            p
        }
        let with_tok = serve(
            "HTTP/1.0 200 OK\r\nServer: MediaViewer/2.12.9\r\nX-CMV-Shell-Token: a1b2c3\r\n\r\n{}",
        );
        assert!(probe_identity_with(with_tok, "a1b2c3"));
        // 错误的令牌 ⇒ 不认（每个假服务只 accept 一次，故每次探测都要新起一个）
        let with_tok2 = serve(
            "HTTP/1.0 200 OK\r\nServer: MediaViewer/2.12.9\r\nX-CMV-Shell-Token: a1b2c3\r\n\r\n{}",
        );
        assert!(!probe_identity_with(with_tok2, "deadbeef"));

        let no_tok_resp = "HTTP/1.0 200 OK\r\nServer: MediaViewer/2.12.9\r\n\r\n{}";
        assert!(!probe_identity_with(serve(no_tok_resp), "a1b2c3"));
        // 没有令牌要求（单元测试 / 独立运行）时只看 Server 头
        assert!(probe_identity_with(serve(no_tok_resp), ""));
    }

    /// P2-4：错误文案里的换行 / U+2028 必须被转义，否则 `win.eval` 里是语法错误，
    /// 启动失败提示会静默丢失（用户只看到遮罩一直转）。
    #[test]
    fn js_escape_handles_newlines() {
        let s = js_single_quoted("a\nb\r'c'\\d\u{2028}e");
        assert!(!s.contains('\n') && !s.contains('\r'));
        assert!(!s.contains('\u{2028}'));
        assert_eq!(s, "a\\nb\\r\\'c\\'\\\\d\\u2028e");
    }

    /// P1-1：Job Object 建得出来，且 `SetInformationJobObject` 认我们的结构体尺寸
    /// （cbJobObjectInformationLength 不对时该 API 直接失败）。
    #[test]
    fn kill_on_close_job_is_created() {
        let job = create_kill_on_close_job().expect("Job Object 创建失败");
        unsafe {
            let _ = win32::CloseHandle(job as win32::Handle);
        }
    }

    /// P2-1：杀进程前的归属校验依赖 `process_image` —— 至少要对**当前进程**查得出
    /// 自己的 exe，且路径比较不吃大小写。
    #[test]
    fn process_image_matches_self() {
        let me = process_image(std::process::id()).expect("查不到自身映像路径");
        let exe = std::env::current_exe().unwrap();
        assert!(same_path(&me, &exe), "me={} exe={}", me.display(), exe.display());
    }

    // ---------- X6：路径身份判定（2026-10-28） ----------
    // 这组用例守的是「别把同一个文件判成两个」：漏判的后果是孤儿后端占着 exe、
    // 换版时报「部署后端失败」，用户只看到「双击没反应」。

    /// 纯词法折叠：大小写 / 分隔符 / `\\?\` 前缀 / `.` `..` / 末尾分隔符。
    #[test]
    fn norm_path_text_folds_common_spellings() {
        assert_eq!(norm_path_text(r"C:\Users\X\App.exe"), r"c:\users\x\app.exe");
        assert_eq!(norm_path_text(r"C:/Users/X/App.exe"), r"c:\users\x\app.exe");
        assert_eq!(norm_path_text(r"C:\Users\X\App.exe\\"), r"c:\users\x\app.exe");
        assert_eq!(norm_path_text(r"C:\Users\X\.\App.exe"), r"c:\users\x\app.exe");
        assert_eq!(norm_path_text(r"C:\Users\Y\..\X\App.exe"), r"c:\users\x\app.exe");
        assert_eq!(norm_path_text(r"\\?\C:\Users\X\App.exe"), r"c:\users\x\app.exe");
        assert_eq!(norm_path_text(r"\\?\UNC\srv\share\App.exe"), r"\\srv\share\app.exe");
        // 卷根不能被削成 `c:`
        assert_eq!(norm_path_text(r"C:\"), r"c:\");
        // 相对路径的 `..` 要保留（否则 `a\..\..\b` 会被折成 `b`）
        assert_eq!(norm_path_text(r"a\..\..\b"), r"..\b");
    }

    /// 两个路径**都不存在**时 canonicalize 必失败 ⇒ 走词法兜底，仍要判对。
    /// （真实场景：旧后端已被换掉，`want` 是个已删除的路径。）
    #[test]
    fn same_path_lexical_fallback_on_missing_paths() {
        let a = Path::new(r"C:\cmv-not-exist-7f3a\backend\ComfyMediaViewer.exe");
        assert!(same_path(a, Path::new(r"c:/cmv-not-exist-7f3a/BACKEND/ComfyMediaViewer.exe")));
        assert!(same_path(a, Path::new(r"\\?\C:\cmv-not-exist-7f3a\backend\ComfyMediaViewer.exe")));
        assert!(same_path(a, Path::new(r"C:\cmv-not-exist-7f3a\other\..\backend\ComfyMediaViewer.exe")));
        assert!(!same_path(a, Path::new(r"C:\cmv-not-exist-7f3a\backend\Other.exe")));
        // 只有前缀相似：不能被「去掉 \\?\ 后前缀匹配」骗过去
        assert!(!same_path(a, Path::new(r"C:\cmv-not-exist-7f3a\backend\ComfyMediaViewer.exe.bak")));
    }

    /// 真文件：`..` 写法与大小写写法都要判成同一个。
    #[test]
    fn same_path_real_file_with_dot_segments() {
        let root = std::env::temp_dir().join(format!("cmv_sp_{}_a", std::process::id()));
        let sub = root.join("sub");
        std::fs::create_dir_all(&sub).unwrap();
        let f = root.join("app.exe");
        std::fs::write(&f, b"x").unwrap();
        let dotted = sub.join("..").join("app.exe");
        assert!(same_path(&f, &dotted), "f={} dotted={}", f.display(), dotted.display());
        let upper = root.join("APP.EXE");
        assert!(same_path(&f, &upper));
        let other = root.join("other.exe");
        assert!(!same_path(&f, &other));
        let _ = std::fs::remove_dir_all(&root);
    }

    /// junction：两条字符串不同、文件是同一个 —— 这正是修复前会漏杀的形态。
    /// `mklink /J` 不需要管理员权限；万一环境不支持就跳过（打印原因，不算失败）。
    #[test]
    fn same_path_resolves_junction() {
        let root = std::env::temp_dir().join(format!("cmv_sp_{}_j", std::process::id()));
        let real = root.join("real");
        std::fs::create_dir_all(&real).unwrap();
        let f = real.join("app.exe");
        std::fs::write(&f, b"x").unwrap();
        let link = root.join("link");
        let out = std::process::Command::new("cmd")
            .args(["/C", "mklink", "/J"])
            .arg(&link)
            .arg(&real)
            .creation_flags(CREATE_NO_WINDOW)
            .output();
        let ok = matches!(&out, Ok(o) if o.status.success());
        if !ok {
            eprintln!("[skip] 本机不支持 mklink /J，跳过 junction 用例：{:?}", out.map(|o| o.status));
            let _ = std::fs::remove_dir_all(&root);
            return;
        }
        let via_link = link.join("app.exe");
        assert!(same_path(&via_link, &f),
                "link={} real={}", via_link.display(), f.display());
        // 词法上它们显然不同：证明判据不是靠字符串
        assert_ne!(norm_path_text(&via_link.to_string_lossy()),
                   norm_path_text(&f.to_string_lossy()));
        let _ = std::fs::remove_dir(&link);          // 只摘链接，不动实体
        let _ = std::fs::remove_dir_all(&root);
    }
}
