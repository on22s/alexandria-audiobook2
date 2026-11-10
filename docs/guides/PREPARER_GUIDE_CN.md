[English](PREPARER_GUIDE.md) | [中文](PREPARER_GUIDE_CN.md) | [日本語](PREPARER_GUIDE_JA.md)

# Alexandria Preparer 指南

用一条命令把一本有声书处理成带标注的 TTS 训练数据集。

## 概览

Preparer 接受一个音频文件（有声书），产出一个 ZIP 归档，里面包含：
- 词级对齐的音频片段（24 kHz 的 `sample_NNNN.wav`）
- `metadata.jsonl`，带文本标注、时长和时间区间
- 可以直接当作 TTS 训练数据集使用

# Alexandria Preparer 指南

## 这个工具做什么（极简版）

Preparer 把一本有声书（音频文件）转换成 Alexandria TTS 引擎的训练数据集。它做三件事：

1. **听音频**，把每个词连同精确的时间戳记下来（这叫"转写"或"ASR"）
2. **可选地与原始的书对照**，让文本使用书里的拼写（而不是 AI 听错的那个）
3. **把音频切成小分块**（每个约 10 秒），存成 WAV 文件，另附一个元数据文件

输出是一个 `.zip` 文件，你可以把它喂给 Alexandria 的 LoRA Training 页，给 TTS 引擎教一个新声音。

### 这是给谁用的？

- **你有一本有声书**（WAV 文件），想克隆其中一个声音
- **你想训练一个自定义声音**，用 Training 页
- **如果你只是用 Alexandria 从书生成有声书，就不需要它**——Web UI 会自动处理一切

---

## 给非程序员的快速开始

### 前置条件

运行 Preparer 之前，请确认：

1. **你装了 Pinokio**，并且 Alexandria 是装在 Pinokio 里的
2. **你有一个有声书 WAV 文件**——如果手上是 MP3，先转成 WAV（可以用在线转换器或 Audacity）
3. **你有原始的书文件**（.epub 或 .txt）——这是可选的，但强烈推荐，效果会好很多

### 第 1 步：打开终端

- **Windows：** 按 `Win + R`，输入 `cmd`，回车
- **Mac：** 打开 Spotlight（Cmd + Space），输入 `terminal`，回车
- **Linux：** 打开应用菜单，搜索 "Terminal"

### 第 2 步：进入 Alexandria 文件夹

输入这条命令并回车：

```bash
cd ~/.pinokio/api/alexandria-audiobook.git
```

如果你把 Alexandria 装在别的位置，就换成那个路径。

### 第 3 步：运行 Preparer

复制粘贴这条命令（把路径换成你实际的文件路径）：

```bash
./app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "path/to/your/audiobook.wav" \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --source "path/to/your/book.epub"
```

**各部分是什么意思：**
- `./app/env/bin/python` —— 这是 Alexandria 自带的 Python（不要用你系统里的 Python）
- `--audio` —— 你有声书 WAV 文件的路径
- `--model` —— 给每个分块标注说话人和演绎指令的 AI 模型
- `--source` —— 原始的书文件（可选，但能提高准确率）

### 第 4 步：等着

Preparer 会：
1. 转写音频（每 5 小时音频大约要 1 小时）
2. 把词与书对照（如果你给了 `--source`）
3. 切成分块并保存 ZIP

完成时你会看到：
```
✓ Dataset written to: alexandria_dataset.zip
```

### 第 5 步：使用输出

1. 解压 `alexandria_dataset.zip`——里面有 WAV 文件和 `metadata.jsonl`
2. 在 Alexandria 的 Web UI 里打开 **Dataset** 页
3. 上传 `metadata.jsonl` 和那些 WAV 文件
4. 在 **Training** 页用它训练一个自定义声音

---

## 这个工具做什么（极简版）

想象你有一本很长的有声书——有人把一整本小说读了出来，
可能有 60 分钟，也可能有 6 小时。要教计算机像那位朗读者一样说话，
你需要大量**很短的片段**，每一段都标注上**它说的是哪些词**。
这个工具会帮你自动做出这些片段。

可以这样理解：

