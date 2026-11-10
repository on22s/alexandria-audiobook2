# 脚本生成

[中文主页](Home-CN.md) · [English](Script-Generation.md)

语言模型把书的文本变成脚本：一个行列表，每行都有说话人和给声音的演绎指令。

## 流水线

1. 在 Script 页**上传**一本书（`.txt`、`.md` 或 `.epub`）。EPUB 文件在上传时转换为纯文本。
2. **Generate Annotated Script** 会对文本跑三遍：
   1. **Split**——把文本分成旁白和对话行。Setup → **Step 1: dialogue detection** 决定怎么做：
      - **Auto**（推荐）——当引号能清楚切分一个片段并通过检查时就用引号；否则问模型。所有已发布的结果都用这一项。
      - **Quote marks only**——从不询问模型。
      - **Quote-aware**——类似 Quote marks，但旁白里被引用的词仍算旁白。
      - **Model only**——总是询问模型。
   2. **Attribute**——根据周围文本，把每条对话行分配给书里角色表中的说话人。
   3. **Instruct**——为每一行写演绎指令。
3. **Review**（可选）——第二次模型处理，修正常见错误（见下文）。
4. **Voices**——脚本里的每个说话人在 Voices 页都有一张声音卡。

每一遍都有存档点。运行可以暂停、在崩溃或断电后继续、把已完成部分保存为快照，或在不动设置的情况下重新开始。Generate 按钮下方的活动行会显示当前步骤、单元、第几次尝试和剩余时间。

生成之前，**Review character identities and source quotes** 让你确认这本书角色表中稳定的角色身份、别名和来源证据。它支持显式的导入核对、合并/拆分决定和精确撤销。工作流、来源坐标和恢复行为见[角色表指南](../character-roster.md)。

## 上传完成与恢复

只有文本完全写入后，上传才成为可复用的。EPUB 导入还会先写完其来源信息伴随文件，然后才让文本可用。未完成的文件保持私有，并计入上传存储上限。已有上传会按内容复用；名称冲突时保留在原名称下。

完成后的来源一旦可见，取消它的上传请求或没能把它选为当前书，都不会让该来源消失。另一个请求可能已经选中了它。对复用来源已完成的来源信息更新也会保留；写入前失败则保留之前的来源信息。错误响应并不意味着来源被删除。再次上传前先检查可复用的上传。选择校验失败会让原来的书保持不变。保存选择之后，迟到的磁盘错误或取消仍可能到达；重试前先刷新并检查当前书。

发布前进程崩溃可能留下一个私有的 `.source-upload-*.part` 文件和一个未配对的 `*.publication.json` 伴随文件。它们仍计入配额，不会作为可复用来源提供，也不会自动删除。重试会跳过孤立的伴随文件，而不会把它挂到无关文本上。要恢复，先停止 AA2，保留这些文件的副本以便检查，然后重新上传原始来源。不要把私有中间文件改名放进可复用库：它可能只包含上传的一部分。只有在确认没有正在运行或已保存的工作依赖它们、并保留恢复所需内容之后，才删除残留文件。

发布需要一次拒绝已存在目标的原子移动。实现使用 Windows rename、Linux `renameat2(RENAME_NOREPLACE)` 或 macOS `renamex_np(RENAME_EXCL)`。操作不可用或文件系统不支持时会失败，而不会退回到覆盖或部分可见的副本。原生 CI 会在 Linux、Windows 和 macOS 上检验完整发布、冲突和进程终止边界；这不是对断电或所有文件系统的保证。

## 脚本格式

脚本是一个 JSON 数组：

```json
[
  {"speaker": "NARRATOR", "text": "The door creaked open slowly.", "instruct": "Calm, even narration."},
  {"speaker": "ELENA", "text": "Ah! Who's there?", "instruct": "Startled and fearful, sharp whispered question, voice cracking with panic."},
  {"speaker": "MARCUS", "text": "Haha... did you miss me?", "instruct": "Menacing confidence, low smug drawl with a dark chuckle, savoring the moment."}
]
```

