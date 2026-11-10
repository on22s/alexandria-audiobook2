# Dataset Builder

[中文主页](Home-CN.md) · [English](Dataset-Builder.md)

Dataset 页一次构建一个样本的 LoRA 训练数据集，让你在定稿前试听每个样本。

## 什么时候用

- 你想反复调整声音描述和情绪，直到每个样本都听起来正确。
- 你想分多次会话构建数据集。
- 你想听不同的情绪与文本组合如何改变声音。

Training 页的 **Build New Dataset** 按钮打开的也是它。

## 流程

### 1. 创建项目

点击 **New Dataset** 并输入名称。这会在 `dataset_builder/` 下创建一个工作区，跨会话保留。

### 2. 描述声音

- **Root Voice Description**——声音的身份，例如 "A warm, deep male narrator with a calm baritone"。哪些词有效，见[声音参考](Voice-Reference-CN.md)。
- **Global Seed**——可选，用于可复现的生成；留空则随机。

### 3. 添加样本

点击 **Add Row** 添加每个样本。每行包含：

| 字段 | 含义 |
|---|---|
| **Text** | 这个样本读出的内容 |
| **Emotion** | 加到声音描述上的风格指示（"cheerful"、"tense whisper"、"barking orders"） |

Emotion 只是用 VoiceDesign **生成**音频时的提示，不会存成训练标签。（如果你准备的是真实录音，见[训练指南](Training-Guide-CN.md#方式-2-上传真实录音-zip)。）

提示：

- 覆盖多种情绪：平静、高兴、愤怒、悲伤、紧张、命令。
- 把短感叹（"Oh!"、"Right."）和较长的段落混在一起。
- 至少包含一段较长的平静段落，用作参考样本。
- 关于什么算好数据集，[训练指南](Training-Guide-CN.md)有更多说明。

### 4. 生成并试听

- 某行的 **Generate**——只渲染该样本。
- **Generate Pending**——渲染所有还没有音频的样本。
- **Regen All**——重新渲染所有样本。
- **Cancel**——停止正在运行的批次；已完成的样本会保留。

试听每个样本，重新生成听起来不对的样本，并按需调整情绪或声音描述。

### 5. 保存为数据集

当每个样本都听起来正确时：

1. 选择**参考样本**。它会成为 `ref.wav`，即训练时使用的说话人嵌入——挑一句清晰、有代表性的行，通常是较长的平静段落。
2. 点击 **Save as Training Dataset**。项目会被复制到 `lora_datasets/`，并出现在 Training 页。

你也可以用 **Import / Export JSON** 在机器之间迁移项目的行。

## 项目保存在哪里

- 每个项目位于服务器上的 `dataset_builder/{name}/`。
- 行、描述和 seed 保存在项目的 `state.json` 里，所以你可以关掉浏览器，之后再回来。
- 保存数据集会**复制**数据；项目仍然保留，你可以继续在里面工作。

## API

[API 参考](API-Reference-CN.md#dataset-builder)说明了如何通过 HTTP 操作 Dataset Builder。