1. **你把有声书交给它**（一个大音频文件）。
2. **工具去听**，判断出每个词从哪里开始、到哪里结束。
3. **它把有声书切成一口大小的片段**，每段大约 10 秒，
   并尽量切在自然的停顿处（比如一句话的结尾），这样每段都不会切在词的中间。
4. **对每一段，它记下**这段里说了什么词。
5. **一个小型 AI 助手会给这些词加一点提示**——比如标出*
   重要*的词，或者朗读者在哪里停顿了很久 `...`
6. **你拿回一个 zip 文件**，里面满是片段，外加一份每个片段说了什么的清单。

这个 zip 就是你之后喂给 TTS 训练程序的东西，让计算机学会模仿那位朗读者的声音。

### 新的关键技巧：利用书

你也可以把**真正的书**（EPUB 或文本文件）交给它。

为什么？因为听音 AI（把声音变成文字的那部分）会出错——尤其是角色名。
朗读者说的是 "**Yurie**"，它可能写成 "**Yudy**"；或者 "**Kudou**" 出来变成 "**Coodo**"。

给了书之后，工具就能为每个片段**查出正确的拼写**。就像写作业时旁边放着答案：
听音 AI 听了音频，工具再去查书，然后说"啊，那段写的是
'Miyo Saimori'，那我就用这个拼写，而不是我猜的那个。"

它还能**丢掉书里没有的片段**——比如朗读者说"Audible presents…"、把章节宣告念出来，
或者任何与正片对不上的内容。

## 最简单的例子（复制粘贴）

### 只把有声书切成片段（不给书文件）

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "/home/fakemitch/Desktop/audiobook.wav" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf
```

用大白话说，它做了这些：
- 读取 `/home/fakemitch/Desktop/audiobook.wav` 这本有声书。
- 用 `models/Qwen2.5-14B-Instruct-Q6_K.gguf` 这个 AI 助手给片段打标签。
- 把结果存成当前文件夹下的 `alexandria_dataset.zip`。

### 同样的事，但同时使用书（推荐）

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "/home/fakemitch/Desktop/audiobook.wav" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf \
  --source "/home/fakemitch/Desktop/books/My Happy Marriage - Volume 01.epub"
```

唯一的区别：最后那一行告诉工具书在哪里。名字会拼对，
纯音频的东西（制作名单、开场白）会被丢掉。

### "我想给你书，但请不要丢掉任何东西"

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "/home/fakemitch/Desktop/audiobook.wav" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf \
  --source "/home/fakemitch/Desktop/books/book.epub" \
  --keep-unaligned
```

`--keep-unaligned` 的意思是"如果某个片段和书对不上，没关系——就用听音 AI 的最佳猜测，把这个片段留下来。"

### "我搞不清它跑到哪了——重来"

别用 `--resume` 就行。默认每次运行都是从头开始。如果上一次尝试留下了垃圾，会自动清理掉。

### "它跑了三小时崩了——请从停下的地方接着跑"

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "/home/fakemitch/Desktop/audiobook.wav" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf \
  --resume
```

同样的命令，只是末尾加上 `--resume`。它会跳过已经做好的片段，从停下的地方继续。（如果你当初用了 `--source`，这里也要加上——必须和最初那次运行一致。）

## 要跑多久？

在 AMD Radeon RX 9070 XT 上粗略的经验值：

- **听音频**（把声音变成文字）：每 1 小时有声书约 1 分钟。
- **给片段打标签**（慢的那部分）：每 1 小时有声书约 10 小时。所以一本典型的 6 小时有声书大约要 60 小时的机器时间。你可以让它通宵跑；如果你要用显卡，按 **Ctrl+C** 停下来，以后再 `--resume`。



**主要特性：**
- **Wav2Vec2 ASR（主力）**，带真正的 CTC 对齐词时间戳
- whisper.cpp Small.en / WhisperX-CPU 作为回退
- **LLM 预处理（富化）**——可选阶段，用本地 GGUF LLM（例如 Gemma-4-E2B）给转写分块加上说话人归属、叙述风格和情绪基调元数据
- 在 GPU 上跑 Gemma 4 / Qwen LLM 标注
- **源文引导切分**（`--source`）：把 ASR 与 EPUB/TXT 对齐，分块文本用源文拼写，丢掉纯音频内容
- **感知停顿的分块边界**：分块结束在句末标点或每个分块最后 30% 内的自然停顿处，而不是时长阈值刚好落到的那个位置
- **三阶段流水线**（ASR → 富化 → 标注），用子进程隔离以保证 ROCm 安全
- 可续跑——不会因为崩溃丢掉工作
- 每 50 个片段做一次周期存档
- 基于实测吞吐的动态 ETA
- 音频只加载一次（在内存里重采样）
- 标注期间 24 kHz 音频落盘（长有声书上省约 1.8 GB 内存）
- 通过 `rocm-smi` 记录显存和利用率