- **`speaker`**——`NARRATOR`，或角色表中大写形式的角色名。
- **`text`**——实际说出的内容。归属标签（"she said"）属于旁白，引号会去掉：说话这个事实由 speaker 字段承载。
- **`instruct`**——一到两句话的简短声音指示（约 8–15 个词），描述情绪、演绎和音质。

## 写出好的指令

`instruct` 字段会作为声音指示发送给语音引擎。最多可以叠加三层：

1. **情绪基调**——角色的感受：愤怒、恐惧、得意、空洞、绝望、被逗乐。
2. **演绎方式**——说话的方式：低语、拖长的低音、简短生硬、从容、逐渐升高。
3. **音质**——声音的状态：破音、沙哑、紧绷、生涩、气息感、勉强。

不是每一行都需要三层。无关紧要的行可以很简单（"Casual, offhand remark."）；把丰富、多层的指令留给情绪高点。

**好例子**

- `"Cold fury, barely contained, voice tight."`
- `"Bright eager excitement, words tumbling out."`
- `"Menacing confidence, low smug drawl with a dark chuckle."`
- `"Devastated, voice cracking, struggling to hold composure."`
- `"Sharp whispered warning, urgent and hushed."`

**旁白**默认是 `"Neutral, even narration."`。场景中情绪变化时，允许加一个修饰——`"Tense, clipped narration."`、`"Quiet, somber narration."`、`"Wry, light narration."`——同一场景内旁白的各行保持一致的语气。

**避免**

- 身体动作（"trembling"、"leaning forward"）——描述声音，而不是身体。
- 两个意思相同的词（"sneering contempt, dripping with disdain"）。
- 弱化限定词（"slightly"、"a bit"）——它们会稀释指示。
- 单纯的速度词（"fast"、"slow"）——改用能量来体现节奏（"urgent intensity"、"heavy, weighted delivery"）。
- 描述声音本身的词（"deep"、"raspy"）。它们会和选定的声音打架；声音的身份锚点会去掉它们。

**有效的音色描述词：**voice cracking、hollow、seething、flat、low、cold、numb、drained、gravelly、tight、raw、breathy、hushed、booming、clipped、strained。[声音参考](Voice-Reference-CN.md)有长得多的词表。

### 非语言声音

声音写成真实、可读的文本——绝不用方括号标签或特殊 token：

- 吸气声："Ah!"、"Oh!"，配一个类似 "Fearful, sharp gasp." 的指令；
- 叹气："Haah..."、"Hff..."；
- 笑声："Haha!"、"Ahaha..."；
- 哭泣："Hic... sniff..."；
- 感叹："Mmm..."、"Hmm..."、"Ugh..."。

任何不可读的内容——裸标签、图形假名——都不会到达语音引擎。

## 语言模型设置

在 Setup 页设置：

- **Base URL**、**API Key**（本地服务器填 `local`，或写 `env:NAME` 从环境变量读取）和 **Model Name**。
- **Reasoning effort**——推理模型用 *low*。不要禁用 `<think>`：在每一个会推理的模型上，reasoning low 测得都比关闭推理更好。

### 采样默认值

| 设置 | 默认值 | 说明 |
|---|---|---|
| Temperature | 0.6 | 通用设置 |
| Step 1 / 2 / 3 temperatures | 各 0.1 | 三遍各用自己的温度 |
| Top P | 0.8 | |
| Top K | 0（关闭） | |
| Min P | 0 | |
| Presence penalty | 0 | |
| Banned tokens | 空 | 逗号分隔；不要把 `<think>` 放进去 |

已发布的准确率数字是在 temperature 0 下测的，此时重跑在每一行上都会给出相同答案。

### 文本如何切分

- **Step 1** 每个请求发送约 3,000 字符（Setup → **Step 1: text per request**）。
- **Step 2** 每个请求发送 25 行，外加它们周围约 2,000 字符的文本。这块周围文本正是 `michel2_full` 提示词加上的内容，也是测到的最大单项提升。把它设为 0 就能复现 2026-09-19 之前的分数。
- 角色表会在请求之间携带，以保证名字一致。
- 如果请求失败，**context rescue** 会用更宽的窗口重试（2,000、4,000 和 6,000 字符，各重试两次）。

