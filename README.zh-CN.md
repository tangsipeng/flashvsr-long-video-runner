# flashvsr-long-video-runner

[English README](README.md)

这是一个围绕**现有上游 FlashVSR 代码目录**构建的轻量包装器，用来更稳妥地运行长视频处理任务，并且更适合开源分发。

这个仓库**不内置 FlashVSR 权重，也不会修改上游项目**。它主要做的是：

- 使用 `ffprobe` 探测输入视频
- 生成带有明确帧范围的分块清单 `manifest`
- 调用上游 `infer_flashvsr_v1.1_tiny_long_video.py` 渲染各个分块
- 通过**chunk 索引 + chunk 文件是否存在**来支持断点续跑，而不是依赖迭代器位置
- 将所有 chunk 视频拼接起来，并重新封装回原始音频

它的主要目标是让长视频执行过程不那么脆弱，并且在中断后更容易恢复。

## 为什么需要这个项目

上游的长视频示例很适合验证思路，但在真正跑长任务时，通常还会需要：

- 可重复、确定性的分块规划
- 明确的帧级账本和边界信息
- 程序崩溃或手动中断后的安全恢复能力
- 对超短尾块更稳妥的处理方式
- 一个不打包私有权重、便于分享的独立仓库

## 架构

### 1. 规划阶段

`flashvsr-long-video plan` 会先检查视频，再生成一个 manifest JSON。

每个 chunk 会记录：

- `source_start`、`source_end`：这个 chunk 在最终输出里真正负责的帧范围
- `render_start`、`render_end`：实际送入上游 FlashVSR 的源视频帧范围
- `pad_left`、`pad_right`：如有需要，在边界补出来的重复帧数量
- `trim_start`、`trim_end`：渲染完成后，如何把结果裁回这个 chunk 精确负责的范围

也就是说，manifest 是执行和恢复流程的唯一事实来源。

### 2. 尾块启发式策略

FlashVSR 更适合的渲染窗口长度通常是 `5, 13, 21, ...`，也就是 `8n-3`。

对于长视频，如果简单按固定长度切分，最后可能会剩下一个非常短的尾块，比如只有 1 到 8 帧。与其直接渲染这样一个很小的尾块，规划器会尝试**把尾块并入一个更大的最终渲染窗口**：从前一个 chunk 借一些上下文帧进来渲染，最后再把结果裁回尾块真正需要的精确范围。

例如：

- 精确的 source chunk 是 `[0:21)`、`[21:42)`、`[42:50)`
- 最后一个 source chunk 只有 8 帧
- 规划器会把最后一个 chunk 的渲染范围设为 `render_start=37`、`render_end=50`，总共渲染 13 帧
- 推理完成后，再把前 5 帧裁掉，只保留 `[42:50)` 对应的结果

这样既能保持 source chunk 的归属关系清晰，也能避免渲染极短尾块时的不稳定问题。

### 3. 运行阶段

`flashvsr-long-video run` 会：

- 读取 manifest
- 动态导入上游推理脚本
- 只初始化一次上游 pipeline
- 按 manifest 中写明的帧范围逐块渲染
- 写出每个 chunk 对应的 MP4 文件
- 用 `ffmpeg` 把所有 chunk 拼接起来
- 把原始输入视频的音频重新 mux 回最终输出

由于每个 chunk 都已经记录了自己的帧范围和输出路径，所以恢复逻辑是**基于 chunk 索引**的。

## 依赖要求

- Python 3.10+
- `PATH` 里可用的 `ffmpeg` 和 `ffprobe`
- 一个可正常运行、且 GPU 依赖已安装好的上游 FlashVSR 环境
- 一个上游代码目录，例如：
  - `/path/to/FlashVSR/examples/WanVSR/infer_flashvsr_v1.1_tiny_long_video.py`
  - `/path/to/FlashVSR/examples/WanVSR/FlashVSR-v1.1/`

这个包装器自身尽量保持依赖精简。真正的 GPU 和运行时依赖仍然来自上游 FlashVSR 环境。

## 安装原始 FlashVSR 模型链路

如果你还没有可用的上游 FlashVSR 环境，建议先按原项目的安装方式准备好。下面这部分是根据官方 FlashVSR README 整理出来的。

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

- 编译阶段可能比较吃内存
- 上游 README 明确说明，除了 A100 / A800 / H200 之外，其他显卡的兼容性和性能没有官方保证

### 4. 下载原模型权重

在上游仓库根目录执行：

```bash
cd examples/WanVSR
git lfs install

# v1（原始版本）
git lfs clone https://huggingface.co/JunhaoZhuang/FlashVSR

# 或 v1.1（上游当前更推荐）
git lfs clone https://huggingface.co/JunhaoZhuang/FlashVSR-v1.1
```

预期目录结构如下：

```text
examples/WanVSR/FlashVSR-v1.1/
  LQ_proj_in.ckpt
  TCDecoder.ckpt
  Wan2.1_VAE.pth
  diffusion_pytorch_model_streaming_dmd.safetensors
```

这个仓库里的 wrapper 只依赖“可正常运行的上游代码目录 + 对应权重目录”，不会把它们打包进来。

## 安装本仓库 wrapper

```bash
cd flashvsr-long-video-runner
pip install -e .
```

## 使用方法

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

## 效果预览

下面这些素材来自这次用本仓库 wrapper 配合上游 `infer_flashvsr_v1.1_tiny_long_video.py` 对本地 `video.mp4` 的实际重跑结果。

- 输入样例：`960x720`、`4007` 帧、约 `133.6` 秒
- 预览输出：第一个渲染 chunk，也就是 `chunk_00000.mp4`
- 输出分辨率：`1920x1408`
- 说明：上游会把尺寸对齐到更适合模型的分辨率，所以高度被居中裁到 `1408`，而不是严格的 `1920x1440`

第 10 帧对比，左边是对齐后的输入，右边是 FlashVSR 输出：

![第10帧对比](docs/media/frame10_compare.png)

Logo 和标题区域的局部对比：

![第10帧细节对比](docs/media/frame10_detail_compare.png)

第一个 chunk 的短视频对比：

[![第一个 chunk 的对比短视频](docs/media/frame10_compare.png)](docs/media/chunk_00000_compare.mp4)

这条样例上的直观观察：

- 大标题笔画和远处山体边缘明显更锐
- 左上角很小的叠字仍然有比较明显的振铃和伪影

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
      "render_start": 37,
      "render_end": 50,
      "trim_start": 5,
      "trim_end": 13,
      "output_path": ".../chunks/chunk_00002.mp4"
    }
  ]
}
```

## 约定与假设

- 上游推理脚本仍然可以通过动态导入方式使用
- 除非显式传入 `--weights-dir`，否则上游权重目录结构仍然是 `FlashVSR-v1.1/`
- 上游模型仍然要求 `8n-3` 形态的渲染窗口，并沿用参考示例里的额外 4 帧内部 padding 策略
- 各 chunk 输出视频之间足够兼容，可以用 stream copy 方式拼接

## 当前限制

- 这个项目目前只针对上游 `infer_flashvsr_v1.1_tiny_long_video.py` 这一套流程
- 目前没有做场景感知分块，也没有做内容感知的 overlap 调优
- 如果整段视频本身就很短，由于没有前序上下文可借，仍然需要依赖边界重复帧
- 当前环境中的验证主要覆盖规划器和单元测试；真正的端到端 GPU 执行仍然需要实际的 FlashVSR 运行环境和权重

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
  upstream.py
tests/
```
