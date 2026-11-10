[English](BATCH_PROCESSOR_GUIDE.md) | [中文](BATCH_PROCESSOR_GUIDE_CN.md) | [日本語](BATCH_PROCESSOR_GUIDE_JA.md)

# Alexandria 批处理器指南

用一条命令按顺序处理多个有声书文件。

## 这个工具做什么（大白话）

批处理器会把 Preparer（见 `PREPARER_GUIDE_CN.md`）跑在**多个有声书文件上**。你不用一个一个手动跑 Preparer，只要给它一份文件清单（或者一整个文件夹），它就会按顺序一个个处理完。

### 这是给谁用的？

- **你有多本有声书**，想为它们全部创建训练数据集
- **如果你只有一本有声书，就不需要它**——直接用 Preparer 就行

### 它和 Preparer 是什么关系

批处理器只是 Preparer 外面的一层包装。它会：
1. 接收一份音频文件清单
2. 对每一个跑 Preparer
3. 用唯一的文件名保存每份输出，避免互相覆盖
4. 全部做完后给你一份汇总

---

## 给非程序员的快速开始

### 第 1 步：把文件归拢好

把所有要处理的有声书 WAV 文件放进一个文件夹。例如：
```
/home/fakemitch/Desktop/my_audiobooks/
  ├── book1.wav
  ├── book2.wav
  └── book3.wav
```

### 第 2 步：打开终端

- **Windows：** 按 `Win + R`，输入 `cmd`，回车
- **Mac：** 打开 Spotlight（Cmd + Space），输入 `terminal`，回车
- **Linux：** 打开应用菜单，搜索 "Terminal"

### 第 3 步：运行批处理器

复制粘贴这条命令（把文件夹路径换成你实际的路径）：

```bash
cd ~/.pinokio/api/alexandria-audiobook.git
./app/env/bin/python alexandria_batch_processor.py \
  --folder "/path/to/your/audiobooks" \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf
```

### 第 4 步：等着

每 5 小时音频大约要 1 小时。如果你有 3 本各 5 小时的有声书，总共大约 3 小时。

### 第 5 步：查看结果

做完后，每本有声书都会有自己独立的输出文件夹，里面是一个 `.zip` 数据集文件。

---

## 概览

批处理器让你可以排队处理多个音频文件。每个文件依次（串行）处理，各自产出一份完整的标注数据集。

**主要特性：**
- 一条命令处理多个文件
- 串行处理（一次一个，没有并行的开销）
- 自动命名输出文件（包含原始文件名）
- 完整的日志和进度跟踪
- 带统计信息的 JSON 结果汇总
- 所有文件都用 GPU 加速
- 每个文件的时间估计

## 快速开始

三种输入方式选一种：

**A. 逐个列出文件**（位置参数，空格分隔）
```bash
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model Qwen2.5-14B-Instruct-Q6_K.gguf
```

**B. 扫描整个文件夹**（用 `--folder` 参数——中间必须有空格）
```bash
python alexandria_batch_processor.py --folder /path/to/audiobooks --model Qwen2.5-14B-Instruct-Q6_K.gguf
```

**C. 两者混用**（文件夹 + 额外的文件）
```bash
python alexandria_batch_processor.py extra.wav --folder /path/to/audiobooks --model Qwen2.5-14B-Instruct-Q6_K.gguf
```

> **带空格的路径必须加引号。** 如果你的文件夹是 `/home/you/Desktop/New folder`，不加引号的话 shell 会把它拆成两个参数：
> ```bash
> # WRONG — shell splits at the space
> python alexandria_batch_processor.py --folder /home/you/Desktop/New folder --model model.gguf
>
> # RIGHT — quotes keep the path as one argument
> python alexandria_batch_processor.py --folder "/home/you/Desktop/New folder" --model model.gguf
> ```

> **别忘了 `--folder` 后面的空格。** 写成 `--/home/...`（没有空格）会让 argparse 把整个路径当成一个未知的参数名。

## 用法

