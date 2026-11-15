# V2.13.2 LoRA Civitai 跳转

LoRA 名称仍可点击复制；右侧同时提供 RED 和 COM 两个独立链接（RED 在前）：

- **`[R│C] ↗` 分段胶囊**（2026-11-11 起）：一个控件、两个字母格，`R` = civitai.red，`C` = civitai.com，
  各自可点、各自可聚焦；字母恒定不变（避免打开面板时文字跳动），固定约 46px。
  - 已匹配模型 → 打开对应模型/版本页面（`title` 写「C 站 RED · 打开对应模型版本（来源）」）。
  - 正在匹配、没有本地文件、同名有歧义、未收录或网络不可用 → 打开名称搜索页
    （`title` 写具体原因，如「C 站 COM · 本机未找到模型，按名称搜索 C 站」），
    同时两格降到 70% 不透明度。
  - 单字母读屏依赖 `aria-label`，内容与 `title` 一致。

本地适配器读取 LoRA Manager 的 `.metadata.json`、旧版 `.civitai.info`
和只读 SQLite 缓存（支持集中存放元数据）；无需启动 ComfyUI 或插件。
读取当前模型库配置与默认 ComfyUI 的 `models/loras`。也可通过
`COMFYUI_ROOT` 指定 ComfyUI 根目录，`LORA_MANAGER_SETTINGS_DIR` 指定插件配置目录。
没有插件仍可对找到的本地模型计算 SHA256，并调用 Civitai 官方哈希查询。
图片已有 LoRA 哈希时优先按图片哈希查找，避免本机同名文件被替换后跳错。

首次读取大模型计算哈希可能需要等待；匹配在后台完成，不阻塞图片预览。
文件哈希与成功匹配保存在查看器数据目录 `lora-links-cache.json`，有效期七天。
失效/失败结果仅在进程内短暂缓存。仅发送模型哈希给 Civitai；不上传图片或模型文件。
点击搜索会将名称传给 Civitai 搜索页面。

桌面版通过本机后端打开系统默认浏览器；浏览器版使用新标签页。
后端仅允许本机、同源 JSON 请求，网址限定 Civitai 模型/版本/搜索页面。
手机访问时仅提供名称搜索，不读取电脑的模型索引。

## 参考与复用边界

参考 https://github.com/willmiao/ComfyUI-Lora-Manager 的公开路由、元数据格式、
SQLite 字段及模型链接格式；当前适配器为独立实现，未复制或导入插件代码。
插件是 GPL-3.0，查看器保留原 MIT 许可证。适配器只读，不修改插件配置、数据库或 sidecar。
在线查询依次尝试 `https://civitai.red/api/v1/model-versions/by-hash/{hash}`、COM 同路径接口。
匹配成功即停止；两个入口共享同一模型/版本 ID，站点是否展示该版本由各站决定。
已有 COM 缓存也会同时生成 RED/COM 两个入口，无需清空缓存。

## 验证

- `python tools/test_lora_links.py`：模型匹配、同名歧义、时间戳变化、模型替换、
  图片哈希优先、缓存、断网、旧 sidecar、只读 SQLite、输入与网址验证。
- `python tools/test_lora_links_http.py`：实际 POST 路由与来源、Content-Type、本机访问限制；
  默认浏览器调用使用 mock，不主动弹出浏览器。
- `node tools/test_lora_links_js.cjs`：真实前端函数的名称复制、负权重、链接、转义、
  哈希传递、桌面分支、过期响应处理。
- `tools/test_lora_links_ui.cjs`：Playwright 窄面板布局和 iframe 点击验证脚本。
  当前执行环境启动 Chromium 被 Windows 拒绝，未完成真实窗口点击验收。
- 本机三份有 LoRA Manager 元数据的模型已定位到具体 Civitai 版本。
  当前环境未成功完成对 Civitai API 的联网实测；在线成功/404/断网分支用模拟响应验证。

## 打包与运行

沿用修好启动问题的桌面壳，使用 PyInstaller 打包更新后端和前端，
将后端复制到 `tauri-app/src-tauri/binaries/ComfyMediaViewer-x86_64-pc-windows-msvc.exe`，
再执行 `cargo build --release --offline --locked --features custom-protocol`。

关闭旧版窗口，再将发布 ZIP **解压到桌面或普通用户目录**后运行 EXE。
源码工作目录存在继承的低完整性标记，直接在那里运行构建产物不能作为正常启动验收。
不修改用户权限或完整性标记；不覆盖已验收的旧 EXE。