## 推荐的模型

Preparer 用一个 LLM 给文本分块做 TTS 标注。AMD 16 GB 以上显卡推荐：

| 角色 | 模型 | 显存（Q6_K） | 为什么 |
|---|---|---|---|
| **主力** | `Qwen2.5-14B-Instruct-Q6_K.gguf` | 约 12 GB | 指令跟随强，结构化输出简短 |
| **回退** | `Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf` | 约 5 GB | Qwen 加载失败时的备选 |

## 用法

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```

### 带 LLM 富化（角色多的书推荐）

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --enrich-with-llm \
  --llm-model-path /path/to/gemma-4-E2B-it-Uncensored-MAX.BF16.gguf \
  --enrich-speaker-attribution --enrich-narration-style --enrich-emotional-tone
```

这会跑**完整的三个阶段**（ASR → 富化 → 标注）。富化阶段用一个小型本地 LLM，在标注之前给每一个转写分块加上 `speaker_attribution`、`narration_style` 和 `emotional_tone` 元数据。

## 命令行选项

### 必填
- `--audio PATH` —— 输入音频文件（.wav、.mp3、.m4a、.flac、.ogg）
- `--model PATH` —— 主 GGUF 模型路径（必填）

### 可选
- `--fallback-model PATH` —— `--model` 加载失败时使用的备用 GGUF 模型
- `--chunk-size SECONDS` —— 每个分块的目标时长，单位秒（默认 `10.0`）
- `--lang CODE` —— 转写用的语言代码（默认 `en`）
- `--output PATH` —— 输出 ZIP 路径（默认 `alexandria_dataset.zip`）
- `--resume` —— 从已有的 `dataset_temp/` 续跑，而不是从头开始
- `--skip-annotation` —— 保留项；目前会被拒绝，因为只做转写的导出还没实现

### LLM 预处理（富化）
在标注之前，用一个本地 GGUF LLM 给转写分块加上元数据。
- `--enrich-with-llm` —— 启用富化阶段（在 ASR 和标注之间运行）。下面三个类别开关至少要选一个；一个都不选时命令会报错退出。
- `--llm-model-path PATH` —— 富化用的 GGUF LLM 模型文件路径（设了 `--enrich-with-llm` 时必填；推荐 Gemma-4-E2B 或类似的小模型）
- `--enrich-speaker-attribution` —— 让 LLM 提取说话人归属（例如 "main character"、"narrator"、"secondary character"）
- `--enrich-narration-style` —— 让 LLM 提取叙述风格（例如 "calm"、"energetic"、"sad"、"questioning"）
- `--enrich-emotional-tone` —— 让 LLM 提取情绪基调（例如 "happy"、"anxious"、"neutral"、"excited"）

### 说话人日志（可选）
通过 pyannote.audio 把转写出的词归属到日志出的说话人。需要 Install 创建的、隔离的 AMD Linux/ROCm `preparer_env`（包含
`requirements-diarization.txt`），以及一个已经接受
`pyannote/speaker-diarization-community-1` 条款的 Hugging Face token。
- `--diarize` —— 始终对整个音频跑完整的说话人日志
- `--auto-detect-speakers` —— 先跑一次便宜的抽样预检（三个 120 秒窗口，约 2–3 分钟），只有当某个窗口里出现占该窗口语音时间 ≥10% 的第二位说话人时，才跑完整的、昂贵的日志流程。音频短于 15 分钟，或预检跑不起来时会跳过（退回完整日志）。显式的 `--diarize` 优先级高于这个开关。
- `--hf-token TOKEN` —— Hugging Face token（默认取 `HF_TOKEN` 环境变量）；上面两个开关都需要它
- 运行 `python3 env_doctor.py`（仓库根目录）可以检查你准备用来跑 Preparer 的那个解释器里是否装了 `pyannote.audio`。

