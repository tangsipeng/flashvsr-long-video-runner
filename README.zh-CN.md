# flashvsr-long-video-runner

[English README](README.md)

这是一个围绕**现有上游 FlashVSR 代码目录**构建的轻量包装器，用来更稳妥地做长视频 2 倍超分。

这个仓库**不内置 FlashVSR 权重，也不会修改上游项目**。它补上的能力包括：

- 带明确帧范围的 manifest 分块规划
- 更安全的长任务断点续跑
- 基于上游 `infer_flashvsr_v1.1_tiny_long_video.py` 的逐 chunk 渲染
- 最终 MP4 拼接并恢复原始音频
- 可选的异步 HTTP 服务，支持上传、排队、轮询、取消和结果下载

## 一眼看懂

这个仓库可以按两种方式使用：

1. CLI runner：本地生成 manifest，执行任务，并在中断后恢复。
2. 异步服务：让外部系统上传视频、查询状态、取消任务，并在稍后下载结果。

当前队列模型：

- 只有一个活动渲染 worker
- 其他任务进入 `queued`
- 可以通过 `--max-queued-jobs` 限制等待队列长度

## 快速开始

### 依赖要求

- Python 3.10+
- `PATH` 中可用的 `ffmpeg` 和 `ffprobe`
- 一个可正常运行、并且 GPU 依赖已经安装好的上游 FlashVSR 环境

预期的上游目录结构：

```text
/path/to/FlashVSR/
  examples/WanVSR/infer_flashvsr_v1.1_tiny_long_video.py
  examples/WanVSR/FlashVSR-v1.1/
```

### 安装本仓库 wrapper

请在一个已经可以运行上游 FlashVSR 的 Python 环境里执行 `plan`、`run`、`serve`。

```bash
cd flashvsr-long-video-runner
pip install -e .
```

### 用一个脚本管理服务启停

```bash
./scripts/flashvsr_service.sh start
./scripts/flashvsr_service.sh status
./scripts/flashvsr_service.sh logs
./scripts/flashvsr_service.sh stop
```

启动 4 倍超分服务：

```bash
./scripts/flashvsr_service.sh start 4
```

这个脚本会自动：

- 使用 `PYTHONPATH=<repo>/src`
- 优先使用 `~/.openclaw/workspace/mycode/FlashVSR/.venv/bin/python`
- 把 PID 文件写到 `.omx/state/`
- 把日志写到 `.omx/logs/`

也可以通过环境变量覆盖这些路径，例如 `FLASHVSR_UPSTREAM_ROOT`、`FLASHVSR_PYTHON`、`FLASHVSR_PORT`、`FLASHVSR_MAX_QUEUED_JOBS`。

### 启动异步服务

```bash
flashvsr-long-video serve \
  --host 0.0.0.0 \
  --port 8000 \
  --state-dir /data/flashvsr_service \
  --max-queued-jobs 8 \
  --upstream-root /path/to/FlashVSR
```

服务会：

- 接收视频上传
- 使用带进度的上传会话时，会立即返回 `job_id`
- 在单 worker 队列里异步执行超分
- 把上传文件、manifest、chunk 和结果都保存到 `--state-dir`
- 在服务重启后恢复 `queued` 和 `running` 任务

### 提交视频

原来的单步上传仍然可用：

```bash
curl -sS -X POST "http://127.0.0.1:8000/v1/jobs?filename=input.mp4" \
  -H "Content-Type: application/octet-stream" \
  -H "X-Filename: input.mp4" \
  --data-binary @/data/input.mp4
```

服务会返回 `202 Accepted`，并携带 `job_id`。

如果你需要在请求体还没传完时就在服务端查询上传进度，可以先创建 job，再把文件传到它的 `upload` 地址：

```bash
SIZE_BYTES="$(stat -c%s /data/input.mp4)"

curl -sS -X POST "http://127.0.0.1:8000/v1/jobs" \
  -H "Content-Type: application/json" \
  -d "{\"filename\":\"input.mp4\",\"size_bytes\":${SIZE_BYTES}}"
```

响应里会有 `status: "uploading"`、`job_id` 和 `urls.upload`。然后上传文件内容：

