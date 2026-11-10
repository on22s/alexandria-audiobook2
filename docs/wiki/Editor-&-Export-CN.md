# 编辑与导出

[中文主页](Home-CN.md) · [English](Editor-&-Export.md)

Editor 页逐行（"分块"）显示你的有声书，并提供编辑、重新渲染、试听和检查每个分块的工具。Result 页导出完成的书。

## 分块编辑器

每个分块显示：

- **Status**——Pending（还没有音频）、Done 或 Error；
- **Speaker**——可编辑；
- **Text**——说出的行或旁白，可编辑；
- **Instruct**——演绎指令，可编辑。

每个分块还可以播放、单独重新渲染、插入或删除。

### 编辑一行

1. 点击字段并编辑；改动会自动保存。
2. 点击分块的 **generate** 按钮只重新渲染这一行——适合在编辑后修正某行、试不同的指令，或比较声音。
3. 或者点击 **Render Pending** 渲染所有还缺音频的分块。

## 发音

### 修正一处发音

打开 **Pronunciation**，加载共享规则，然后 **Preview counts and line voices**。选择一行并 **Load line rules**。输入原始拼写和朗读替换，然后 **Find original spelling**，把想要匹配的位置加为草稿。匹配针对的是为朗读准备好的文本，即经过提示音和符号清理之后的文本。脚本本身保持不变。

要进行有界的英语词表扫描，在加载行规则后选择 **Discover English homographs**。它使用现有的精确拼写词边界来查找 lead/read/wind，而不是穷尽的语言分析、含义推断或散文语言检测。解析出的 TTS 语言必须是英语。在某处旁边输入朗读替换，并选择一个含义/示例，即可创建一个未批准的草稿，并带一条用户选择的含义备注。重复点击不会给已经加入草稿或已批准的位置再加一份草稿。审阅并明确批准；发现功能不会生成音频、保存规则或改动文本/take。结果被截断时会省略后面的匹配。

检查草稿，选择 **Approved**，在 **Save line rules** 之前先 **Preview line**。草稿和被拒绝的规则不影响朗读。已批准的某处规则在该处优先于共享规则；插入的替换不会被二次替换。可选的限制条件可以选择脚本作者指定的说话人、确切分配的声音或合声组、语言或引擎模式。音标提示对纯文本引擎需要有明确的朗读回退；否则预览会报告它被绕过。

保存行规则会等待正在运行的工作完成或被取消。只有实际朗读发生变化才会让该行变为待处理，其之前的音频文件会保留。备注和未启用的草稿保留现有音频。改变语言或引擎模式也会让受影响的条件行变为待处理。预览不会加载 TTS 模型；上面的试听会用所选的行声音生成一次性音频。试听和渲染出的行都要听，来判断朗读效果。

规则属于当前 Editor 的行身份。调整行的顺序会保留其规则；编辑行的文本则需要重新检查新的匹配。加载或重建已保存脚本会创建新的行，不会转移某处规则。重叠的已批准位置和过期的匹配会被拒绝。一次生成的 take 会把已应用和已绕过的决定连同音频修订记录下来，所以之后添加的审阅备注无法给更早的表演重新贴标签。

## 渲染音频

- **Render Pending** 渲染所有还没有音频的分块。
- **Regenerate All** 重新渲染每个分块。
- 使用内置 TTS（`local` 模式）时，两者都用批量渲染，约为实时的 3–6 倍。使用外部 TTS 服务器时，它们会逐行并行发送请求。见[批量生成](Batch-Generation-CN.md)。
- 两者都遵守 GPU 锁：脚本生成正在使用显卡时它们不会启动，除非语言模型被标记为不在本机 GPU 上。
- **Cancel** 停止渲染；已完成的分块会保留。

## 检查音频

- **Check Voices** 把每个渲染出的分块与说话人的参考声音比对（ECAPA 相似度），标记发生漂移的分块。勾选 **flagged only** 只看这些。
- **Text integrity** 逐词显示脚本与来源文本的差异。
- 每个生成的文件都会被校验是否为真实音频；损坏的文件会在其分块上被标记。

## 试听

- 用播放按钮 **Play** 单个分块。
- **Play Sequence** 按顺序播放分块，带自然停顿：
  - 不同说话人之间——默认 500 ms（**Speaker Change Pause**）；
  - 同一说话人的行之间——默认 250 ms（**Same Speaker Pause**）。

  两者都在 Setup 页设置。

## 已保存的 take 快照

检查某个已渲染的行并打开 **Saved take snapshots**。在修改它的文字、声音或 seed，或重新生成它之前，选择 **Save this take**。这会保存所选音频的不可变副本，以及记录下来的文本、说话人、演绎、seed 计数器和可用证据。用 **This line's snapshots** 试听副本，或用 **All project snapshots** 查找更早的行和书籍生成中的副本。播放会校验已保存的字节，不会改变所选行的音频。

**Protect** 阻止删除。**Delete with undo** 让快照从播放中隐藏，但保留字节；**Undo deletion** 恢复它。**Remove permanently…** 是另一个需要确认的操作，会释放存储且无法撤销。当前使用中、被引用和受保护的快照文件无法删除。不会自动清理。

项目归档允许每个书/行身份 16 条记录、共 4,096 条记录和 1 GiB 负载。每个副本限制为 64 MiB、单声道/立体声和十分钟。元数据限制为每条记录 128 KiB、每个清单 16 MiB，另有一次上限 16 MiB 的待处理元数据写入。已删除和中断的副本会计入，直到显式删除。失败或中断的副本绝不会替换所选音频；其计入的未完成记录会保留可见以便清理。未知或损坏的归档元数据会保留以便检查，而不是被静默修复或清理。

