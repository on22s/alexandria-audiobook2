[English](COMPARE_GUIDE.md) | [中文](COMPARE_GUIDE_CN.md) | [日本語](COMPARE_GUIDE_JA.md)

# Alexandria 对比指南

把你 `metadata.jsonl` 里的转写和原始 EPUB 或文本文件做差异对比。每一处改动都需要你确认——不会自动写入任何东西。

## 这个工具做什么（大白话）

Preparer 听过你的有声书、把听到的内容写下来之后，有时会听错词——尤其是人名、生僻词，或者发音相近的词。对比工具让你**把 AI 听到的内容和书里实际写的内容对照起来**，逐个修掉错误。

### 这是给谁用的？

- **你跑过 Preparer**，得到了一个 `metadata.jsonl` 文件
- **你有原始的书**（.epub 或 .txt），也就是朗读时用的那一本
- **你想在把数据用于训练之前修掉转写错误**

### 为什么要费这个劲？

如果 AI 把 "Sherlock" 听成了 "sure lock"，TTS 训练就会学到错误的词。修掉这些错误，你训练出的声音会更准确。

---

## 给非程序员的快速开始

### 第 1 步：解出你的数据集

如果你的 Preparer 输出是一个 `.zip` 文件，先解压：
```bash
unzip alexandria_dataset.zip -d my_dataset/
```

你现在应该有一个文件夹，里面有 `metadata.jsonl` 和许多 `sample_NNNN.wav` 文件。

### 第 2 步：打开终端

- **Windows：** 按 `Win + R`，输入 `cmd`，回车
- **Mac：** 打开 Spotlight（Cmd + Space），输入 `terminal`，回车
- **Linux：** 打开应用菜单，搜索 "Terminal"

### 第 3 步：运行对比工具

复制粘贴这条命令（把路径换成你实际的路径）：

```bash
cd ~/.pinokio/api/alexandria-audiobook.git
./app/env/bin/python alexandria_compare.py \
  --jsonl /path/to/my_dataset/metadata.jsonl \
  --source /path/to/original/book.epub
```

### 第 4 步：检查并修正错误

工具会逐条显示 AI 转写与书里不一致的分块。你会看到类似这样的东西：

```
[ANNOTATED] sure lock looked at the clock
[ORIGINAL]  Sherlock looked at the clock
[a]ccept [m]erge [k]eep [e]dit [s]kip [q]uit
```

**每个键做什么：**
- **`a` (accept)：** AI 是对的——保留 AI 的版本
- **`m` (merge)：** 合并两个版本——通常这是最好的选择，因为它既保留书里正确的拼写，又保留 AI 加上去的演绎标记
- **`k` (keep)：** 原样保留 AI 的版本
- **`e` (edit)：** 自己输入修正
- **`s` (skip)：** 这一条先跳过（以后可以回来）
- **`q` (quit)：** 保存进度并退出

**小技巧：** 大部分时候按 **`m` (merge)**。它保留书里正确的词，同时保住 AI 加的演绎提示（比如 "*whispered*" 或 "*excitedly*"）。

### 第 5 步：使用修正后的数据

对比工具跑完会保存一个修正过的 `metadata.jsonl`。训练声音时请用这个修正后的文件，而不是原始的。

---

## 概览

对比工具会：
1. 加载你的 JSONL 和原始源文本
2. 把每个约 10 秒的分块与源文中对应的段落做模糊对齐
3. **自动通过**已经足够匹配的条目（阈值可配置）
4. **向你展示**有差异的条目，等你的决定
5. 只按你批准过的改动写出修正后的 JSONL

## 依赖

纯文本源不需要额外安装任何包。支持 EPUB 需要：
```bash
uv pip install ebooklib beautifulsoup4
# or
pip install ebooklib beautifulsoup4
```

对比脚本还会 import `alexandria_alignment.py`（同目录下的兄弟模块），用于源文加载、模糊对齐和专有名词词表。两个文件必须放在一起。

## 你在 Preparer 里用过 `--source` 吗？