### 源文引导切分（可选）
传入与有声书对应的 EPUB 或文本文件，在 LLM 步骤之前从源头修正 ASR 误转写并丢掉纯音频内容。
- `--source PATH` —— 音频所朗读的同一本书的 EPUB 或 TXT
- `--source-threshold N` —— 保留一个分块所需的最低对齐比例（默认 `0.65`）
- `--keep-unaligned` —— 当分块低于 `--source-threshold` 时，写入 ASR 文本而不是丢弃（默认丢弃）
- `--source-start N` —— 从源文第 N 个词开始对齐（跳过自动锚定）
- `--source-start-text TEXT` —— 在源文里模糊搜索 TEXT，从那开始
- `--no-auto-anchor` —— 禁用自动源文锚定检测（从第 0 个词开始）

## 示例

### 标准运行（Qwen 主力，Gemma 回退）
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf
```

### 自定义输出路径
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio book1.wav \
  --model model.gguf \
  --output datasets/book1.zip
```

### 暂停与续跑（比如要打游戏或腾出显卡）

任何时候按 **Ctrl+C** 都能停下脚本。进度会自动保存：

```
⚠ Process interrupted by user
Partial results preserved in dataset_temp/ - rerun with --resume to continue
```

准备好继续时，用同样的参数再加上 `--resume` 重跑：

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model model.gguf \
  --resume
```

Preparer 会从它停下的确切位置接着跑——不会重复工作。

### 崩溃后续跑
如果脚本在 60 小时标注的第 50 小时崩了，恢复方法完全一样——加 `--resume` 重跑：

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model model.gguf \
  --resume
```

Preparer 会：
1. 读取已有的 `dataset_temp/metadata.jsonl`
2. 找到最后一个完成的片段及其结束时间
3. 跳过已经处理过的音频
4. 从那个位置开始继续处理新片段

### 换语言
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio spanish_book.wav \
  --model model.gguf \
  --lang es
```

### 更大的分块
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model model.gguf \
  --chunk-size 15.0
```

## 源文引导切分

当你传入 `--source <epub-or-txt>` 时，Preparer 会把每个 ASR
分块与源文做模糊对齐，然后要么：

- 在对齐比例达到 `--source-threshold`（默认 0.65）时，**用源文的拼写替换**分块的 ASR 文本。LLM 随后标注的就是已经正确的文本。角色名、方言拼写和标点都和 EPUB 里一模一样。
- 在对齐低于阈值时**整块丢弃**。不生成 WAV，也不写入 JSONL 条目。被剥掉的就是纯音频内容：朗读者开场、"this audiobook is a work of fiction" 之类的制作声明、文本里没有的章节宣告等等。

如果你更想保留低置信度的分块（用它们的 ASR 文本），传 `--keep-unaligned`。这些分块不会推进游标，所以下一个分块仍然会从上次确认匹配的正文位置去尝试对齐。

### 示例

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --source "/home/fakemitch/Desktop/books/My Happy Marriage - Volume 01.epub"
```

### 日志里会看到什么

启动时，Preparer 会报告源文加载和词表构建：
```
▶ Loading source for guided chunking: /path/to/book.epub
  ├─ Source: 284,125 characters
  ├─ 29 recurring proper nouns (gifted, godou, grotesqueries, hana, kanoko, kaya +23 more)
  ├─ 49,686 source words
  └─ Auto-anchor: entry 3 → source word 287 (62.1% match)
```

标注期间每丢弃一个分块：
```
↪ DROPPED chunk at 421.36s (source ratio 0.42 < 0.65)
```

每个保留下来的低置信度分块（只在 `--keep-unaligned` 时）：
```
↪ chunk 42 kept (ASR text); source ratio 0.51 < 0.65
```

### 源文件给错了的检测

在 LLM 标注开始之前，Preparer 会拿前约 30 个 ASR
分块与源文做预扫描。如果平均对齐低于 50%，或者超过 40% 的分块低于 60%，Preparer 会**报错中止**，而不是把好几小时的 LLM 时间浪费在一个对不上的源文上：

```
⚠ Source/audio divergence too high to proceed:
  Sampled 30 chunks — avg alignment 38%, 21 (70%) below 60%.
  Usually means a wrong edition or different translation.
  Re-run without --source, or pass --keep-unaligned to accept the ASR
  text for low-confidence chunks.