### 单个文件（原始脚本）
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```

### 多个文件（批处理器）
```bash
python alexandria_batch_processor.py \
  audiobook1.wav audiobook2.wav audiobook3.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```

### 文件夹扫描（批处理器）
```bash
python alexandria_batch_processor.py \
  --folder "/path/to/audiobooks" \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```
文件夹路径里有空格就加引号。

## 命令行选项

### 位置参数
- `audio_files` - 一个或多个要处理的音频文件（必填，空格分隔）

### 可选参数
- `--folder DIR` - 扫描文件夹里所有受支持的音频文件（`.wav .flac .ogg`），按字母排序；可以和逐个列出的文件混用
- `--model PATH` - 主 GGUF 模型文件路径（必填，推荐 `Qwen2.5-14B-Instruct-Q6_K.gguf`）
- `--fallback-model PATH` - 可选的备用 GGUF 模型，在 `--model` 加载失败时使用（例如 `Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf`）
- `--chunk-size SIZE` - 每个分块的目标时长，单位秒（默认 10.0）
- `--lang CODE` - 转写用的语言代码（默认 en）
- `--force` - 即使 `alexandria_dataset_<name>.zip` 已存在也重新处理（默认跳过）

### 源文引导模式（可选）

启用后，批处理器会把源文引导切分的参数转发给每个 Preparer 子进程。该模式具体做什么见 `PREPARER_GUIDE_CN.md`；批处理层面的概括是：角色名会按 EPUB 正确拼写，纯音频的段落（制作名单、章节开场）会被自动丢掉。

- `--source-folder DIR` - 存放源文 `.epub` 或 `.txt` 文件的文件夹。每个音频文件按 basename 匹配源文件：`audio/Book1.wav` 会去找 `<source-folder>/Book1.epub`（优先）或 `<source-folder>/Book1.txt`。没有匹配源文的音频文件会退回旧的仅 ASR 模式，并给出警告。与 `--source` 互斥。
- `--source PATH` - 一个源文件，应用到批处理里的**每一个**音频文件。当你处理同一本书的多个录音（例如不同朗读者）时很有用。与 `--source-folder` 互斥。
- `--source-threshold N` - 保留一个分块所需的最低对齐比例（默认 0.65）。转发给 Preparer 的 `--source-threshold`。
- `--keep-unaligned` - 不丢弃低置信度的分块，改用 ASR 文本。转发给 Preparer 的 `--keep-unaligned`。

批处理开始时，处理器会打印哪些文件匹配上了、多少个没匹配上，这样你可以在它啃掉 60 小时音频之前就发现重命名问题：

```
├─ Source-guided: matching from /path/to/books/
│   3 matched, 1 no match (legacy ASR-only for those)
│     no match: 'Audiobook4'
├─ Source threshold: 0.65 (strict-drop)
```

## 暂停与续跑

### 运行中暂停（比如要打游戏或腾出显卡）

任何时候按 **Ctrl+C** 都能停下批处理器。当前文件的存档点在 `dataset_temp/` 里，会自动保留。准备好继续时，原样重跑同一条命令即可——不需要额外参数：

```bash
# Stopped mid-run with Ctrl+C — just rerun the same command to resume
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf
```

批处理器会：
- **跳过**输出 zip 已存在的文件（已完成的文件）
- **续跑**被中断的文件，从它最后的存档点继续（不重复工作）
- **全新处理**还没开始的文件

## 可续跑能力

批处理器支持**两层续跑**：

**1. 文件级跳过**——输出 zip（`alexandria_dataset_<name>.zip`）已存在的文件，在后续运行中会被跳过。

**2. 文件内续跑**——如果某个文件在标注中途被中断（Ctrl+C 或崩溃），批处理器会自动给 Preparer 传 `--resume`。Preparer 读取自己的存档点，从最后一个完成的片段继续。

Preparer 在 `dataset_temp/` 里的 `.source` 标记保证我们绝不会误续到另一个文件的部分工作上——如果标记与当前音频文件不符，`dataset_temp/` 会被清空并从头处理。

```bash
# First run - processes all 3 files
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf
# (book1 completes, book2 crashes 50h into annotation)

# Second run:
#  - book1.wav skipped (zip exists)
#  - book2.wav resumes from last checkpoint (continues from segment ~4000)
#  - book3.wav processed fresh
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf

# Force reprocess everything (no skip, no resume)
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf --force
```

## 实时输出流

批处理器会**实时**转发主脚本的输出，所以你可以监控：
- 转写进度（正在处理的 Wav2Vec2 分块）
- GPU 利用率百分比
- 标注进度，含已用时间和剩余时间估计
- 任何发生的警告或错误

再也不用等上几个小时、心里嘀咕是不是卡住了！

## 示例

### 处理 3 本有声书
```bash
python alexandria_batch_processor.py \
  book1.wav book2.wav book3.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf
```

### 用自定义分块大小处理
```bash
python alexandria_batch_processor.py \
  book1.wav book2.wav \
  --model model.gguf \
  --chunk-size 15.0
```

### 用不同语言处理
```bash
python alexandria_batch_processor.py \
  spanish_book.wav french_book.wav \
  --model model.gguf \
  --lang es