如果这个 JSONL 是用 `alexandria_preparer_rocm_compatible.py --source ...` 生成的，角色名的 ASR 误转写和方言拼写已经在准备阶段修正过了——分块文本用的是源文的拼写，而不是 ASR 转写出来的那个。这种情况下，对比复核多半只是语律上的抽查：扫一遍看起来不对的 `*emphasis*` 和 `...` 停顿标记，用 `[e]dit` 修几个离群的，就完了。

如果生成 JSONL 时没有用 `--source`，每个分块的文本都是 ASR 的原始输出，你就得靠对比复核去修姓名、方言，以及 Preparer 不知道该丢掉的纯音频内容。这才是这个脚本最初设计的经典流程。

## 重要：必须在真实终端里运行

这个脚本是**交互式**的——它会在每个条目处等你的键盘输入。你必须在真实的终端会话里运行它，不能通过 AI 编码工具或任何其他不支持 stdin 的环境。

```bash
cd /path/to/alexandria-audiobook2
app/env/bin/python alexandria_compare.py \
  --jsonl /path/to/metadata.jsonl \
  --source /path/to/original.txt
```

然后在 `>` 提示符处直接输入你的选择（`a`、`k`、`e`、`s` 或 `q`）。

## 用法

```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source original_book.epub
```

或者用纯文本文件：
```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source original_book.txt
```

如果你的数据集已经被打成 zip，先从里面解出 `metadata.jsonl`：
```bash
unzip alexandria_dataset.zip dataset_temp/metadata.jsonl
```

## 选项

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--jsonl PATH` | 必填 | `metadata.jsonl` 的路径 |
| `--source PATH` | 必填 | 原始的 `.epub` 或 `.txt` |
| `--output PATH` | `<jsonl>_corrected.jsonl` | 修正后的 JSONL 写到哪里 |
| `--threshold N` | `0.90` | 相似度高于此值的条目自动通过 |
| `--review-all` | 关 | 无论相似度多高都逐条显示 |
| `--reset` | 关 | 丢弃已保存的进度，从头开始 |
| `--source-start N` | 关 | 手动从源文第 N 个词开始（跳过自动锚定） |
| `--source-start-text "..."` | 关 | 在源文里模糊搜索一个短语，从那开始 |
| `--no-auto-anchor` | 关 | 禁用自动锚定；从源文第 0 个词开始 |
| `--review-preanchor` | 关 | 逐条复核锚点之前的条目（默认：自动保留） |

## 源文加载

启动时你会看到一两行诊断信息，类似：

```
Loading source  : /path/to/book.epub
  284,125 characters
  29 recurring proper nouns (gifted, godou, grotesqueries, hana, kanoko, kaya, keiko, kiyoka +21 more)
```

专有名词那一行是个快速抽查，确认脚本从源文里认出了正确的角色名。它会扫描在句子中间多次出现的、首字母大写的词——角色名、地名，以及像 `Grotesqueries` 或 `Spirit Sight` 这种故事特有的词。对齐时用它们来放宽边界接受标准：当某个被 ASR 弄坏的分块词（`coodo`、`youth`、`caia`）在源文里紧挨着这些名字之一时。如果样例看起来不对（缺主要角色，全是常用词），对齐仍然能工作，但在专有名词边界上可能需要更多手工 `[e]dit`。

脚本在加载源文时还会自动清理两类反复出现的 EPUB 抽取伪影：
- **字母-数字-字母的错乱**（`thos1e` → `those`，`Kars1a` → `Karsa`），由 OCR 在字母之间插入杂散数字造成。
- **被拆开的变音符号**（`fianc é` → `fiancé`，`fiancé e` → `fiancée`），由 EPUB 抽取把预组合的拉丁变音符号和它的主体分开造成。

这些你都不用管——它们自动发生。

## 初始对齐（音频开头 vs 文本前言）

有声书和源文本很少从同一个地方开始：
- 音频可能以**制作名单**（"Audible presents…"）、**朗读者开场**或文本里没有的**章节宣告**开头。
- 文本可能以朗读者不会读的**版权页**、**目录**或**献词**开头。

在一次全新的运行里，对比工具会做一次**自动锚定搜索**：它扫描整个源文，找出与第一个可靠的 JSONL 条目最匹配的位置，然后从那里开始对齐。

你会看到类似这样的输出：
```
🔍 Searching for initial alignment anchor...
  ✓ JSONL entry 3 anchors at source word 287 (62.1% match)
  Source preview: "It was a warm Saturday morning when Sakuta..."

  ⚠ 3 JSONL entries before the anchor have no matching source text
     (likely audio intro/credits not present in the text)
     ✓ Auto-kept as-is (use --review-preanchor to review them individually)
