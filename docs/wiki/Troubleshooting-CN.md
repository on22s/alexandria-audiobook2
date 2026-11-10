# 故障排查

[中文主页](Home-CN.md) · [English](Troubleshooting.md)

先看日志。它们几乎总会指出问题：

- Script 页 Generate 按钮下的**活动行**——运行在等什么、第几次尝试、剩余时间；
- `logs/api/<task>-latest.log`——每个后台任务的完整日志（例如 `logs/api/audio-latest.log`）；
- `logs/llm_responses.log` 和 `logs/review_responses.log`——脚本生成和审阅期间每一次语言模型的回复，含结束原因、token 数和耗时。旁边的 `*.requests.log` 保存每条回复背后的确切请求，用 `request=` 编号对应，所以出错的回复可以重放；
- `logs/api/start.js/latest`——启动器的日志；`logs/sessions/` 把相关的安装、启动和辅助运行归组。

README 的 [Troubleshooting](../../README.md#troubleshooting) 一节覆盖启动器（Pinokio 到不了 "Open Web UI"）。本页覆盖其余部分。

## 书与保存

### 崩溃后保存的书打不开

加载或保存一本书会一起替换好几个文件。应用会为每次切换记一份事务日志（journal），所以切换被打断时（崩溃、断电、磁盘满），下次打开这本书时应用会把它做完或撤销。通常你根本察觉不到。

如果书的文件和事务日志对不上，比如另一个程序在切换途中改了某个文件，应用不会去猜哪个版本才对。它会拒绝打开这本书，并在 Home 页告诉你该试哪些命令。在 Alexandria 文件夹里用应用的 Python 运行：

```bash
python app/book_state_recovery.py inspect --hash
python app/book_state_recovery.py finish
python app/book_state_recovery.py quarantine --accept-current-files
```

- `inspect` 只查看，不改动。它会显示切换当时在做什么、哪些文件和预期不同、被移开的文件夹，以及崩溃留下的东西。
- `finish` 立即执行应用自己会做的那套恢复。
- `quarantine --accept-current-files` 是最后手段。它把书的文件原样保留，把事务日志和工作文件夹移到一个 `.book-switch-quarantine-*` 文件夹里。不会删除任何东西。
- 如果应用提示事务日志来自旧版本，请用 `migrate-legacy --trust-journal-hashes`。

在确定没有切换正在进行之前，不要动任何 `.book-switch-*` 文件夹。崩溃在事务日志写入之前留下的文件夹不会被自动清理，所以等书能正常打开后，你可以自己删掉它。每种崩溃状态、Home 页上的摘要和退出码，见[书的保存与切换被中断](../guides/BOOK_SWITCH_RECOVERY_CN.md)。

## 脚本生成

### 生成失败或产出垃圾

- 检查语言模型服务器正在运行，并且能在 **Base URL** 访问；在 Setup 里点击 **Test Connection**。
- 检查 **Model Name** 与服务器已加载的模型一致（刷新按钮会列出它们）。
- 在 `logs/llm_responses.log`（生成）或 `logs/review_responses.log`（审阅）里读模型的实际回复，并在对应的 `.requests.log` 里看它背后的请求。
- 使用[适合你显卡的模型](Which-Model-For-Your-Card-CN.md)里的模型，保持 JSON schema 开启，并把推理设为 *low*。

### 推理（"思考"）模型

推理模型工作得很好——事实上更好。把 **Reasoning effort** 设为 *low*，让服务器限制预算。**不要**把 `<think>` 加进禁用 token：在每一个会推理的模型上，reasoning low 测得都比关闭推理更好。

### 模型加载的上下文太小

请求可能超过任何已加载的上下文大小；8k 并非天生就有问题，32k 对你的显存也不总是安全。把提示词、回复和推理预算加在一起算。

- 改设置之前，先读服务器错误和 token 数。
- 减少受影响请求的文本、行数或审阅窗口；如果模型和可用内存支持，就增大上下文。
- LM Studio 的优化器使用按模型限定的受保护配置档，本地回退为 8,192 token；它并不总是选 32k。
- 按[选择上下文大小](Setup-and-Serving-CN.md#选择上下文大小)操作，然后运行 **Test this book with the LLM**。样本通过并不证明之后每个分块都装得下。

### 长时间"闲置"

读活动行：它写明步骤、单元、第几次尝试，以及在等什么（模型、限流退避、重试）。

- 如果几分钟都没有变化，服务器已停止回应——在 Setup 里测试它。
- 被 **When retries run out → pause** 暂停的运行在等你点击 Resume。

### Pause 是灰的

在 Windows 上应用无法挂起工作进程，所以那里的 Pause 被禁用。Cancel 和 **Resume failed run** 仍然可用。

### 角色名不一致

- 角色表会在请求之间携带，以保持名字一致。
- 用 **Find Nicknames** 和 **Edit aliases**，让同一个人的不同名字共用一个声音。
- 你可以在 Editor 里手动修正名字，或用 Saved Scripts 里的说话人修复。

### 完全没有 API

把 **How requests are sent** 设为 `manual`。应用会把每个请求写入 `manual_llm/pending.json`，Script 页会显示一个面板，供你复制提示词并粘贴回复。

## 语音生成

### 模型下载失败或很慢

- TTS 模型（每个约 3.5 GB）在第一次渲染时从 Hugging Face 下载。卡在 0% 的下载是网络问题，不是应用死机。
- **中国或受限网络：**启动前把下载指向镜像：

  ```
  HF_ENDPOINT=https://hf-mirror.com
  ```

  或者在 `start.js` 的 `shell.run` 参数里加上它：
  `env: { HF_ENDPOINT: "https://hf-mirror.com" }`。
- **限流（HTTP 429）：**创建一个免费的 [Hugging Face 账号](https://huggingface.co/join)，并把 `HF_TOKEN` 设为你的访问 token。
- **下载了一半：**删除 Hugging Face 缓存里的残缺文件后重试。`download_model.py` 可以在应用外获取权重。

### 模型加载失败

- 读 `logs/api/audio-latest.log` 里的第一个 traceback。
- 确认显存足够（见 README 的 [GPU 兼容性](../../README.md#gpu-compatibility)）。
- 最后手段：把 **Device** 设为 `cpu`——会慢很多。
- 在 AMD APU 上应用会自动使用 fp32；如果你强制了 `bf16`，去掉它。

### 克隆声音不工作

- 检查配置路径上的参考音频存在。
- 参考文本必须是音频的确切文字稿。
- 使用干净的 5–15 秒音频；短于约 7 秒的参考会得到不稳定的声音。
- 确认设计的声音已经**保存**，而不只是预览过。

### 找不到 LoRA 适配器

- 检查训练已经完成（Training 页）。
- 适配器路径相对于项目文件夹——检查 `lora_models/<adapter_id>` 存在。
- 在 Voices 页刷新适配器列表。

### 训练出的声音停不下来

这是训练出声音的缺陷（目标 2.3），通常来自过高的学习率或过拟合。终止检测（stop check）会在采纳前发现它。这期间先给该角色用一个随附声音，并以 1e-6 的学习率重新训练（见[训练指南](Training-Guide-CN.md)）。

### Voice Design 出问题

- 避免描述与指令之间的矛盾（"calm gentle voice" 配 "screaming furiously"）。
- 描述只写声音本身（年龄、性别、音质），把情绪和演绎留给指令。

### 外部 TTS 服务器

- 在 `external` 模式里，确认 Gradio TTS 服务器正在配置的 URL 上运行。
- 外部模式每个请求渲染一行；要批量渲染请用 `local` 模式。

## 性能

### 渲染很慢

1. 在 Setup 里点击 **Auto-Configure**。
2. 开启 **Compile Codec**——一次性 30–60 秒预热后快 3–4 倍。
3. 开启 **Sub-batching**——减少在填充上浪费的 GPU 时间。
4. 如果内存允许，提高 **Parallel Workers**。
5. 同一张卡上的语言模型会让 TTS 可用的显存减半。如果你的 LLM 托管在别处，打开 **Provider request options**，把 **Runs on this machine's GPU?** 设为 **No**。

见[批量生成](Batch-Generation-CN.md)。

### 显存不足

- 调低 **Parallel Workers** 和 **Max Items/Batch**。
- 关闭其他使用 GPU 的程序。
- 内存检查会拒绝装不下的批次，所以运行中途显存不足通常意味着检查之后有别的进程占用了显卡。
- LoRA 训练需要独占 GPU；应用会自动卸载自己的模型。
- 在声音类型之间切换时，加载模型可能短暂需要额外显存。

### 第二批卡住或失败

这是显存碎片。应用会在子批次之间清理内存以预防它。如果仍然发生，减小批大小，或在大型运行之间重启应用。

### MIOpen 警告（AMD）

AMD GPU 上的 MIOpen 工作区警告会被自动处理，不影响质量或速度。

## 音频输出

### 损坏或很小的 MP3 文件（428 字节）

ffmpeg 缺失或无法编码 MP3。安装器会把 ffmpeg 构建进应用的环境，所以这通常意味着应用没有在 `app/env` 里运行：从 Pinokio 启动它，或激活 `app/env`。每个生成的文件都会被检查，Editor 会标记损坏的分块。

手动检查 ffmpeg：

```bash
ffmpeg -encoders 2>/dev/null | grep mp3
```

### 音频质量

- 克隆时使用干净的 5–15 秒参考。
- 为 Custom 声音换一个 seed（或留空以随机）。
- 检查指令：自相矛盾的指令，或描述身体而非声音的指令，会降低质量。描述声音本身的词（"deep"、"raspy"）会和选定的声音打架；身份锚点会去掉它们。
- 对 LoRA 声音，确认训练数据有情绪多样性。

### 文本里的乱码

- 上传时的修复会处理常见编码问题。
- 如果书里仍显示 `â€™`，从 Saved Scripts 运行修复预览。
- 确认输入文件是 UTF-8。
- 图形假名和其他不可读字符会在 TTS 之前被有意移除。

## 训练

### loss 一直很高（高于 10）

- 检查 WAV 文件有效、没有损坏。
- 检查 `metadata.jsonl` 里每个片段都有正确的文字稿。
- 检查参考音频存在且干净。
- **不要提高学习率**来强行压低 loss：在 5e-6 上声音永远不会停止说话。

### 训练后声音平淡

- 训练数据缺少情绪多样性——最常见的原因。
- 用情绪和文本都多样的样本对（高兴、愤怒、悲伤、低语、喊叫）重建数据集。
- 试更低的 LoRA rank（8–16），以保留更多基础模型的表现力。

### 训练崩溃或卡住

- 训练需要独占 GPU；应用应先卸载自己的模型。
- 读日志里第一个 CUDA 或 ROCm 错误。
- 试更小的批大小或更少的梯度累积步数。

## Windows

### 文件锁定错误

Windows 在并行读写时可能锁定 JSON 文件。应用会原子地写文件并以退避重试。如果错误持续：

- 调低 **Parallel Workers**；
- 关闭打开了项目文件的编辑器或文件资源管理器。

## 检查安装

```bash
cd app
python run_isolated_api_tests.py          # quick — model-free API checks in a throwaway copy
python run_isolated_api_tests.py --full   # also generation — needs a GPU and an LLM
```

它会用空数据文件夹启动自己的一份应用副本，所以你的书和声音不会被碰到。通过快速模式只验证它跑到的无模型路径；生成仍可能因应用代码、模型加载、GPU 资源或提供方集成而失败。检查失败任务的日志。测试已经在运行的应用见 [Home](Home-CN.md#检查安装)。
