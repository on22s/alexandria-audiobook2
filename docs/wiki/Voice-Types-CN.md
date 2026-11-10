# 声音类型

[中文主页](Home-CN.md) · [English](Voice-Types.md)

脚本里的每个说话人在 Voices 页都有一张声音卡。本页说明可以选择的声音类型，以及各自适合什么时候用。

## 角色状态的人设

高级全局人设生成会为角色每一个确定的年龄/性别状态创建单独的人设和预览。每个状态使用自己的对话和附近的旁白，并保留原脚本的证据索引。故事后部重复出现的状态有各自编号的卡片。

在 Voices 里，用 **Show cards** 显示所有角色、有状态卡的角色，或没有状态卡的角色。状态卡有各自独立的声音类型、参考音频/文字稿、人设描述、风格、seed、批准状态和候选池。编辑某个状态的声线前，先生成缺失或过期的状态人设。状态人设只会在它自己那部分书发生变化时过期（该角色的行，或该状态所在段落的旁白）；书里其他位置的修改不会让它过期。角色的基础声音和手动版本仍保留在角色可展开的基础声音区块里。

高级模式下，**Only characters without a voice yet** 也会生成缺失或过期的状态人设。在某张状态卡上 Regenerate persona 只针对该状态。失败按状态分别报告；成功完成的状态会保存，失败状态之前的设置会保留。

生成和编辑不会把声音变化应用到已渲染的音频。打开角色基础区块里的 **Voice changes** 控件，检查建议的状态版本，然后按 **Apply**。之后的渲染会在各自的时间线边界使用这些版本。合并分块内部的改动、编辑后不再对齐的对话，或过期的脚本，都需要在应用前重建/审阅分块；这样的状态会被跳过并给出警告，角色的其他状态仍然生效。状态声音只通过这条时间线应用，绝不会被选为角色的整本书版本。应用、编辑或清除声音时间线时，只有实际声音发生变化的行会被标记为待处理。用 Editor 里的 **Render Pending** 替换它们。之前的音频文件仍留在磁盘上，但在新的渲染成功之前，待处理的行会从导出中分离。

已知的性别变化，如果年龄没有大幅跳跃，会被标记为 **Possible identity reveal** 供审阅。标签保持脚本里的写法。如果角色应当始终听起来一样，设置其基础声音，选择 **Main voice throughout**，然后 **Apply**；只选择不会保存任何东西。如果变化是有意的，就保留各自独立的状态声音。

带有已知年龄标签、可播放参考音频和参考文字稿的已保存 Designer 声音，也会连同 LoRA 声音库里的声音一起出现在 **Voice changes** 中。它们以克隆参考的方式应用。年龄/性别标签用于引导建议；确切声明为儿童档位的排在最前，其他角色已使用的声音会标为复用。缺失或损坏的参考会被排除。要把其中一个用作角色的基础声音，在 **Voice Clone** 下选择它已保存的 Designer 参考。

用 **Remove saved state persona** 删除一个未应用的状态版本（当前的或过期的），同时保留它的音频文件和其他声音。如果播放仍在用它，先清除它已应用的时间线点。把角色表保存到声音库会保留手动版本，但绝不会把状态版本或声音时间线带到另一本书。

在状态卡上用 **Recover persona** 打开手动恢复，并选中那个确切状态。你自己在恢复面板里输入角色时，要显式选择它的基础人设或带编号的状态。状态恢复和 Resume 会保留基础状态及其他状态；复制的提示词只能使用所选状态里的对话/上下文。


## 语音模型

应用使用三个 Qwen3-TTS 模型：

| 模型 | ID | 用途 |
|---|---|---|
| **CustomVoice** | `Qwen3-TTS-12Hz-1.7B` | 9 个内置声音，遵循演绎指令 |
| **Base** | `Qwen3-TTS-12Hz-1.7B-Base` | 从参考音频克隆声音，以及 LoRA 声音 |
| **VoiceDesign** | `Qwen3-TTS-12Hz-1.7B-VoiceDesign` | 从文字描述创建声音 |

模型在首次需要时加载，之后保持缓存。只会把当前声音类型所需的模型加载进显存。

## 声音类型一览