```bash
curl -sS -X PUT "http://127.0.0.1:8000/v1/jobs/<job_id>/upload" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @/data/input.mp4
```

### 轮询进度

```bash
curl -sS "http://127.0.0.1:8000/v1/jobs/<job_id>"
```

返回里的重点字段包括：

- `status`：`uploading`、`queued`、`running`、`cancelling`、`cancelled`、`succeeded`、`failed`
- `input.uploaded_bytes`
- `input.upload_percent`
- `progress.phase`：`uploading`、`queued`、`planning`、`rendering`、`finalizing`、`cancelling`、`cancelled`、`completed`、`failed`
- `progress.percent`
- `progress.uploaded_bytes`
- `progress.total_upload_bytes`
- `progress.upload_percent`
- `progress.done_source_frames`
- `progress.total_source_frames`
- `progress.current_chunk`
- `progress.elapsed_seconds`
- `progress.estimated_remaining_seconds`

### 下载结果

```bash
curl -L -o output_x2.mp4 "http://127.0.0.1:8000/v1/jobs/<job_id>/result"
```

如果下载中断，可以使用 `Range` 续传：

```bash
curl -L -H "Range: bytes=1048576-" -o output_x2.part \
  "http://127.0.0.1:8000/v1/jobs/<job_id>/result"
```

### 取消任务

```bash
curl -X DELETE "http://127.0.0.1:8000/v1/jobs/<job_id>"
```

取消语义：

- 空闲的 `uploading` 任务可以立即取消，并直接进入 `cancelled`
- 正在接收文件的 `uploading` 任务会先进入 `cancelling`，随后停止继续读入字节并进入 `cancelled`
- `queued` 任务会立刻变成 `cancelled`
- `running` 任务会先变成 `cancelling`，然后在下一个 chunk 边界安全停止，再进入 `cancelled`
- 已完成任务不能取消，会返回 `409`

## HTTP API

### 接口列表

- `POST /v1/jobs`
  如果 `Content-Type: application/octet-stream`，会单步上传视频并创建超分任务；如果 `Content-Type: application/json`，会根据 `filename` 和 `size_bytes` 创建一个 `uploading` 状态的上传会话。
- `PUT /v1/jobs/<job_id>/upload`
  给 JSON 创建出来的 job 上传文件内容，完整接收声明的 `size_bytes` 后进入队列。
- `GET /v1/jobs/<job_id>`
  查询任务状态、上传进度、渲染进度、ETA 提示和结果是否可下载。
- `GET /v1/jobs/<job_id>/result`
  下载完成后的 MP4，支持单段 `Range` 续传。任务未完成时返回 `409`。
- `DELETE /v1/jobs/<job_id>`
  取消一个排队中或运行中的任务。
- `GET /v1/jobs`
  列出近期任务和队列摘要。
- `GET /healthz`
  轻量健康检查。

### 失败处理

上传处理：

- 上传内容会先写入临时 `.part` 文件
- `POST /v1/jobs` 的 octet-stream 单步上传仍然可用
- 需要上传进度时，先用 JSON `POST /v1/jobs` 创建任务，再用 `PUT /v1/jobs/<job_id>/upload` 上传内容
- 进度上传期间，`GET /v1/jobs/<job_id>` 会报告 `input.uploaded_bytes`、`input.upload_percent` 和 `progress.upload_percent`
- `DELETE /v1/jobs/<job_id>` 现在也支持 `uploading` 阶段；空闲会话会立刻取消，活跃上传会在下一次 copy-loop 检查时停止
- 如果单步上传中途断开，服务会返回错误，并删除未完成的任务目录
- 如果进度上传中途断开，这个可见 job 会进入 `failed`
- 如果等待队列已满，`POST /v1/jobs` 会返回 `429 Too Many Requests`

渲染处理：

- manifest 中会持久化每个 chunk 的状态
- 如果渲染失败，任务会进入 `failed`
- 错误文本会写到响应里的 `error`
- 服务重启后会用 `resume=True` 恢复 `queued` 和 `running` 任务

下载处理：