```

### 源文引导：每本有声书配一个 EPUB 的批处理

每个音频文件按 basename 与同级源文件配对。所以
`audio/MyHappyMarriage.wav` 会配 `books/MyHappyMarriage.epub`，
以此类推。没有匹配源文的音频文件仍会处理——只是走旧的
仅 ASR 模式（启动时会有警告）。

```bash
app/env/bin/python alexandria_batch_processor.py \
  --folder /home/fakemitch/Desktop/audiobooks/ \
  --source-folder /home/fakemitch/Desktop/books/ \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf
```

### 源文引导：同一个 EPUB 应用到批处理里的每个文件

当你有多份同一本书的录音（不同朗读者、不同版本）时，用 `--source` 而不是 `--source-folder`，让同一个源文件应用到所有文件：

```bash
app/env/bin/python alexandria_batch_processor.py \
  narrator_a.wav narrator_b.wav narrator_c.wav \
  --source "/home/fakemitch/Desktop/books/My Happy Marriage - Volume 01.epub" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf
```

### 源文引导 + 更严格的阈值

如果默认的 0.65 阈值在丢弃你想保留的段落，就放宽它（或者用 `--keep-unaligned` 永不丢弃）：

```bash
# Loosen threshold to 0.55 — keep more borderline chunks
app/env/bin/python alexandria_batch_processor.py \
  book.wav --model model.gguf \
  --source book.epub \
  --source-threshold 0.55

# Or: keep everything (use ASR text for low-confidence chunks)
app/env/bin/python alexandria_batch_processor.py \
  book.wav --model model.gguf \
  --source book.epub \
  --keep-unaligned
```

## 输出文件

对每个输入文件，批处理器会创建：
- `alexandria_dataset_<filename>.zip` - 标注好的数据集
- 从 `alexandria_dataset.zip` 自动重命名而来，以避免冲突

示例：
```
Input:  audiobook1.wav
Output: alexandria_dataset_audiobook1.zip

Input:  audiobook2.wav  
Output: alexandria_dataset_audiobook2.zip
```

## 日志

批处理器在 `logs/` 目录里创建详细日志：
- `logs/alexandria_batch_YYYYMMDD_HHMMSS.log` - 完整的处理日志

## 结果汇总

处理完成后会创建一个 JSON 汇总：
- `batch_results_YYYYMMDD_HHMMSS.json` - 处理结果

内容包括：
- 每个文件的结果（成功/失败）
- 输出文件路径和大小
- 每个文件的处理时间
- 总处理时间
- 时间戳

示例：
```json
{
  "timestamp": "2026-05-16T16:30:00",
  "total_time_seconds": 14400,
  "results": {
    "succeeded": [
      {
        "file": "book1.wav",
        "output": "alexandria_dataset_book1.zip",
        "output_size_mb": 250.5,
        "time": "2h 15m",
        "time_seconds": 8100
      }
    ],
    "failed": [],
    "skipped": []
  }
}
```

## 处理流程

1. **校验** - 检查所有文件存在且是有效的音频格式
2. **模型检查** - 确认 Gemma 模型可访问
3. **GPU 监控** - 开始时记录显存和利用率
4. **串行处理** - 逐个处理每个文件：
   - 加载音频（16kHz + 24kHz）
   - **用 Wav2Vec2 转写**（GPU 主力，30 秒重叠分块以保留上下文）
     - 需要时回退到 whisper.cpp Small.en
     - 需要时回退到 WhisperX-CPU
   - **用 Gemma 标注分块**（GPU 加速）
     - 每 10 个片段监控一次显存和利用率
     - 已用时间按小时/分钟格式显示
     - 剩余时间实时估计并更新
   - 生成带标注的音频片段
   - 打包成 ZIP 文件
   - 重命名输出以避免冲突
   - 文件之间记录 GPU 统计
5. **汇总** - 生成含批处理统计信息的处理报告

## GPU 监控与进度跟踪

### 实时监控
- **显存**：在以下时机跟踪已分配/总显存及百分比：
  - Wav2Vec2 模型加载后
  - Wav2Vec2 分块处理之前/期间（每 50 个分块）
  - Gemma 模型加载后
  - 标注期间（每 10 个片段）
  - 批处理中文件之间

- **GPU 利用率**：通过 rocm-smi 显示实际的 GPU 负载百分比
  - 帮助确认 GPU 确实在被使用
  - 能识别"声称在用其实没用"的假象
  - 示例：`Utilization: 45.3%`

### 进度跟踪
- **时间格式**：所有时间估计都按小时/分钟显示
  - 预计总量：`~60h 13m`，而不是只给秒数
  - 已用时间：`Elapsed: 7m 40s`
  - 剩余时间：`Remaining: ~60h 12m`
  - 每 10 个标注片段更新一次

### 进度输出示例
```
GPU Usage (chunk 50/725):
  ├─ Memory: 4.52GB / 16.00GB (28.3%)
  └─ Utilization: 87.5%