| Voices 页里的类型 | 模型 | 最适合 | 是否批量 |
|---|---|---|---|
| **Custom Voice** | CustomVoice | 旁白和主要角色 | 是 |
| **Built-in Voice** | Base + 随附的 LoRA | 现成的训练声音，无需自己训练 | 是 |
| **Voice Clone** | Base | 需要一个特定声音的角色 | 是 |
| **LoRA Voice** | Base + 你的 LoRA | 需要独特、持久声音的常驻角色 | 是 |
| **Voice Design** | VoiceDesign | 只有 1–3 行的次要角色 | 否——逐行 |
| **Together** | 各成员自己的声音 | 一组人同时说话 | — |

## 选择类型

```
Is this a main character with many lines?
├─ Yes → Do you need a voice other than the 9 built-in ones?
│        ├─ Yes → LoRA Voice (train one) or Built-in Voice (use a shipped one)
│        └─ No  → Custom Voice
└─ No  → Does the character have more than 3–4 lines?
         ├─ Yes → Voice Clone (save a designed voice and use it as the reference)
         └─ No  → Voice Design (no setup needed)
```

## 身份锚点与风格时间线

每张声音卡都有 **Character Style** 字段。它是声音的**身份锚点**：一段固定的描述（例如 "Heavy Scottish accent" 或 "Refined aristocratic tone"），随每一行一起发送，所以逐行的演绎指令无法改变角色听起来是谁。

- **Custom** 声音的锚点放在**最前**，然后是这一行自己的指令：`"Heavy Scottish accent Excited, bright energy."`。在 120 条真实行上测量，这把旁白的音高漂移从 3.49 半音降到 2.53 半音，并让声音更接近它自己的开头（ECAPA 0.737 → 0.771）。
- **LoRA** 和 **Built-in** 声音的角色风格加在行的指令**之后**：`"Excited, bright energy. Heavy Scottish accent"`。

**风格时间线**让角色从某一行起发生变化——"时间跳跃之后变老"——而不会变成另一个说话人。每个变化点从那一行起设置新的锚点。

## Generate Personas（自动分配声音）

与其手动设置每个说话人，不如点击 **Generate Personas**：

1. 语言模型阅读每个说话人的样本行。
2. 它写出一段声音描述（"A gruff middle-aged man with a deep, gravelly voice"）和一句示例句子。
3. VoiceDesign 模型根据每段描述渲染参考音频。
4. 每个说话人被分配那段音频作为 **Voice Clone**。

选项：

- **Scope**——默认只处理*还没有声音*的角色；你可以选择所有角色。
- **Save the current voices to the library first**——保留你已有的声音。
- **Advanced**——设置批大小（默认 40），用于角色表非常大的脚本。

说明：

- 人设声音是克隆声音：它们保持一致的身份，但语气中性，因为克隆声音不遵循演绎指令。
- 想要更有表现力的声音，可以先用好人设，再把关键角色换成 LoRA 或 Custom。
- 生成的参考音频保存在 `designed_voices/`，可以复用。

**Suggest LoRA Voices** 对训练好的声音做类似的事：把你已有的 LoRA 声音匹配到角色。

## 别名

声音卡上的 **Alias of** 下拉让一个说话人使用另一个说话人的声音。

用于：

- 名字变体："DR. SMITH" → "SMITH"；
- 年龄变体："YOUNG ELENA" → "ELENA"；
- 头衔："KING MARCUS" → "MARCUS"；
- 减少需要设置的声音数量。

工作方式：

- 生成音频时，带别名的说话人使用目标说话人的声音。
- 别名可以串联：如果 A → B → C，说话人 A 使用 C 的声音。
- 循环（A → B → A）会被检测到，记为警告，并中止。
- 别名以说话人的 `alias_of` 字段保存在 `voice_config.json` 里。

## Custom Voice

**模型：**CustomVoice。**最适合：**旁白和主要角色。

从 9 个内置声音中选一个，通过该行的指令完全控制情绪和演绎。

- **声音：**Aiden、Dylan、Eric、Ono_anna、Ryan、Serena、Sohee、Uncle_fu、Vivian。
  - 大多数不是英语母语者；Ryan 和 Aiden 是。
- **设置：**声音，以及 **Character Style**（身份锚点）。
- **批量：**是——行会批量处理，并按长度拆成子批次。

## Built-in Voice

**模型：**Base 加随附的 LoRA 适配器。**最适合：**无需训练的现成声音。

