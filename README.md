# ComfyMediaViewer

[![ci](https://github.com/Hartmann1995/ComfyMediaViewer/actions/workflows/ci.yml/badge.svg)](https://github.com/Hartmann1995/ComfyMediaViewer/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

本地优先的 ComfyUI 出图浏览器。扫描出图目录、网格预览、点开看生成参数，
支持收藏分类、提示词搜索、重复图检测、抹除元数据下载与回收站删除。

后端是单文件 Python（`server.py`，仅标准库 + Pillow），前端是原生 JS / CSS
（`frontend/`，零构建、零框架），桌面版用 Tauri 2 包一层壳。

> 当前版本 **V2.13.5** · [下载桌面版（Windows，单文件免安装）](https://github.com/Hartmann1995/ComfyMediaViewer/releases/latest)。
> 完整变更记录见 [`doc/CHANGELOG.md`](doc/CHANGELOG.md)，
> 逐步操作说明见 [`doc/使用说明.md`](doc/使用说明.md)。

## 功能

- **目录浏览**：自动探测 `ComfyUI/output`，子文件夹递归 / 收拢、面包屑、根目录切换
- **生成参数**：模型、LoRA（只统计实际启用的）、采样器、步数、CFG、Seed（沿连线解析真实值）、
  正负提示词，一键复制；单节点双输出工作流的正负向不会串味
- **视频**：mp4 / webm / mov / m4v / mkv / avi，停在首帧当海报，内嵌参数可读可搜
- **收藏与标签**：星标 + 自定义分类、8 组语义标签色、批量贴标签
- **提示词搜索**：后台增量索引，`tag:` `model:` `sampler:` `size:` `fav:` `type:`
  `after:` `before:` `name:` `wf:` 等条件各占一个可删除的 Chip
- **下载抹元数据**：字节级剥离 PNG / JPG 里的提示词与工作流，像素不变
- **删除与回收站**：单张 / 批量，移入应用内回收站可还原（默认保留 30 天）
- **重复图检测**：按组比对，保留一张、清理其余
- **出图统计**：每日产出日历热力图 + 模型 / 采样器 / 尺寸 Top 10
- **回传 ComfyUI**：把图片的工作流送回 ComfyUI，已开页面时直接载入成新画布
  （需装一次桥接脚本，界面可一键安装）
- **窄屏适配**：≤900px 左栏变抽屉，≤620px 详情面板变底部抽屉
- **卡片层与位置刻度**：卡片独立圆角 + 主题描边（静止不加投影）；文件名只在滚动 / 悬停 / 聚焦时出现，
  右下角给「第 N / 共 M 张 · 百分比」刻度，设置里可改为常显

完整功能与逐版变更见 [`doc/README.md`](doc/README.md) 与 [`doc/CHANGELOG.md`](doc/CHANGELOG.md)。

## 环境要求

- **Windows 10 / 11**：桌面版与目录探测、托盘行为按 Windows 设计
- **Python 3.9+**（开发与发布验证使用 3.12 / 3.13）
- 运行期只依赖 Pillow；内容检测额外需要 `numpy` + `onnxruntime`
  （缺失时该功能自动降级为「不可用」，其余功能不受影响）
- 构建桌面版另需 Rust 工具链与 Tauri 2 的构建依赖

## 运行

### 源码直启（推荐用于开发）

```bat
pip install -r requirements.txt
start_dev.bat
```

`start_dev.bat` 会依次尝试 `.venv` → `venv` → PATH 里的 `python`，
默认在 `8891` 启动后端并打开浏览器。也可以直接：

```bat
python server.py --port 8891
```

### 桌面版

从 [Releases 页面](https://github.com/Hartmann1995/ComfyMediaViewer/releases/latest) 下载
桌面版 exe（单文件，免安装）。GitHub 附件名不支持空格，因此以连字符代替，
例如 `ComfyMediaViewer-Desktop-V2.13.5.exe`；下载后可直接双击，也可以改回带空格的名字。
首次启动会把后端释放到 `%LOCALAPPDATA%\ComfyMediaViewer\backend\` 并复用。

## 数据与隐私

- 出图目录**只读**：程序不会改写、移动你的原始图片；删除走应用内回收站。
- 配置、索引、缩略图缓存、回收站都在 `%LOCALAPPDATA%\ComfyMediaViewer\`，
  可用设置页的「清除缓存」单独清理。
- 全程离线，不向任何服务器上传图片或提示词；内容检测模型在本机推理。

## 目录结构

| 路径 | 说明 |
|---|---|
| `server.py` | 后端全部逻辑（HTTP 服务、扫描、索引、缩略图、回收站、桥接） |
| `frontend/` | 前端源码（`index.html` + `js/` + `css/` + `bridge/`），零构建、零框架 |
| `ws_client.py` | 极简 WebSocket 客户端（纯标准库，供「回传 ComfyUI」的 CDP 通道使用） |
| `nsfw.py` | 内容检测推理封装（onnxruntime + numpy，懒加载） |
| `lora_links.py` | LoRA 元数据适配器（只读本地 `.metadata.json` / SQLite 缓存） |
| `models/nsfw/` | 随包分发的 ONNX 模型，来源与许可见 `NOTICE.md` |
| `tauri-app/` | 桌面壳（Tauri 2 / Rust），`frontendDist` 指向 `../frontend` |
| `doc/` | 变更日志、使用说明、第三方链接说明 |
| `start_dev.bat` | 源码直启（自动在本地补齐 Pillow / onnxruntime，不污染全局环境） |

> 开发期的门禁脚本、诊断工具与内部审计报告**不在本仓库发布**，仓库只含
> 产品源码、构建配置与用户文档。

## 构建

后端（PyInstaller 单文件）：

```bat
pyinstaller ComfyMediaViewer.spec
```

桌面版（需要 Rust 工具链，且先把上一步产物放到
`tauri-app/src-tauri/binaries/ComfyMediaViewer-x86_64-pc-windows-msvc.exe`）：

```bat
cd tauri-app\src-tauri
cargo build --release
```

产物在 `target\release\comfy-media-viewer.exe`。

## 已知限制

- 桌面版目前只在 Windows 上验证；后端本身不绑定平台，但目录探测与托盘按 Windows 写。
- 超大库（万张以上）首次启动要建提示词索引，期间搜索会显示「索引中」，属预期行为。
- 内容检测用的是轻量档模型（约 10 MB），只适合初筛，不保证判定准确。

## 许可

代码以 [MIT](LICENSE) 发布。第三方组件：

- Pillow、numpy、onnxruntime —— 各自上游许可
- 内容检测模型 `models/nsfw/nsfwjs.onnx` —— 见 [`models/nsfw/NOTICE.md`](models/nsfw/NOTICE.md)

本项目与 ComfyUI 官方无隶属关系，是独立第三方工具。
