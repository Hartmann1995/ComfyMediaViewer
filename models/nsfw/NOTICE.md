# 第三方模型归属与许可说明

本目录中的 `.onnx` 模型不是本项目训练的，随源码与桌面版一并分发，
用于在本机对图片做内容判定（不联网、不上传）。

## 默认档 · `nsfwjs.onnx`

| 项 | 值 |
|---|---|
| 体积 | 10,318,774 字节（约 9.84 MB） |
| md5 | `8378845657f32646d1bcbda233249104` |
| 结构 | MobileNetV2 · 输入 224×224 |
| 用途 | 五分类内容打分，`drawings` 不计入 NSFW 分数 |

**上游权重**：来自 [infinitered/nsfwjs](https://github.com/infinitered/nsfwjs) 仓库
`models/mobilenet_v2/` 下的 TensorFlow.js MobileNetV2 权重（224×224 输入、五分类输出）。
上游仓库许可为 **MIT**（SPDX: `MIT`，许可原文见
[上游 LICENSE](https://github.com/infinitered/nsfwjs/blob/master/LICENSE)），权重随其 npm 包一同发布。

本目录的 `.onnx` 是该权重转换而来的 ONNX 单文件；转换只改格式，不改网络结构与权重含义。
本项目不对模型主张著作权，也不把它当作自有训练成果。

## 精确档 · `inception_v3.onnx`（不随包）

按项目文档，精确档取自 `deepghs/imgutils-models` 仓库的 `nsfw/inception_v3.onnx`，
体积约 85 MB，需要用户自行放入 `models/nsfw/`。该文件不在本仓库内。

## 许可与再分发

- 本项目自身代码以根目录 [LICENSE](../../LICENSE)（MIT）发布。
- 模型权重遵循其上游许可；本项目仅做格式转换与随包分发，不对模型主张著作权。
- 如果你要**再分发**本仓库或其中的模型，请同时保留本说明，并自行核实上游许可是否允许。

## 来源追溯与核验

可确证的部分已经写在上面的「上游权重」段。工程内**没有留存**当年做 ONNX 转换时所用脚本
或下载页 URL，因此这里不推断具体转换者与发布地址，只提供可独立核验的文件指纹：

| 核验项 | 期望值 |
|---|---|
| 字节数 | 10,318,774 |
| md5 | `8378845657f32646d1bcbda233249104` |
| 网络结构 | MobileNetV2，输入 224×224，五分类输出 |

本地核验（仓库 CI 也跑这一条）：

```bash
python -c "import hashlib;print(hashlib.md5(open('models/nsfw/nsfwjs.onnx','rb').read()).hexdigest())"
```

若你出于合规需要不随包分发第三方权重：删掉 `models/nsfw/nsfwjs.onnx` 即可，
内容检测会自动降级为「不可用」，浏览、参数查看、搜索、标签等其余功能不受影响
（此时请一并留意 `ComfyMediaViewer.spec` 的 `datas` 配置）。
如上游权利人对本文件的再分发提出异议，请通过仓库 Issue 反馈，我们会移除或替换为许可明确的等价模型。