**Automatic take history** 默认关闭。在 Editor 里打开该区块，选择 **Refresh setting**，然后 **Turn on**，以便在重新生成前保留之前选中的表演。归档限制仍然生效：如果之前的 take 无法安全保留，重新生成会在替换它之前被拒绝。**Turn off** 停止新的自动快照；已有快照保留，待处理的之前 take 在被成功替换之前保持受保护。

在支持的宿主机上，当前书和当前行的就绪快照如果带有恢复证据，会显示 **Check & restore**。该按钮会启动资格检查；它并不保证快照可以恢复。恢复会把已保存的音频及其原始生成依赖与当前行做校验，保留被替换的 take，并在不生成新语音的情况下选中已保存的表演。如果被替换的 take 无法安全保留，恢复会被拒绝。它不会为了迁就不兼容的快照而改动脚本。经过校验的恢复目前可用于受支持 Linux 宿主机上的内置本地 CustomVoice、VoiceClone 和 LoRA。已保存的表演需要渲染时开启合成窗口和恢复所产生的原生发布证据。当前的 clone 参考字节和 LoRA 适配器代次必须与记录的执行依赖一致。没有这些证据的旧快照仍可试听。

就绪、未删除的旧录音，或没有匹配恢复证据的快照，仍可试听。文本、声音、模型设置或其他已记录依赖的改动都可能让快照失去恢复资格。加载已保存脚本会创建新的行身份；旧快照仍留在 **All project snapshots** 里，绝不会按位置或文本分配给新的行。

## 合并

点击 **Merge All**，把所有有有效音频的分块按顺序合并成一整本有声书，并使用上面的停顿。拼接分块时，每行开头和结尾的静音（空白音——LoRA 声音上中位为 310–340 ms）会被裁掉，所以你设置的停顿就是你听到的停顿。结果显示在 Result 页。

## 导出选项

### 合并后的有声书

- 一个 128 kbps 的 MP3，包含所有声音和自然停顿。
- 在 **Result** 页下载；文件是 `cloned_audiobook.mp3`。

### 带章节的 M4B

**Export M4B** 生成带章节标记的有声书文件。你可以设置 **Title**、**Author**、**Narrator**、**Year**、**Description** 和 **Cover Image**，并选择 **Per-chunk chapters**。

### 导出完整性检查

Result 页在替换上一次交付之前，会检查完成的 MP3、M4B 和分章音频。这些检查把编码后的音频与独立的来源帧计划比对，解码以查找损坏或非有限的样本，并检查 M4B 章节标记和写入的书元数据。失败或被取消的导出会保留上一次的交付。

在 **Export integrity checks** 下，为下一次导出设置静音阈值和最大意外间隙。长静音默认产生警告；开启 **Reject exports with unexpected long silence** 则改为拒绝。脚本里设定的行间停顿不计入。这些检查不证明朗读内容或发音正确。

**Show checks for this book** 分别显示完成的检查和被拒绝的尝试，包括帧数以及静音或章节的时间范围。结果在本次应用会话内保留，每种导出类型最多 32 次交付和 16 次被拒绝的尝试。文件元数据变化时，该视图会省略回执；打开它不会重新计算哈希或解码音频。它的 SHA-256 值描述的是完成检查时的字节。导出发布本身会检查完整文件字节和来源身份。

Audacity 检查会核对 ZIP 成员、WAV 头、确切的轨道帧、LOF/标签和归档 CRC。它的非活动说话人间隙是有意的，不会被归类为缺失旁白。

### 分章文件

**Export chapters** 把每一章写成自己的 MP3 或 WAV 文件，按 `{chapter_number} - {chapter_name}` 这样的模板命名。选项包括编号位数、书名和系列名、卷、章节列表、逐分块文件、仅变化的章节，以及文件名预览。这些文件及其 zip 位于 `chapter_exports/`。

### 单独的行

每一行也会作为单独文件保存在 `voicelines/`，按阅读顺序编号并按说话人命名，可直接导入音频软件：

```
voicelines/
├── voiceline_0001_narrator.mp3
├── voiceline_0002_elena.mp3
├── voiceline_0003_marcus.mp3
└── ...
```

### Audacity

**Export to Audacity**（Result 页）为每个说话人生成一条 WAV 轨道，用于多轨编辑：

```
audacity_export.zip
├── project.lof       # open in Audacity to import every track at once
├── labels.txt        # import via File > Import > Labels for line annotations
├── narrator.wav      # full-length track with only the NARRATOR's audio
├── elena.wav         # full-length track with only ELENA's audio
├── marcus.wav        # full-length track with only MARCUS's audio
└── ...
```

使用方法：

1. 下载并解压 `audacity_export.zip`。
2. 在 Audacity 里打开 `project.lof`；每个说话人的轨道都会导入。
3. 通过 **File > Import > Labels** 导入 `labels.txt`，获得行标注。

每条轨道都会用静音补齐到相同长度，所以一起播放时听起来和合并后的 MP3 完全一样。这适合做每角色的音量、对单个说话人加效果、调整行间的时间，以及专业后期制作。

## 损坏或很小的 MP3 文件

428 字节的 MP3 意味着 ffmpeg 缺失或无法编码 MP3。安装器会把 ffmpeg 构建进应用的环境，所以这通常意味着应用没有在 `app/env` 里运行。从 Pinokio 启动它（或激活 `app/env`），然后重新渲染。见[故障排查](Troubleshooting-CN.md)。
