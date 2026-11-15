# NSFW 检测模型（**随包分发**）

> V2.12.5 口径：这个目录里的 `.onnx` **既进 exe，也进源码包**。
> **离线可用是硬要求** —— 别人拿到程序不联网也应该能直接用上内容检测。

## 目录里有什么

| 文件 | 档位 | 大小 | 说明 |
|---|---|---|---|
| `nsfwjs.onnx` | 轻量档（默认） | 9.84 MB | MobileNetV2 · 224×224 · CPU 单张约 2 ms |

## 体积账（为什么值得这 10 MB）

随包要把推理引擎一起带上：`onnxruntime` ≈ 40 MB + `numpy` ≈ 15 MB + 模型 9.84 MB，
exe 因此从 **14.5 MB → 约 60 MB**（打包前估算；**实测 43.05 MB**，`ComfyMediaViewer.spec` 已开 UPX）。

我们评估过「引擎不随包」（决策 1B，小包方案），**最终否掉**，理由是：
对冻结进程说「`pip install onnxruntime`」是一句做不到的话 —— 用户手上没有可装的
Python 环境。不随包等于这个功能对绝大多数人不存在，界面上还多一条「不可用」的灰提示。

两处都留了口子，**改口径时必须同步改这三处**，否则界面说的话会和包里的内容对不上：

| 位置 | 1A（当前） | 改成 1B 要做什么 |
|---|---|---|
| `ComfyMediaViewer.spec` | `excludes=[]` + `datas` 带 `models/nsfw/*.onnx` | 加回 `excludes=['onnxruntime','numpy']`、去掉 `_nsfw_models` |
| `nsfw.py` 的 `unavailable_reason()` | 冻结态提示「构建环境缺少依赖」 | 改回「本版未随包分发推理引擎」 |
| 本文档 | 随包 | 整篇翻回去 |

## 查找顺序

程序按下面的顺序找模型，先命中先用（`nsfw.py` 的 `_default_model_dirs()`）：

1. 环境变量 `CMV_NSFW_MODELS` —— 验证脚本 / 自定义部署用
2. PyInstaller 解包目录 `models/nsfw` —— **打包后走这条**
3. 程序目录 `models/nsfw` —— **源码直启走这条**；手动放模型也放这里
4. 数据目录 `models/nsfw` —— `%LOCALAPPDATA%\ComfyMediaViewer\models\nsfw`（便携模式是 `cmv-data\models\nsfw`）

## 想用「精确档」怎么办

精确档是 InceptionV3 版本（约 85 MB），误判更少但更慢。**默认不随包**（体积代价太大）。

1. 下载 `inception_v3.onnx`（来源：`deepghs/imgutils-models` 仓库的 `nsfw/inception_v3.onnx`）
2. 放进**程序目录**的 `models/nsfw/`，或数据目录的 `models/nsfw/`
3. 重启程序 → 设置 → 内容 → 模型档位，就能选「精确档」

放程序目录会跟着整个文件夹走（拷 U 盘带走）；放数据目录则与程序位置无关（换版本不丢）。

## 换模型后

不同档位**共用一套分数定义**，但分数本身没有可比性。
切换档位会让旧检测结果全部作废、需要重新检测 —— 这是刻意的：
`nsfw_index.json` 里每条都记了 `md`（档位），对不上的一律不采用。
手动纠正过的判定存在另一个文件（`nsfw_manual.json`），换档位**不会**丢。

## 口径提醒

`drawings`（插画 / 像素画 / 二次元场景图）**不计入** NSFW 分数 —— 否则正常插画会被
大批误杀。这条写在 `nsfw.py` 的 `NSFW_IDX` 里，改动时别把它加回去。