```

这通常意味着你给错了 EPUB（不同译本、删节版，或者干脆是另一本书）。用 `--source-start N` / `--source-start-text "..."` 手动重新锚定，换成正确的源文，或者不带 `--source` 运行，退回原来只用 ASR 的流程。

### 什么时候用、什么时候跳过

**在以下情况用 `--source`：** 你有与这本有声书匹配的 EPUB/TXT，并且你希望训练数据集里角色名和方言拼写是干净的，并且你能接受丢掉源文里没有的分块（章节开场、朗读者说明、附属文字）。

**在以下情况跳过 `--source`：** 你没有源文本，或者你处理的是已知只是松散地基于源文的音频（改编、重写、删节），严格丢弃会丢掉你想保留的内容。

### 对下游对比复核的影响

源文引导模式会大幅减少你用 `alexandria_compare.py` 复核结果 JSONL 时看到的东西。用户通常会做的大部分编辑（角色名 ASR 修正、方言拼写修正）都在准备阶段就已完成。对比复核变成了语律抽查——通常每本书几分钟，而不是一小时。

## 感知停顿的分块边界

当累计时长达到 `--chunk-size` 时，切分器会输出一个分块，但它不会正好切在那个词上。它会回看分块时长的最后 30%，优先切在：

1. 以句末标点（`.`、`!`、`?`）结尾的词上——对 TTS 连贯性最好；取窗口内**最靠后**的那个。
2. 同一窗口内最长的自然停顿（ASR 词的结束到下一个词开始之间间隔 ≥ 0.25 秒）。
3. 最后一个词（当前落在阈值上的那个词）——只有在上面两种都不可用时。

回看时剪下来的词会成为下一个分块的种子；不会丢音频。净效果是：分块结束在换气、小句或句子的断点上，而不是短语中间。配合 `--source` 模式，产出的 WAV 只需极少手工修改就能用于 TTS 训练。

## 处理流程

1. **校验输入**——检查音频文件和模型是否存在
2. **加载音频**——按原生采样率只读一次，重采样到 16 kHz（给 ASR）和 24 kHz（给输出）
3. **把 24 kHz 落盘**——写出 `.alexandria_audio_24k.wav` 并释放内存
4. **转写音频**（Wav2Vec2 → IFW → WhisperX-CPU）：
   - Wav2Vec2 以 30 秒为单位处理音频，重叠 3 秒
   - 每个分块用 `output_word_offsets=True` 解码，得到真正的 CTC 帧对齐
   - 重叠区域的词按词中心归属去重
5. **标注分块**：
   - 把 Gemma 4 模型完整加载到 GPU（`n_gpu_layers=-1`）
   - 遍历词片段，累积成约 10 秒的分块
   - 用 3 个片段的上下文窗口为每个分块生成 TTS 标注
   - 每个 `sample_NNNN.wav` 立即写出并立刻追加到 `metadata.jsonl`（崩溃安全）
   - 每 10 个片段记录一次动态 ETA
6. **生成输出数据集**——把 `dataset_temp/` 的内容打包到输出路径
7. **清理**——删除临时音频 + 临时目录（只在成功时）

## 可续跑能力

Preparer 在处理完一个片段后**立刻**把它的元数据写入 `dataset_temp/metadata.jsonl`（不是缓冲到最后）。每个条目包含：

```json
{
  "audio_filepath": "sample_0042.wav",
  "text": "annotated text...",
  "duration": 10.04,
  "start": 421.36,
  "end": 431.40
}
```

`--resume` 时：
- 加载已有的条目
- 最大的 `end` 时间成为续跑点
- `start < resume_time` 的词被跳过
- 新条目被追加（已有条目不动）
- 最后 5 个条目的文本成为 LLM 上下文

**主动暂停（Ctrl+C）或崩溃时**：`dataset_temp/` 会被保留（不会删除）。加上 `--resume` 以及同样的 `--audio` 和 `--model` 参数重跑，就能从停止处继续。

**成功时**：zip 创建完成后删除 `dataset_temp/`。

### 源文件安全检查

`dataset_temp/` 里会写入一个 `.source` 标记文件，内容是正在处理的音频文件的绝对路径。`--resume` 时：
- 标记与当前的 `--audio` 一致 → 安全续跑。
- 标记缺失或指向另一个文件 → **清空 `dataset_temp/` 并从零开始**（会记一条警告日志）。

这能在把同一个工作目录用于不同有声书时防止意外污染。批处理器正是依赖这项保证，才敢在每次重试时安全地传 `--resume`。

## GPU 监控

Preparer 会在关键点记录 GPU 统计：
- Wav2Vec2 模型加载之后
- 分块处理之前/期间（每 50 个分块）
- Gemma 模型加载之后
- 标注期间（每 10 个片段）

每条日志会显示：
```
GPU Usage (chunk 50/725):
  ├─ Memory: 4.52 GB / 17.10 GB (26.4%)
  └─ Utilization: 87.5%