- 已完成结果支持 `Range` 续传
- 非法字节范围会返回 `416 Requested Range Not Satisfiable`
- 客户端下载过程中断开，不会破坏已保存的结果文件

## CLI 工作流

### 生成 manifest

```bash
flashvsr-long-video plan \
  --input /data/input.mp4 \
  --output /data/output_x2.mp4 \
  --scale 2 \
  --work-dir /data/flashvsr_run \
  --upstream-root /path/to/FlashVSR
```

或者显式指定推理脚本：

```bash
flashvsr-long-video plan \
  --input /data/input.mp4 \
  --output /data/output_x2.mp4 \
  --infer-script /path/to/FlashVSR/examples/WanVSR/infer_flashvsr_v1.1_tiny_long_video.py \
  --weights-dir /path/to/weights/FlashVSR-v1.1
```

命令会把 manifest JSON 打印到终端，并在未提供 `--manifest` 时默认写入 `<work-dir>/manifest.json`。

### 基于 manifest 运行

```bash
flashvsr-long-video run --manifest /data/flashvsr_run/manifest.json
```

恢复一个已经部分完成的任务：

```bash
flashvsr-long-video run --manifest /data/flashvsr_run/manifest.json --resume
```

如果 manifest 里还没有保存上游路径，也可以在运行时补充：

```bash
flashvsr-long-video run \
  --manifest /data/flashvsr_run/manifest.json \
  --upstream-root /path/to/FlashVSR \
  --resume
```

## 依赖与上游安装

如果你还没有可用的上游 FlashVSR 环境，建议先准备好。

### 1. 克隆上游 FlashVSR

```bash
git clone https://github.com/OpenImagingLab/FlashVSR
cd FlashVSR
```

### 2. 创建 Python 环境

上游项目推荐使用 Python `3.11.13`：

```bash
conda create -n flashvsr python=3.11.13
conda activate flashvsr
pip install -e .
pip install -r requirements.txt
```

### 3. 安装 Block-Sparse-Attention

FlashVSR 依赖 Block-Sparse-Attention 后端。上游 README 建议在仓库外单独找一个干净目录安装：

```bash
git clone https://github.com/mit-han-lab/Block-Sparse-Attention
cd Block-Sparse-Attention
pip install packaging
pip install ninja
python setup.py install
```

注意：

- 编译步骤可能比较吃内存
- 上游 README 明确说明，除了 A100 / A800 / H200 之外，其他显卡的兼容性和性能没有官方保证

### 4. 下载模型权重

在上游仓库根目录执行：

```bash
cd examples/WanVSR
git lfs install

# v1（原始版本）
git lfs clone https://huggingface.co/JunhaoZhuang/FlashVSR

# 或 v1.1（上游当前更推荐）
git lfs clone https://huggingface.co/JunhaoZhuang/FlashVSR-v1.1
```

预期权重目录结构：

```text
examples/WanVSR/FlashVSR-v1.1/
  LQ_proj_in.ckpt
  TCDecoder.ckpt
  Wan2.1_VAE.pth
  diffusion_pytorch_model_streaming_dmd.safetensors
```

这个仓库里的 wrapper 只依赖“可正常运行的上游代码目录 + 对应权重目录”，不会把它们打包进来。

## 架构概览

### 规划阶段

`flashvsr-long-video plan` 会先检查视频，再生成一个 manifest JSON。

每个 chunk 会记录：

- `source_start`、`source_end`：这个 chunk 在最终输出里真正负责的帧范围
- `render_start`、`render_end`：实际送入上游 FlashVSR 的源视频帧范围
- `pad_left`、`pad_right`：如有需要，在边界补出来的重复帧数量
- `trim_start`、`trim_end`：渲染完成后，如何把结果裁回这个 chunk 精确负责的范围

manifest 是执行和恢复流程的唯一事实来源。

### 尾块启发式策略

FlashVSR 更适合的渲染窗口长度通常是 `5, 13, 21, ...`，也就是 `8n-3`。
但这个长视频 pipeline 的内部 buffered decode 路径至少要拿到 21 帧渲染窗口，才会真正产出可拼接的结果。