## 审阅脚本

生成之后，**Review Script** 会跑第二次模型处理，修正：

1. **对话里的归属标签**——去掉 "said he"、"she replied" 之类。
2. **被分给角色的旁白**——拆出来作为 NARRATOR。
3. **藏在旁白里的对话**——提取为该角色的行。
4. **切得过碎的旁白**——合并同一场景里连续的短旁白行。
5. **无效的指令**——把身体动作换成对应的声音表达，同时保留有效的丰富指令。

**Contextual Review** 用带重叠的窗口处理脚本（默认每侧 4 行），这样模型能看到周围的文本。它在说话人和语气一致性上更准确。

**Labels-only review**（需手动开启：`prompts.review_labels_only: true`，或 `review_script.py --labels-only`）只询问每行的说话人和演绎说明，绝不改写文本，所以改动一个逗号或移动一个说话标签，都不会再让一个批次失去它的说话人修正。它不能拆分或合并行；普通审阅仍然可以（[详情](../features/labels-only-review.md)）。

**Batch review** 可以一次审阅一整个系列已保存的脚本，可选先发现昵称，并跑两遍（正向，再反向）。

相关工具：**Find Nicknames** 发现角色的其他称呼，**Edit aliases** 让你修正它们，于是 "Betty" 和 "BEATRICE" 共用一个声音。

## 提示词

每一遍读取 `app/` 下自己的提示词文件：

- `default_prompts_segment.txt`（第 1 步）、`default_prompts_attribute.txt`（第 2 步）、`default_prompts_instruct.txt`（第 3 步）；
- 审阅遍用 `review_prompts.txt`。

每个文件包含一条系统消息、一行 `---SEPARATOR---`，然后是一条带 `{roster}` 和 `{batch}` 占位符的用户消息。

要修改它们：

- **在 Setup 里**——选择归属提示词变体，编辑文本，然后 **Save as preset**。预设重启后仍在。默认是 `michel2_full`。`michel2_spoken` 只询问对话行，在 Qwen3-14B 不加适配器时（本地三本书金标集）得 70.1%，对比 63.6%，而第 2 步耗时约少 3 倍（[详情](../features/spoken-only-attribution.md)）。其他模型还没有测量；保持使用归属适配器训练时的提示词。把鼠标悬停在任一设置或其标签上（或键盘聚焦）半秒，可以看到说明，以及在测过的地方给出的推荐。
- **What the model will see** 显示第 2 步将会发送的确切消息。
- **Reset to Defaults** 无需重启即可重新读取文件。

改提示词就改变了测量：已发布的分数属于它的提示词变体。见[提示词与适配器](Prompts-and-Adapters-CN.md)。

### 其他语言的书

默认提示词是为英语写的。其他语言请按该语言的惯例改写——法语引号（« »）、日语括号（「」）、通常的说话归属方式——并把 TTS **Language** 设为对应语言。

## 用哪个模型

按显存给出的实测推荐（带分数）见[适合你显卡的模型](Which-Model-For-Your-Card-CN.md)和 README 的[推荐的 LLM 模型](../../README.md#recommended-llm-models)。简而言之：

- **Qwen3.8-27B** 的 UD-Q3_K_XL 是能放进 16 GB 显卡的最准本地模型。
- **Qwen3.6-35B-A3B** 是快的选择：速度约与 8B 模型相当，准确率高约 11 个点。
- **DeepSeek v4-pro**（托管）是测过的最准模型。

## 已保存脚本

脚本在 Script 页的 **Saved Scripts** 区块保存和加载：

- **Save** 把脚本及其声音设置保存在 `scripts/`。
- **Load** 恢复它们，于是你可以继续编辑或重新生成特定的行。
- **Repair** 预览修正已保存脚本中的说话人或内容，并保留原件的备份。