仓库在 `builtin_lora/` 里随附了一组训练好的声音。首次使用时从 Hugging Face 下载，行为与 LoRA 声音相同。

## Voice Clone

**模型：**Base。**最适合：**需要一个特定声音的角色。

从 5–15 秒的参考录音克隆声音。

- **设置：**
  - **Designed Voice**——选择 Designer 页保存的声音（会自动填入音频路径）；或
  - **Custom Path**——任意 WAV 文件的路径；
  - **Reference Text**——参考音频的确切文字稿。
- **提示：**
  - 使用没有背景噪声的干净音频。
  - 短于约 7 秒的参考会得到不稳定的声音；导入检查会规范化并测量它们。
  - Designer 页可以从文字描述创建参考音频。
  - 上传的参考会记录其文字稿、来源和权利依据。
- 克隆声音**会忽略演绎指令**：声音完全来自参考。
- **批量：**是——行按说话人分组并一起批量处理。

当说话人标签或文件路径指向相同的音频和相同的参考文字稿时，本地克隆声音会复用参考准备结果。复用前会再次检查音频；参考字节、文字稿或常驻模型发生变化时，会重建受影响的工作。缓存存储有上限，被淘汰的参考会自动重新准备。卸载克隆模型会清空它的参考缓存。

最近的 TTS 诊断包含缓存命中、未命中、淘汰和计入的缓存字节。这些计数描述的是保留的缓存负载；它们不衡量进程或显存的总用量。

## LoRA Voice

**模型：**Base 加 LoRA 适配器。**最适合：**需要独特、持久且遵循演绎指令的声音的常驻角色。

LoRA 适配器在一小组声音样本上微调 Base 模型。结果比克隆更一致，而且会响应情绪和演绎指令。

- **设置：**适配器（来自 Training 页，或内置的），以及 **Character Style**。
- **工作方式：**
  1. 在你的声音样本上训练适配器（见[训练指南](Training-Guide-CN.md)）。
  2. 生成时把适配器加载到 Base 模型之上。
  3. 训练中的一段参考片段提供声音提示词。
  4. 锚点加上该行的指令控制演绎。
- **Clone 还是 LoRA？**
  - **Clone**——快，无需训练；声音来自一段参考片段。
  - **LoRA**——在很多行上更一致，并遵循情绪指示，但需要训练。
- **批量：**是——行按适配器分组；每个适配器的组各自批量处理。

## Voice Design

**模型：**VoiceDesign。**最适合：**只有 1–3 行的次要角色。

根据文字描述即时创建声音。**每一行都会得到一个全新生成的声音**，所以行与行之间没有持久的身份。

- **设置：****Description**——角色的基础声音（"Young strong soldier"、"Old gravelly bartender"）。
- **描述和该行指令如何组合：**
  - 描述：`"Young strong soldier"`；
  - 行指令：`"Shouting in rising desperation, with hints of panic"`；
  - 发送给模型：`"Young strong soldier, Shouting in rising desperation, with hints of panic"`。
- **提示：**
  - 描述只写声音本身（年龄、性别、音质），把情绪和演绎留给指令。
  - 避免自相矛盾，例如 "calm gentle voice" 配 "screaming furiously"——它们可能导致音频故障。
  - 角色超过 3–4 行时，保存设计好的声音并把它当作克隆使用。
- **批量：**否——每行的描述可能不同，所以这些行在所有批量类型之后逐行渲染。

## Together

用于一句话由几个角色同时说出。每个成员用自己已有的声音说这句话，片段会混合在一起。片段按最长的那段对齐，所以结果听起来像合唱，而不是完全齐声。

## 各类型的一致性如何？

| 类型 | 一致性 | 原因 |
|---|---|---|
| Custom | 高 | 每次都使用同一个内置声音（配合 seed） |
| Clone | 高 | 相同的参考音频得到相同的声音 |
| LoRA | 高 | 训练出的身份，跨行稳定 |
| Voice Design | 低到中 | 相同描述只能让声音相似，而非一致 |

对 Voice Design 来说，一致性取决于组合后的描述和指令在行与行之间有多相似。如果描述不变、只有情绪变化，声音听起来足够接近，不会让人觉得突兀。

Editor 的 **Check Voices** 直接测量这一点：它把每个渲染出的行与说话人的参考比对，标记发生漂移的行。