对于长视频，如果简单按固定长度切分，最后可能会剩下一个非常短的尾块，比如 1 到 8 帧。与其直接渲染这样一个小尾块，规划器会尝试把尾块并入一个更大的最终渲染窗口，从前一个 chunk 借一些上下文帧进来渲染，最后再把结果裁回尾块真正需要的范围。

例子：

- 精确的 source chunk 是 `[0:21)`、`[21:42)`、`[42:50)`
- 最后一个 source chunk 只有 8 帧
- 规划器会把最后一个 chunk 的渲染范围设为 `render_start=29`、`render_end=50`，形成 21 帧渲染窗口
- 推理完成后，再把前 13 帧裁掉，只保留 `[42:50)`

### 运行阶段

`flashvsr-long-video run` 会：

- 读取 manifest
- 动态导入上游推理脚本
- 只初始化一次上游 pipeline
- 按 manifest 中写明的帧范围逐块渲染
- 写出每个 chunk 对应的 MP4 文件
- 用 `ffmpeg` 把所有 chunk 视频拼接起来
- 把原始输入视频的音频重新 mux 回最终输出

由于每个 chunk 都已经记录了自己的帧范围和输出路径，所以恢复逻辑是基于 chunk 索引的。

## Manifest 示例

```json
{
  "input_path": "/data/input.mp4",
  "output_path": "/data/output_x2.mp4",
  "scale": 2.0,
  "video": {
    "total_frames": 50,
    "fps_text": "25/1"
  },
  "chunks": [
    {
      "index": 2,
      "source_start": 42,
      "source_end": 50,
      "render_start": 29,
      "render_end": 50,
      "trim_start": 13,
      "trim_end": 21,
      "output_path": ".../chunks/chunk_00002.mp4"
    }
  ]
}
```

## 效果预览

下面这些素材来自这次用本仓库 wrapper 配合上游 `infer_flashvsr_v1.1_tiny_long_video.py` 对本地 `video.mp4` 的实际重跑结果。

- 输入样例：`960x720`、`4007` 帧、约 `133.6` 秒
- 预览输出：第一个渲染 chunk，也就是 `chunk_00000.mp4`
- 输出分辨率：`1920x1408`
- 上游会把尺寸对齐到更适合模型的分辨率，因此高度被居中裁到 `1408`，而不是严格的 `1920x1440`

第 10 帧对比，左边是对齐后的输入，右边是 FlashVSR 输出：

![第10帧对比](docs/media/frame10_compare.png)

Logo 和标题区域的局部对比：

![第10帧细节对比](docs/media/frame10_detail_compare.png)

第一个 chunk 的短视频对比：

[![第一个 chunk 的对比短视频](docs/media/frame10_compare.png)](docs/media/chunk_00000_compare.mp4)

这条样例上的直观观察：

- 大标题笔画和远处山体边缘明显更锐
- 左上角很小的叠字仍然有比较明显的振铃和伪影

## 约定与假设

- 上游推理脚本仍然可以通过动态导入方式使用
- 除非显式传入 `--weights-dir`，否则上游权重目录结构仍然是 `FlashVSR-v1.1/`
- 上游模型仍然要求 `8n-3` 形态的渲染窗口，并沿用参考示例里的额外 4 帧内部 padding 策略，而且在长视频模式下每个渲染窗口至少要有 21 帧源序列
- 各 chunk 输出视频之间仍然足够兼容，可以用 stream copy 方式拼接

## 当前限制

- 这个项目目前只针对上游 `infer_flashvsr_v1.1_tiny_long_video.py` 这一套流程
- 目前没有做场景感知分块，也没有做内容感知的 overlap 调优
- 如果整段视频本身就很短，由于没有前序上下文可借，仍然需要依赖边界重复帧
- 当前环境中的验证主要覆盖单元测试；真正的端到端 GPU 执行仍然需要实际的 FlashVSR 运行环境和权重

## 开发

运行测试：

```bash
pytest
```

## 仓库结构

```text
src/flashvsr_long_video_runner/
  cli.py
  manifest.py
  media.py
  planning.py
  runner.py
  service.py
  storage.py
  upstream.py
  workflow.py
tests/
```