```

### 自动锚定锚错了怎么办

如果锚点落到了错误的位置（比如某个短语在书里重复出现，它找错了那一处），就手动覆盖：

```bash
# Skip the first 412 words of source (e.g., copyright + TOC + dedication)
app/env/bin/python alexandria_compare.py \
  --jsonl metadata.jsonl --source book.txt --source-start 412

# Or search for a distinctive opening phrase
app/env/bin/python alexandria_compare.py \
  --jsonl metadata.jsonl --source book.txt \
  --source-start-text "It was a warm Saturday morning"

# Or disable auto-anchor entirely and start at word 0
app/env/bin/python alexandria_compare.py \
  --jsonl metadata.jsonl --source book.txt --no-auto-anchor
```

### 锚点之前的条目

位于锚点之前的 JSONL 条目没有对应的源文。默认会原样自动保留（因为它们通常是音频独有的开场内容，你不会想覆盖掉）。用 `--review-preanchor` 可以改为逐条复核。

注意：自动锚定只在**第一次**运行时执行。从存档点续跑时，会直接使用保存下来的游标位置——你之前的对齐决定会被保留。

## 中途重新对齐

即使初始锚点很好，源文和音频也可能在书的中途漂开（比如音频读了一个文本里没有的章节标题，或者跳过了一段文本里有的内容）。脚本会自动检测这种情况：

- 当某个条目与游标附近的源文跨度得分很低时，脚本会**往前做更大范围的搜索**（最多 3,000 个词）。
- 如果找到一个有把握的匹配，游标就会**跳到那里**。
- 如果前方任何地方都没有有把握的匹配，该条目会被标记为没有对应源文，并显示为：

  ```
  ORIGINAL  :  (no matching passage found in source within search range)
  ```

  对这类条目，`[a]` 接受原文的动作被禁用；请用 `[k]` 保留、`[e]` 编辑或 `[s]` 跳过。

这通常能抓到：只在音频里念的章节宣告、音频片尾的制作名单，以及任何只在音频里出现的朗读者插入内容。

## 复核界面

每个低于阈值的条目都会这样显示：

```
────────────────────────────────────────────────────────────────────────
Entry 42/4713  sample_0042.wav  7m 1s → 7m 11s  Match: 78.3%
────────────────────────────────────────────────────────────────────────
ANNOTATED :  He *walked* ... slowly across the room .... pausing to look
ORIGINAL  :  He walked slowly across the room, pausing to look

  TRANS diff:  He walked [slowly] across the room [pausing] to look
  ORIG  diff:  He walked [slowly] across the room [,] [pausing] to look

  [a] accept original   [k] keep annotation   [e] edit manually   [s] skip for now   [q] quit & save
  >