Progress: 10/4713 chunks | Elapsed: 7m 40s | Remaining: ~60h 12m
```

## 性能建议

- **GPU 加速** - 所有转写和标注都用 GPU，并实时监控
- **串行处理** - 没有并行开销，每个文件都能独占 GPU
- **磁盘空间** - 每本有声书确保约 5-10 GB 空闲空间
- **内存监控** - 文件之间会清理 GPU 缓存，内存占用都有日志

## 时间估计

按每本 19,500 秒的有声书约 5.5 小时估算：
- 转写：约 15-20 分钟（GPU 加速）
- 标注：约 4-5 小时（GPU 加速，每个分块约 46 秒）

合计 = 每本有声书约 4.5-5.5 小时

3 本有声书：总共约 13-16 小时

## 故障排查

### "unrecognized arguments" 或 "--folder path is not a directory"
```
error: unrecognized arguments: --/home/you/Desktop/New
Error: --folder path is not a directory: /home/you/Desktop/New
```
几乎总是 shell 引号问题，不是 bug：
- **少了空格：** `--/home/...` 应该写成 `--folder /home/...`（参数和路径之间要有空格）
- **空格没加引号：** 任何含空格的路径都要用双引号包起来：`--folder "/home/you/Desktop/New folder"`
- **行尾反斜杠 + 空格：** 在多行命令里，确保 `\` 是该行最后一个字符（后面没有尾随空格）

### 找不到文件
```
Error: File not found: audiobook.wav
```
- 检查文件路径是否正确
- 如果文件在别的目录，用绝对路径
- 含空格的路径要加引号：`"/path with spaces/book.wav"`

### 不支持的格式
```
Warning: Unsupported format: audiobook.aac
```
- 支持的格式：`.wav`、`.flac`、`.ogg`（通过 soundfile/libsndfile 原生支持）
- `.mp3` 和 `.m4a` 不可靠地支持——先转成 `.wav`：`ffmpeg -i input.mp3 output.wav`

### 找不到模型
```
Error: Model file not found
```
- 确认模型路径正确
- 缺 Gemma-4 模型的话下载它

### GPU 不可用
```
Warning: GPU not available - running on CPU (slower)
```
- 检查 CUDA/ROCm 安装
- 确认显卡驱动已安装
- 在 CPU 上单个文件会慢得多

### 空间不足
```
Error: No space left on device
```
- 清理旧数据集
- 腾出磁盘空间（每个文件需要约 10GB）
- 用 `du -sh alexandria_dataset*.zip` 看看旧文件

### 数据集看起来完成了，但只覆盖了每本有声书的一小部分
如果你的 `.wav` 文件每个都大于约 4 GiB，而且 Preparer 的汇总在本该是几小时的书上显示"Total audio in dataset: 1.5 min"，那你就踩到了 >4 GiB 的 WAV 文件头回绕 bug——WAV 的 data-chunk-size 字段是 32 位的，会静默截断回绕点之后的一切。Preparer 现在会自动检测并让加载走 ffmpeg，但用旧加载器生成的数据集都该重跑。详情见 `PREPARER_GUIDE_CN.md` → "超大 WAV"。

## 进阶用法

### 处理文件夹里的所有文件
```bash
python alexandria_batch_processor.py \
  --folder /path/to/audiobooks \
  --model model.gguf
```

文件按字母顺序取。支持的格式：`.wav .flac .ogg`——这些由 soundfile/libsndfile 原生支持。`.mp3` 或 `.m4a` 文件请先转成 `.wav`（例如 `ffmpeg -i input.mp3 output.wav`）。

你也可以把文件夹和逐个列出的文件混用：
```bash
python alexandria_batch_processor.py extra.wav \
  --folder /path/to/audiobooks \
  --model model.gguf
```

### 带错误恢复的处理
批处理器会自动：
- 开始之前校验所有文件
- 某个文件失败时继续处理下一个
- 报告哪些文件成功/失败
- 把结果保存到 JSON

### 监控进度
```bash
# In another terminal
tail -f logs/alexandria_batch_*.log
```

## 开发说明

批处理器：
- 包装 `alexandria_preparer_rocm_compatible.py`
- 处理文件名冲突
- 记录耗时和结果
- 记录所有操作
- 清理临时文件

要改行为，直接编辑 `alexandria_batch_processor.py`。