```

内存来自 `torch.cuda.memory_allocated()`。利用率来自 `rocm-smi --showuse --json`（AMD 显卡）。注意：当 Gemma 是唯一的 GPU 使用者时，`torch.cuda.memory_allocated()` 会报 0，因为 llama-cpp-python 在 PyTorch 之外管理显存——真实情况请看 `rocm-smi`。

## 进度跟踪

流水线分**三个阶段**，每个阶段作为独立子进程启动，以实现 ROCm 隔离：

### 阶段 1：Wav2Vec2（ASR）
```
↳ Chunk 50/725 | avg 0.51s/chunk | ETA 5m 47s
```

### 阶段 2：LLM 富化（可选，需要 `--enrich-with-llm`）
```
▶ Created 72 chunks for LLM enrichment
▶ Running LLM enrichment: python llm_enricher.py --model-path ... --input-file ... --output-file ...
Enriching chunk: 10.00s - 20.14s
...
✓ LLM Enrichment Phase completed successfully.
```

### 阶段 3：标注
```
↳ Progress: 50/4713 chunks | Avg: 46.2s/chunk | Elapsed: 38m 42s | ETA: 59h 31m
```

时间格式会自适应：超过 1 小时显示 `Nh Mm`，超过 1 分钟显示 `Mm Ss`，否则显示 `Ns`。

## 日志

详细日志写到 `logs/alexandria_preparer_YYYYMMDD_HHMMSS.log`：
- 每一步的完整 DEBUG 级记录
- GPU 统计快照
- 子进程命令行和输出
- 异常 traceback（如有）

在另一个终端里跟踪日志：
```bash
tail -f logs/alexandria_preparer_*.log
```

## ASR 方法优先级

| # | 方法 | 设备 | 上下文 | 词时间戳 |
|---|---|---|---|---|
| 1 | **Wav2Vec2**（主力） | GPU | 30 秒分块 + 3 秒重叠 | 真实（CTC 帧对齐） |
| 2 | whisper.cpp Small.en | 原生 GPU/CPU | 连续 | 原生，零时长词会保守地合并 |
| 3 | WhisperX-CPU | CPU | 不适用 | 通过强制对齐得到 |

优先选 Wav2Vec2，因为它保留了重叠分块的上下文，并有 CTC 对齐的逐词时间戳。如果它不可用，或者它的输出没通过时间戳校验，Preparer 会使用 `install.js` 装好的、钉住的 whisper.cpp Small.en；WhisperX 仍是最后的 CPU 回退。Small.en 有意只支持英语，所以非英语请求会跳过它，而不是偷偷用错模型。

## 性能建议

- **显存**——Gemma 用约 8 GB；Wav2Vec2 用约 2 GB。两者在 16 GB 以上的显卡上都很宽裕。
- **内存**——把 24 kHz 音频落盘，能在长达数小时的标注阶段省下约 1.8 GB。
- **磁盘空间**——每本有声书留出约 5–10 GB 空闲（临时 WAV + 片段 WAV + 最终 zip）。
- **运行时间**——在 AMD Radeon RX 9070 XT 上，一本约 5.4 小时的有声书、10 秒分块，预期约 6 分钟 Wav2Vec2 + 约 60 小时 Gemma 标注。

## 故障排查

### 找不到音频文件
```
Error: Audio file not found: audiobook.wav
```
- 检查路径是否正确；脚本的当前工作目录就是你启动它时所在的位置。

### 找不到模型文件
```
Error: Model file not found: ...
```
- 下载 Gemma GGUF 模型（例如 `download_model.py`），或者提供 `--model PATH`。

### `rocm-smi unavailable`
- 确认 `/opt/rocm/bin/rocm-smi` 存在并且可执行。
- 没有它显存仍然能用；只是拿不到利用率百分比。

### `expandable_segments not supported`
- 来自 PyTorch 的提示性警告——AMD ROCm 不支持那个内存优化。PyTorch 会退回标准分配。不需要处理。

### 续跑选错了文件
- Preparer 通过 `dataset_temp/.source` 标记跟踪源文件。标记对不上时，会在重新处理之前自动清空 `dataset_temp/`（并给出警告）。
- 如果你无论如何都想强制从头开始，手动删掉 `dataset_temp/`，或者不加 `--resume`。

### 磁盘空间不足
- 用 `du -sh dataset_temp/ .alexandria_audio_24k.wav` 检查。
- 腾出空间，然后用 `--resume` 重跑。

### 超大 WAV（>4 GiB）——文件头报出的时长很小
症状：一个几 GB 的有声书 WAV 报出的时长只有几分钟，而不是几小时。启动日志示例：
```
[WARNING] ⚠ Oversized WAV detected: header says 135.3s (2.3 min) but file size
implies 24477.2s (6.80 hr). WAV data-chunk-size field is 32-bit and has
wrapped — ffmpeg `-ignore_length 1` will be used to read the full audio.
```

原因：标准 WAV 用 32 位无符号字段记录 `data` 块大小，到 4 GiB 就溢出。在 44.1 kHz 立体声 PCM_16 下，这大约在 6.8 小时发生，所以任何由没有改用 RF64/WAV64 的工具生成的完整有声书 WAV，文件头都是回绕的。`soundfile` 和 `librosa.load` 会照信这个错误的文件头，静默地截断到 `filesize mod 4 GiB` 的音频——每一次在超大文件上"完成"的运行，其实只是第一段（例如 6.8 小时文件只处理了 2 分钟，27 小时文件只处理了 22 分钟）。

Preparer 怎么处理：`validate_inputs` 会把文件大小与文件头隐含的字节数比较，不一致时打印上面那条警告，并让加载走 ffmpeg（`-ignore_length 1`）。24 kHz 的临时文件直接流式落盘，16 kHz 的 ASR 缓冲区通过管道进入 float32 numpy 数组。不会在原生采样率上生成中间文件，所以无论音频多长，内存峰值都是有界的。

你该做什么：正常情况下什么都不用做——Preparer 检测到回绕时会自动修复。如果你想把 WAV 重新编码一次以永久解决（并避免重复的 ffmpeg 解码），可以转成 FLAC 或 RF64-WAV，例如：
```
ffmpeg -ignore_length 1 -i input.wav -c:a flac output.flac
```

## API（以编程方式调用）

### Python（子进程）
```python
import subprocess

result = subprocess.run([
    "python", "alexandria_preparer_rocm_compatible.py",
    "--audio", "audiobook.wav",
    "--model", "Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf",
    "--chunk-size", "10.0",
    "--lang", "en",
    "--output", "my_dataset.zip",
], check=True)
```

### JavaScript（Node.js）
```javascript
const { execFile } = require("child_process");

execFile("python", [
  "alexandria_preparer_rocm_compatible.py",
  "--audio", "audiobook.wav",
  "--model", "model.gguf",
  "--output", "my_dataset.zip",
], { maxBuffer: 1024 * 1024 * 100 }, (err, stdout, stderr) => {
  if (err) throw err;
  console.log(stdout);
});
```

### Curl（不适用）
Preparer 是 CLI 工具，不是 HTTP 服务。如果你需要网络访问，请把它包进一个 Flask/FastAPI 服务器。

## 另见

- `BATCH_PROCESSOR_GUIDE_CN.md` —— 按顺序处理多本有声书，支持跨文件续跑
- `../../README.md` —— 项目概览