```

**差异的颜色：**
- `RED` 的词——在转写里有，原文里没有（多出来的/错的）
- `GREEN` 的词——在原文里有，转写里没有（漏掉的）
- 正常——匹配上的词

**ANNOTATED** 显示包含 TTS 标记（`...`、`*word*`）的完整文本。差异行比较的是去掉标记并归一化后的词，所以标点噪声不会干扰对比。

## 你的选择

| 键 | 会发生什么 |
|---|---|
| `a` | 用纯源文替换 JSONL 文本。**会剥掉所有语律标记。** |
| `m` | **合并**——用源文的词，但保留 LLM 的 `*emphasis*` 和 `...` 停顿。只有当标注里有值得保留的标记时才显示。 |
| `k` | 原样保留当前的标注文本 |
| `e` | 输入自定义替换内容（你写的会原封不动进入 JSONL） |
| `s` | 跳过这个条目——它保持不变，本次会话不会再出现（下次运行还会显示） |
| `q` | 保存进度并退出——重跑同一条命令即可继续 |

## 别让声音变平：优先用 `[m]erge`

Preparer 的 LLM 会给 JSONL 加上语律标记：
- `*word*`——重读词上的**强调**
- `...`——自然停顿，`....`——更长的停顿

正是这些标记让训练出的 TTS 声音有表现力。如果你对很多条目都用 `[a]ccept original`，你就用纯源文的词替换了标注文本——**剥掉了所有语律提示**，把声音推向平板/单调的输出。

`[m]erge` 选项会修正词，同时保留标记：

```
ANNOTATED :  HE *WALKED* SLOWLY... ACROSS THE ROOM
ORIGINAL  :  He walked slowly across the room,
MERGED    :  He *walked* slowly ... across the room,
            (correct spelling + capitalisation + the LLM's emphasis and pause)
```

合并会自动处理几种棘手的形态：
- **多词强调**——`*Trull Sengar*` 在对齐之前会展开为 `*Trull* *Sengar*`，这样两个名字都带着标记进入源文拼写。
- **用点连起来的词链**——像 `YOU...*WALKED*...HOME` 这样的分块（LLM 用 `...` 而不是空白当词分隔符）会被正确拆分，让每个 `*word*` 都能在合并中存活。
- **被 ASR 揉成一团的名字**——单个分块词 `*Tralesengar*` 覆盖两词语的源文短语 `Trull Sengar` 时，会通过拼接模糊匹配把强调广播到两个源文词上。

当 ASR 误转写严重到逐词模糊比和拼接模糊比都过不了阈值时，强调仍然可能丢失——这些情况会出现在复核里，你可以用 `[e]dit` 修。无论哪种情况，词与词之间的停顿都会保留。

会话结束时，如果你接受了很多原文却没有合并，你会看到一条警告，建议下次用 `[m]erge`。

## 暂停与续跑

在任何复核提示处按 `q` 即可停止。存档点文件会自动保存在你的 JSONL 旁边：

```
.metadata_compare_progress.json
```

重跑完全相同的命令，就能从上次停下的地方继续：

```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source original_book.epub
```

要彻底从头开始（丢弃之前所有决定）：
```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source original_book.epub \
  --reset
```

## 输出

修正后的 JSONL 默认写到 `metadata_corrected.jsonl`（就在输入 JSONL 旁边）。你选了 `[k]` 保留的条目和被自动通过的条目保持不变。只有 `[a]` 接受和 `[e]` 编辑的条目文本不同。

你可以指定别的输出路径：
```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source book.epub \
  --output my_reviewed_dataset.jsonl
```

## 阈值调整

默认阈值 `0.90` 意味着与原文有 ≥ 90% 的词匹配的条目会被静默通过。想看到更多条目就把它调低：

```bash
# See everything with less than 95% match
python alexandria_compare.py --jsonl ... --source ... --threshold 0.95

# See everything, no auto-approve
python alexandria_compare.py --jsonl ... --source ... --review-all
```

## 对齐是怎么工作的

工具在源文的词表里维护一个**游标**。对每个 JSONL 条目：
1. 从转写里剥掉 TTS 标注标记（`...`、`*word*`）
2. 把转写和源文词都归一化（小写、无标点）
3. 在游标附近 ±200 个词的范围内滑动窗口，用模糊序列匹配给每个位置打分
4. 把游标推进到最佳匹配的末尾

这意味着**对齐的准确性取决于转写是顺序正确的**，而它一直是顺序正确的，因为它本来就是按音频顺序产出的。如果个别分块的 ASR 错误非常严重，对齐可能轻微漂移——你会从周围条目异常低的匹配分数上察觉。

## 完成时的汇总

```
Complete!
  Auto-approved (≥90%) : 4512
  Kept annotation      : 83
  Accepted original    : 78
  Edited manually      : 12
  Skipped              : 28
  Re-run to review 28 skipped entries.
```
