<img width="475" height="467" alt="Alexandria Logo" src="https://github.com/user-attachments/assets/fa2c36d3-a5f3-49ab-9dfe-30933359dfbd" />

# Alexandria Audiobook2

[English](README.md) | 中文

[Alexandria](https://github.com/Finrandojin/alexandria-audiobook) 的研究分支：把一本书变成多角色配音的有声书，并且**对每一个选择都做测量**——哪个模型、哪种提示词能在标注好的金标数据上正确判断“这句话是谁说的”，合成的声音离真人朗读有多近，日语的音高重音在合成后是否还在。胜出的设置就是默认值——`michel2_full` 归属提示词在 2026-09-19 最后一个基座确认后成为产品默认，其他变体仍可在 Setup 页选择。数字、失败和配方都在仓库里，而不是在博客里。

**[GOALS.md](GOALS.md)** — 每个目标的定义和当前测量值 · **[RECIPES.md](RECIPES.md)** — 产生过有效结果的训练/推理设置，以及每一个“看起来像但其实失败”的对照 · **[RESULTS_INDEX.md](RESULTS_INDEX.md)** — 全部实验产物索引 · **[Hugging Face 上的适配器](https://huggingface.co/Om22s/alexandria-qwen3-attribution)** · **[HF_MODEL_GUIDE.md](HF_MODEL_GUIDE.md)** — 适配器的发布规范

## 示例音频：[sample.mp3](https://github.com/user-attachments/files/25276110/sample.mp3)

## 结果速览

下面的每个数字都在固定的测试集上、带成对对照测得，并注明测试集。它们是基准测试结果，不是对你那本书的保证。

### 谁在说话——说话人归属准确率

“四本书产品测试集”是四部日译轻小说中 768 条带说话人标注的对话行，通过应用自身的三遍流水线在 temperature 0、JSON schema 下打分。提示词的形态是本项目发现的最大单一杠杆；`michel2_full` 变体（在每个请求周围附上 2,000 字符的上下文，沿用 Michel 等人的表述）在所有测过的基座模型上都胜出。以下为基座模型、无适配器、reasoning low（RECIPES §"Prompt variants × bases"，2026-09-18）：

| 基座模型 | 文件 | 默认提示词 | `michel2_full` | 说明 |
|---|---:|---:|---:|---|
| DeepSeek v4-pro（API，关闭思考） | — | 91.1 | **94.9** | 思考 low、8k：**95.4**——云端上限，跑一遍测试集约 $0.50–0.75 |
| Qwen3.8-27B UD-Q4_K_M | 16.5 GB | 82.9 | 89.8 | `michel2` **90.9**——本地最好成绩 |
| Qwen3.6-35B-A3B UD-Q4_K_XL | 22.4 GB | — | **89.6** | IQ3_XXS（13.2 GB）/ IQ2_XXS（10.8 GB）在九部 PDNC 小说上为 91.6 / 90.5 |
| Muse-Glimmer-30B UD-Q3_K_XL | 13.4 GB | 81.5 | **90.5** | 产品自带的基座；`michel2` 86.6；关闭推理 72.1 |
| Qwen3-14B Q4_K_M | 9.0 GB | 66.1 | **82.0** | 仅靠提示词 +16，所有基座里增益最大 |
| Qwen3.5-9B / Qwen3-8B Q4_K_M | 5–6 GB | 62.6 / 60.8 | 71.9 / 71.7 | 在最难的一本书上都崩了 |

**本地 vs 云端**（目标 4.2，每本书最好的本地成绩对 DeepSeek 最好成绩）：97.3% / 97.5% / 94.5% / 92.9%。两本书落在目标要求的 5% 区间之外；下面的适配器就是为此做的。

### 适配器——只用版权干净的数据训练

用于说话人归属任务的 LoRA 适配器，训练数据只有 20 部公有领域 PDNC 小说、RiQuA 和 CC0 剧本改写的散文（没有任何轻小说文本，没有任何不能再分发的东西）。已公开在 Hub：[Om22s/alexandria-qwen3-attribution](https://huggingface.co/Om22s/alexandria-qwen3-attribution)。

| 适配器 | 测试集 | 基座 | 适配器 | 变化 |
|---|---|---:|---:|---:|
| Qwen3-14B rights-clean，seed 1 | 四本书，reasoning low，budget 1024 | 66.1 | 74.7 | **+8.6** |
| 同配方，seed 2 | 同上 | 66.1 | 74.9 | **+8.8** |
| seed 1 / seed 2 | 《爱玛》，未参与训练（318 行） | 68.9 | 75.8 / 69.5 | +6.9 / +0.6 |
| Qwen3.8-27B `michel2` 适配器 | 四本书，Q4_K_M，关闭推理 | 87.4 | 88.3 | +0.9（第一档；更低量化排队中） |
| Qwen3.6-35B-A3B `michel2` 适配器 | 量化阶梯 IQ1_M → Q4_K_XL | — | — | 排队中 |

四本书上的增益在两个随机种子间复现；未训练书目上的增益还没有（两个种子在《爱玛》上不一致）。适配器原本是为了提升基座模型——结果大部分提升来自提示词——所以它们现在的任务是**量化能做到多小**：对每个基座，找到 base + 适配器在 `michel2_full` 下仍留在 4.2 区间内的最小档位。模型卡上也如实写着：《太阳照常升起》是 20 部训练小说之一，所以 PDNC 结果只在真正未训练的八部上汇总。

### 声音——对照真人上限

| 目标 | 度量 | 结果 |
|---|---|---|
| 2.1 说话人相似度 | ECAPA 余弦相似度，生成音频 vs 真人朗读，以同一朗读者自身一致性为 100% | 英语 clone 93%（LJSpeech）/ 86%（第二位朗读者）；日语 98%；中文待 2.2 锚点修复后重测。目标 95%。 |
| 2.8 一本书里声音不变 | CustomVoice 旁白 120 行的半音漂移，对照开头几行的锚点 | 按原 instruct **3.49 半音**；加每角色身份锚点 **2.53 半音**（ECAPA 0.737 → 0.771）。整本书的运行排队中。 |
| 2.9 音高承载的意义 | 日语重音核后 H→L 下降的实现率，音拍经 CTC 对齐 | 真人朗读 72.6%，clone 74.4%，LoRA 71.2%——合成达到了接近真人的水平 |
| 空白音 | LoRA 声音每行开头的静音 | 中位 310–340 ms，占全书 4–5%；拼接时裁掉（−45 dBFS，保留 40/80 ms） |
| 第一遍对话检测 | 引号切分器在全部 28 部 PDNC 小说上 | 片段召回 99.84%，精确率 94.89%——带引号的英文切分不需要调用模型 |

已达成并有测试守住的目标（GOALS 第二部分）：选择缺口已闭合、每个生成文件都是真实音频、一个角色一个声音、输出可复现、快于实时、不可读内容不会进入 TTS、外来词按外来词朗读，以及测量完整性规则 6.1–6.4。

## 工作原理

<img src="docs/architecture.png" alt="Alexandria Audiobook2 架构图" width="100%" />

顶部一行是一本书经过的顺序：上传并修复来源 → 预检与规范化 → **第一遍：切分** → **第二遍：归属说话人** → **第三遍：添加演绎指令** → 质量门与恢复 → 审阅与身份稳定 → 分配角色与候选声音 → 批量 TTS 与校验 → 编辑、合并、导出。下方的方框是执行每一步的代码；可编辑的源文件是 [`docs/architecture.drawio.svg`](docs/architecture.drawio.svg)。

## 本分支相对上游的改动

按你会遇到的顺序：

- **三遍脚本生成**——切分、归属、指令——每一遍的请求文本量、每请求行数、周围上下文都在 Setup 页可调，按遍存档点，可续跑、可暂停。
- **可选择的归属提示词变体**（`default`、`michel`、`michel2`、`michel2_full`、`michel2_shot`），每一个都在同一金标上测过；你看到、编辑、保存为预设的就是实际发送的提示词文本。
- **推理模型是一等公民**：推理强度设置到达每一次 LLM 调用，请求带 JSON schema，服务器不报告推理 token 时从 trace 里估算，重试上限跟随配置的 token 预算。不再需要把 `<think>` 加进禁用 token。
- **LLM 配置档**：本地和远程两套，各有 URL、密钥（或 `env:NAME`）、模型、超时、重试/退避策略、自定义请求头和请求体；一套放弃时切换到另一套；“LLM 是否在本机 GPU 上”的开关，让托管模型标注时本机可以同时渲染音频；**手动传输**模式——你就是模型：应用把每个请求写进文件，等你粘贴回复。
- **第一遍对话检测**：自动 / 仅引号 / 仅模型。
- **Script 页遥测**：活动行显示运行正在等什么，重试按尝试次数显示，剩余时间由流水线自己估算；Start over；把已完成部分保存为快照；Windows 上 Pause 置灰而不是每次点击都报错。
- **Voices**：只为*还没有声音的角色*生成人设或推荐 LoRA 声音，并先把现有声音存进库；每角色**身份锚点**，演绎指令改不了音色，并支持**变化点**（“从这一行起这个角色变老了”）；系列角色表与声音库；昵称发现；克隆参考导入需登记权利（文字稿、来源、权利依据）。
- **Editor**：渲染音频偏离说话人声音的片段会被标记；拼接时裁掉空白音。
- **导出**：128 kbps MP3、带文件名模板的分章导出、Audacity 工程包、带章节的 M4B。
- **Voice Lab**：一本有声书进去，一个命名好的 LoRA 声音出来——Preparer（对齐 + 可选 LLM 富化）→ 去重 → 批量 LoRA 训练 → 画像 → 命名，每个阶段都在应用自己的环境里运行。
- **Reports 页**：运行历史、审阅存档点、基准测试（环境 · LLM · TTS · 训练）。
- **归属适配器**通过 `llama-server --lora` 提供，只用版权干净的数据训练，在 Hub 上发布，模型卡写明测了什么、没测什么。
- **测量纪律**：`GOALS.md`、`RECIPES.md`、只重建不合并的结果索引、每个产物带来源信息、发布校验器，以及 3,348 个单元测试。

## 截图

<img src="docs/screenshots/setup.png" width="49%" alt="Setup"></img> <img src="docs/screenshots/script.png" width="49%" alt="Script"></img>
<img src="docs/screenshots/voices.png" width="49%" alt="Voices"></img> <img src="docs/screenshots/editor.png" width="49%" alt="Editor"></img>

## 主要功能

### 脚本智能
- **任何 OpenAI 兼容的 LLM**——llama.cpp（所有数字都用它测得）、LM Studio、Ollama、DeepSeek/OpenAI 风格 API；两套保存的配置档可互相切换。
- **三遍标注**——第一遍把文本切成旁白和对话行（引号切分器或模型），第二遍根据已建立的角色表和周围上下文给每行分配说话人，第三遍为每行写演绎指令。每一遍有自己的温度和请求大小。
- **测过的提示词变体**——在 Setup 页选择归属提示词；下拉项名称与 RECIPES 一致，文本可编辑。
- **审阅遍**——第二次 LLM 处理，去掉对话里的归属标签、拆分错归的旁白、合并过度切分的旁白、校验 instruct 字段；带 ±N 上下文的审阅；跨系列批量审阅，可先发现昵称，可正反两遍。
- **身份稳定**——合并重复角色名、记住角色回应过的每个称呼的别名、第一人称叙述者处理、译者前言剥离、乱码来源修复。
- **恢复**——按遍存档点、断电后续跑、API 重试耗尽时暂停而不是让分块失败、已完成前缀快照、不动设置的重新开始。

### 语音生成
- **内置 Qwen3-TTS**——无需外部服务器；也支持外部 Gradio 服务器池。
- **四种声音类型**——CustomVoice（9 个预设，支持 instruct 控制）、Clone（5–15 秒参考音频，导入时登记权利）、Voice Design（文字描述）、LoRA（训练出的声音，内置预设）。
- **身份锚点 + 风格时间线**——每角色一段 instruct 改不掉的固定描述，并可在某一行起改变角色的声音风格。
- **人设生成**——LLM 描述每个角色，Voice Design 渲染参考音频，自动分配克隆声音；默认只处理新增角色。
- **批量渲染**——按长度分桶的子批次、显存感知调度、可选 `torch.compile` 编解码器，中端显卡 3–6 倍实时。
- **漂移检查**——渲染出的片段与说话人参考比对，偏离的会被标记。
- **十种语言**——英、中、法、德、意、日、韩、葡、俄、西，或自动检测。

### 编辑与导出
- **分块编辑器**，支持单块重新生成、仅看被标记的、顺序播放。
- 说话人之间与同一说话人内部的**自然停顿**；拼接时**裁掉空白音**。
- **合并 MP3（128 kbps）**、逐行音频、带文件名模板的**分章导出**、**Audacity** 多轨工程包、带章节的 **M4B**。

### Voice Lab 与工具
- **Preparer**——有声书 + EPUB/TXT → 对齐好的（音频，文本）样本对，可选 LLM 富化（说话人、叙述风格、情绪基调）。
- **Dataset Builder** 与 **LoRA Training** 页处理单个声音；整库的批量训练、去重、画像、命名。
- 适配器**盲评**——对比、晋升、带回执回滚。
- **Reports**——运行历史、审阅存档点、基准测试清单。

## 系统要求

- [Pinokio](https://pinokio.computer/)（或下面的 Docker / Colab）
- 一个应用能通过 OpenAI 兼容 API 访问的 **LLM 服务器**——测过什么见[推荐的 LLM 模型](#推荐的-llm-模型)：
  - [llama.cpp](https://github.com/ggml-org/llama.cpp) 的 `llama-server`——上面所有数字都用它产生；支持 `--lora` 加载归属适配器，`--reasoning-budget` 限制推理预算
  - [LM Studio](https://lmstudio.ai/) 或 [Ollama](https://ollama.ai/)
  - 托管 API（测过 DeepSeek v4-pro；任何 OpenAI 风格接口都可以）
- **GPU**（用于 TTS）：最低 8 GB 显存，推荐 16 GB。每个 TTS 模型约 3.4 GB，其余决定批大小。LLM 需要自己的显存——16 GB 的卡可以*要么*跑一个 9–13 GB 的 GGUF，*要么*渲染音频；应用的 GPU 锁会防止两者相撞，除非你告诉它 LLM 在别处。
- **内存**：推荐 16 GB。**磁盘**：环境和 TTS 权重约 20 GB，另加你的 LLM 文件和音频。

### GPU 兼容性

| GPU | 系统 | TTS | 说明 |
|-----|-----|--------|-------|
| **NVIDIA** | Windows / Linux | 完整支持 | 通过 `torch.js` 安装 CUDA 版，含 flash attention；Preparer 用的 whisper.cpp 以 CUDA 编译 |
| **AMD 独显** | Linux | 完整支持 | ROCm；每天在 RX 9070 XT（RDNA4，ROCm 7.0）上测。安装时按你的 `gfx` 目标用 HIP 编译 llama-cpp-python 和 whisper.cpp |
| **AMD APU / 核显** | Linux | 完整支持，较慢 | TTS 以 fp32 加载（660M/680M/780M 这一类的 bf16 有问题）；其余相同 |
| **AMD** | Windows | 仅 CPU | Windows 没有 ROCm；要用 GPU 请用 Linux |
| **Apple Silicon** | macOS | 仅 CPU | Qwen3-TTS 不支持 MPS；whisper.cpp 以 Metal 编译 |
| **Intel** | macOS | 仅 CPU | |

> **文档：**上游的 [Wiki](https://github.com/Finrandojin/alexandria-audiobook/wiki) 对声音类型、LoRA 训练、批量生成的说明仍然适用；本分支不同之处以本 README 和 `RECIPES.md` 为准。

## 安装

### 方式 A：Pinokio（推荐）

1. 安装 [Pinokio](https://pinokio.computer/)
2. 在 Pinokio 里点 **Download**，粘贴 `https://github.com/on22s/alexandria-audiobook2`
3. 点 **Install**——创建 `app/env`，安装与平台匹配的 torch（`torch.js`），并把它钉住，防止后续安装换成 CPU 版；为你的 GPU 编译 llama-cpp-python 和 whisper.cpp
4. 点 **Start**，然后 **Open Web UI**

### 方式 B：Google Colab（无需安装）

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/on22s/alexandria-audiobook2/blob/main/alexandria_colab.ipynb)

在免费 T4 上运行。需要一个免费的 [ngrok](https://dashboard.ngrok.com/signup) 账号做隧道；笔记本里有完整步骤。

### 方式 C：Docker（NVIDIA）

```bash
git clone https://github.com/on22s/alexandria-audiobook2.git
cd alexandria-audiobook2
docker compose up --build
```

需要 [Docker](https://docs.docker.com/get-docker/) 和 [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)。界面在 `http://localhost:4200`；TTS 权重首次使用时下载到卷里；上传、声音配置、适配器和音频通过绑定挂载持久化。

> Compose 默认只发布在 `127.0.0.1`。容器内部绑定 `0.0.0.0`（Docker 转发需要）。如果你把宿主机端口发布到 `0.0.0.0`，请同时设置 `ALEXANDRIA_AUTH_PASSWORD`——见英文页的 [Authentication](README.md#authentication-optional)。

## 首次启动——必读

### 1. 先启动 LLM 服务器

Alexandria 不包含 LLM。生成脚本前，下面之一必须在运行并能从 Setup 页的 Base URL 访问：

- **llama.cpp**（推荐，也是所有数字的测量环境）：
  ```bash
  llama-server -m Qwen3-14B-Q4_K_M.gguf --host 127.0.0.1 --port 8090 \
    -ngl 99 -c 32768 --parallel 1 --flash-attn on \
    --reasoning on --reasoning-format deepseek --reasoning-budget 1024 \
    --lora rightsclean.f16.gguf        # 可选：归属适配器
  ```
  Base URL 填 `http://127.0.0.1:8090/v1`，密钥 `local`，模型名填 `--alias`（或文件名）。
- **LM Studio**：加载模型、启动服务器，`http://localhost:1234/v1`。在 Setup 里勾选 **Optimize LM Studio settings**，应用会钉住需要的上下文长度和并行槽位（显存被占时模型悄悄以 8k 上下文加载，是“今天比昨天慢十倍”最常见的原因）。
- **Ollama**：`http://localhost:11434/v1`，模型名按 `ollama list` 显示的填。
- **托管 API**：把 LLM Location 切到 *Remote*，填 URL 和密钥（或 `env:DEEPSEEK_API_KEY` 从环境变量读取），并取消勾选“Runs on this machine's GPU”，这样它标注时本机可以同时渲染音频。

思考型模型没问题——反而更好。把 **Reasoning effort** 设为 *low*，让服务器限制预算；不要把 `<think>` 加进禁用 token。用 Setup 里的 **Test Connection** 确认连接并查看模型回复。

### 2. 首次 TTS 生成会下载约 3.5 GB

Qwen3-TTS 权重（每个变体约 3.4 GB：CustomVoice，以及用于克隆/设计/LoRA 的 Base）在第一次渲染时下载。看 Editor 的日志；卡在 0% 的下载是网络问题，不是死机。

### 3. 首批生成有预热

模型加载，若开启 Compile Codec 还有 30–60 秒的 `torch.compile`。Editor 的活动行会告诉你是哪一个。

### 4. 显存决定你能同时做什么

TTS 约 3.4 GB 加批处理余量；同一张卡上的 LLM 需要自己的空间。脚本生成占着卡时，GPU 锁会拒绝启动音频渲染，*除非*当前 LLM 配置档标记为不在本机 GPU（托管 API 或另一台机器）——那样标注和渲染可以重叠。

### 5. 出问题时去哪里看

- Script 页 Generate 按钮下方的**活动行**：运行在等什么、第几次尝试、剩余时间估计。
- `logs/api/<task>-latest.log`——每个后台任务的完整日志。
- `logs/review_responses.log`——脚本生成和审阅的每一次 LLM 请求与回复，含结束原因、token 数和耗时。
- 运行中的任务：`GET /api/status/eta` 和 `GET /api/status/<task>`。
- **Reports** 页：带产物的运行历史、审阅存档点。

## 新手指南：你的第一本有声书

### 开始之前
- 一本 `.txt`、`.md` 或 `.epub` 格式的书。
- 一个运行中的 LLM 服务器（见上一节）。16 GB 显卡上一个 9 GB 的 Qwen3-14B GGUF 就能得到不错的脚本；[推荐的 LLM 模型](#推荐的-llm-模型)一节的表说明每种规模能换来什么。
- 中端 GPU 上一本短书约二十分钟；其余的 Script 页会边跑边告诉你。

### 第 1 步——Setup
选择 **LLM Location**（Local 或 Remote），填 **Base URL**、**API Key**、**Model Name**（刷新按钮列出服务器提供的模型），思考型模型把 **Reasoning effort** 设为 *low*，点 **Test Connection**。TTS 部分保持 `local` / `auto`。**Prompt Settings** 里的归属提示词已经是 `michel2_full`（所有基座上测得的最佳），直接 **Save Configuration**。**Auto-Configure** 会根据你的显卡填好 TTS 批处理设置。

### 第 2 步——Script
选择书（或复用之前的上传）。如果小说是第一人称叙述，填入该角色的准确名字。点 **Generate Annotated Script**。按钮下方的活动行会显示 *Step 1 (split) · unit 3 of 41 — asking the model*，然后 *Step 2 (speakers)*、*Step 3 (delivery)*，重试按尝试次数显示，流水线掌握速度后给出剩余时间。你可以 **Pause**、**Save snapshot**（把完成的部分存成脚本）、**Start over**，或之后 **Resume failed run**。完成后可选 **Review Script** 或 **Contextual Review (+/- N)**，用 **Find Nicknames** 和 **Edit aliases** 让“Betty”和“BEATRICE”共用一个声音，再 **Save Current** 存入库。

### 第 3 步——Voices
每个说话人一张卡片。为每个角色选择类型——CustomVoice（最快）、Clone、LoRA 或 Voice Design——或者点 **Generate Personas**，让模型描述每个角色、渲染参考音频并分配克隆声音。范围选择器默认只处理*还没有声音的角色*，“Save the current voices to the library first”会保留你已有的。**Suggest LoRA Voices** 把训练好的声音匹配给角色。卡片上的**风格时间线**可以让声音从某一行起改变（“时间跳跃后变老”），身份锚点保证还是同一个人。**Save to cast** 和 **Apply cast** 把角色表带到整个系列。

### 第 4 步——Editor
**Render Pending** 批量渲染所有分块。试听，在线编辑文本或 instruct，重新生成单块，勾选 **flagged only** 查看音频偏离说话人声音的分块（**Check Voices** 运行漂移检查），然后 **Merge All**。

### 第 5 步——Result
播放有声书；下载 MP3；用文件名模板 **Export chapters**；**Export to Audacity** 得到每说话人一轨；**Export M4B** 带标题、作者、朗读者、封面和章节标记。

### 出问题了
活动行会说明运行在等什么。查看 `logs/api/<task>-latest.log`，然后看[常见问题](#常见问题)。

## 界面说明

各页的每一个控件在英文页的 [Web interface](README.md#web-interface) 一节逐项列出；这里只概括：

- **1 · Setup**——两套 LLM 配置档（URL、密钥、模型、超时、重试/退避、自定义请求头和请求体、发送方式 http/manual、是否在本机 GPU、推理强度、故障切换、远程 SSH 别名）；TTS 设置（模式、外部服务器池、设备、语言、并行数、种子、编解码器编译、子批次、停顿）；三遍参数（第一遍每请求字符数与对话检测方式、第二遍每请求行数与周围文本量、各遍温度、上下文救援窗口）；归属提示词下拉、可编辑的系统/用户/示例文本、预设、“What the model will see”、审阅提示词；LM Studio 优化。
- **2 · Script**——上传或复用、第一人称叙述者、批量模式与排序、Generate / Start over / Pause / Save snapshot / Cancel / Resume failed run、合并重复角色名、剥离译者前言、审阅与上下文审阅、昵称与别名、批量审阅、已保存脚本、手动传输面板。
- **3 · Voices**——每说话人一张卡片与 Alias of、隐藏已就绪、人设生成范围、先存库、LoRA 推荐、候选应用/忽略、暂停人设运行的校验并保存/续跑、风格时间线、系列角色表。
- **4 · Editor**——顺序播放、渲染待处理、全部重新生成、检查声音（漂移）、仅看标记、文本完整性、全部合并。
- **5 · Result**——播放与下载、Audacity 导出、M4B 导出（标题、作者、朗读者、年份、简介、封面、逐块章节）、分章导出（文件名模板、编号位数、格式、书名、系列名、卷、章节选择、逐块、仅变化、要求就绪、预览文件名、预设）。
- **Designer / Preparer / Dataset / Training / Voice Lab / Reports**——见英文页对应小节，以及 [PREPARER_GUIDE.md](PREPARER_GUIDE.md)、[lora.md](lora.md)、[BATCH_PROCESSOR_GUIDE.md](BATCH_PROCESSOR_GUIDE.md)。

## 性能

在 RX 9070 XT（16 GB，ROCm）上、GPU 锁下一次一个任务测得（除非另注）：

| 任务 | 速度 |
|---|---|
| 批量 TTS 渲染，CustomVoice | 3–6 倍实时 |
| LoRA 声音重训，一个适配器（200 段） | 约 5.4 分钟 |
| 身份门检查，一个适配器 | 2.0–2.7 分钟 |
| 三遍归属，一部小说 40 个窗口，本地 A3B IQ2/IQ3 GGUF，reasoning low | 每部约 30–50 分钟 |
| Qwen3-14B Q4_K_M 在 llama.cpp 里完全放进显卡 | 约 32 tok/s |
| 全部单元测试（CPU） | 约 20 秒 |

**批处理设置**：16 GB 上 Parallel Workers 4–8，开启子批次，长书开启 Compile Codec（预热 30–60 秒后解码快 3–4 倍）。**Auto-Configure** 会按你的显卡选好。

**ROCm 说明**：应用自己套用 RDNA 专用设置（`device_utils.enable_rocm_optimizations`）；AMD APU 上 TTS 用 fp32；LLM 侧 RDNA4 上 ROCm 比 Vulkan 快约 11%，KV 缓存量化是让 27B 和 TTS 同时挤进 16 GB 的关键。千万不要用 PyPI 的 `llama-cpp-python` 轮子覆盖安装器编译的 HIP 版——那是纯 CPU 版，而且会静默替换。

## 脚本格式

标注脚本是一个 JSON 数组；每个条目是 TTS 要读的一行：

```json
[
  {"speaker": "NARRATOR", "text": "雨已经下了三天。", "instruct": "低沉、平稳、不急不缓。"},
  {"speaker": "MARA", "text": "我们该回头了。", "instruct": "紧张、轻声、近乎耳语。"},
  {"speaker": "TOMAS", "text": "还不行。", "instruct": "平直、坚决。"}
]
```

- `speaker` 是 `NARRATOR` 或角色表里的大写角色名（模型判断不了时是 `UNKNOWN`；审阅遍和别名会清理这些）。
- `text` 就是说出口的话——归属标签（“她说”）属于旁白而非对话，引号会去掉：说话这个事实由 speaker 字段承载，不靠标点。
- `instruct` 是第三遍写的演绎指令；Voices 页的身份锚点在渲染时叠加，所以 instruct 改不了声音是谁。

### 非语言声音
第三遍把发声写成可读的文字（“Ahh!”、“Mmm…”、“Haha!”）并配上 instruct；TTS 直接读出来。任何不可读的内容（裸标签、图形假名）都不会到达引擎——目标 5.1。

### 中文书籍处理提示
- TTS Language 设为 `Chinese`（或 `Auto`）。
- 中文归属的探针见 `app/experiments/chinese_attribution.py`（WP / JY 语料，角色匿名化）：Qwen3-14B 基座在 150 行上 146/150，rights-clean 适配器不增不减；n=1000 的单元已排队。
- 来源修复会处理常见的编码乱码；上传后仍显示乱码的书，用已保存脚本里的修复预览。

## 输出文件

英文页 [Output files](README.md#output-files) 有完整列表。要点：`uploads/` 是你上传的书；`annotated_script.json` / `voice_config.json` 是当前书及其声音；`scripts/` 是脚本库；`voicelines/` 每块一个音频；`cloned_audiobook.mp3` 是合并结果；`audiobook.m4b`、`chapter_exports/` 是导出；`logs/api/` 与 `logs/review_responses.log` 是日志；`ab_test_runtime/experiments/` 是全部测量产物。

## API 参考

见英文页 [API reference](README.md#api-reference)：可选的 HTTP Basic 认证、核心流程的 curl / Python / JavaScript 示例，以及按路由文件生成的 181 条路由表。交互式文档在 `/docs`，schema 在 `/openapi.json`。

## 推荐的 LLM 模型

每一行都在四本书产品测试集（768 行）上、用应用自身的流水线在 temperature 0、JSON schema、reasoning low（1,024 token 预算）下测得（RECIPES §"Prompt variants × bases"，2026-09-18）。“最佳变体”是在 Setup 里应选的归属提示词。

| 模型 | GGUF | 适合的显卡（文件 + 上下文） | 最佳变体 | 分数 | 适合什么 |
|---|---|---:|---|---:|---|
| DeepSeek v4-pro（托管 API） | — | 无需 | `michel2_full`，思考 low 8k | **95.4** | 上限；一本测试集大小的书约 $0.50–0.75 |
| Qwen3.8-27B UD-Q4_K_M | 16.5 GB | 24 GB | `michel2` | **90.9** | 本地最佳；带示例的 `michel2_shot` 在最难的书上最高（94.4） |
| Qwen3.6-35B-A3B UD-Q4_K_XL / IQ3_XXS / IQ2_XXS | 22.4 / 13.2 / 10.8 GB | 24 / 16 / 16 GB | `michel2_full` | 四本书 89.6；九本书 IQ3 / IQ2 为 91.6 / 90.5 | 用 `--n-cpu-moe` 卸载部分专家后能塞进 16 GB 显卡的 MoE，IQ2 也只比 Q4 差一分；量化阶梯正测到 IQ1_M |
| Muse-Glimmer-30B UD-Q3_K_XL | 13.4 GB | 16 GB | `michel2` | 86.6 | 强，但必须开推理并用 deepseek 推理格式，绝不能 `--skip-chat-parsing` |
| Qwen3-14B Q4_K_M | 9.0 GB | 12 GB | `michel2_full` | 82.0 | 提示词带来 +16；rights-clean 适配器在 `default` 提示词下再 +8.6 |
| Qwen3.5-9B / Qwen3-8B Q4_K_M | 5–6 GB | 8 GB | `michel2_full` | 约 72 | 8 GB 显卡的选择；两者在最难的书上都崩（47 / 62） |

在所有基座上都成立的规律：
- `michel2_full` ≥ `michel2` ≥ `michel` ≥ `default`（唯一例外是 Qwen3.8，`michel2` 比 `michel2_full` 高一分）。周围文本块是最大的一步。
- 带示例（`michel2_shot`）除了 Qwen3.8 之外从不加分。
- **在 `default` 提示词上训练的适配器换提示词就会掉分。** rights-clean Qwen3-14B 适配器用 `default`；`michel2` 适配器用 `michel2_full`。
- 会推理的模型开 reasoning low 并在服务器端限预算，都比关掉好；预算 512 / 1024 / 2048 是平的（适配器单元 +8.1 / +8.6 / +8.8）。不要禁用 `<think>`。
- 空闲 GPU 上 temperature 0 是确定的：重跑得到逐行相同的分数。你看到的“噪声”都是另一个任务在共用显卡。

## 常见问题

### Pinokio 到不了 “Open Web UI”

`start.js` 等待 Alexandria 打印服务地址。导入失败、端口占用、Python 回溯和 FastAPI 启动失败都会让启动器明确停下，而不是停在 **Starting**。

1. 打开正在运行或失败的 Start 条目旁的 **Terminal**，读第一个回溯或启动错误。导航栏的构建标签显示实际运行的版本；悬停可看 Python 与包版本。
2. 打开 Pinokio 的 **Logs** 页，选最近的 Alexandria 会话。其 **Get Help** 报告会打包相关启动器日志和系统信息（经 Pinokio 常规的密钥/路径脱敏）便于分享。
3. 直接看文件：当前启动器日志是 `logs/api/start.js/latest`，带时间戳的运行在旁边，`logs/sessions/` 把相关的 install/start/helper 运行归组。应用任务日志（脚本生成、审阅、音频）仍在 `logs/api/*-latest.log`。
4. 修好第一个启动错误后，停止并重新启动已有的 `start.js` 条目。不要为绕过端口占用再启动一份。`env_doctor.py` 会报告 `app/env` 缺失或 torch 被后续安装换成 CPU 版——这是最常见的两个原因。

启动器使用 Pinokio 分配的空闲端口并把 Alexandria 绑定到 `127.0.0.1`；不需要也不建议写死端口。

### 脚本生成时“闲置”很久
看 Generate 下方的活动行：它写着步骤、单元、第几次尝试、在等什么（模型、限流退避、重试）。如果几分钟没有变化，是 LLM 服务器不回应了——在 Setup 里测试。“重试耗尽时 → 暂停”的运行会等待 Resume。

### Pause 是灰的
Windows 上应用无法挂起工作进程；那里 Pause 直接禁用，而不是每次点击都失败。Cancel 和 Resume failed run 可用。

### 脚本生成失败
- 模型的回答不在 JSON 约定内：从上表选模型，保持 schema 开启，推理设为 low。`logs/review_responses.log` 有原始回复。
- 上下文长度：以 8k 加载的模型会静默地在长窗口上失败。LM Studio 勾选 **Optimize LM Studio settings**；llama.cpp 传 `-c 32768`。
- 完全没有 API：把 **How requests are sent** 切到 `manual`，在 Script 页面板里自己回答提示词。

### 模型下载失败或很慢
TTS 权重在第一次渲染时从 Hugging Face 下载；代理或下载了一半的缓存会表现为卡在 0%。删掉 Hugging Face 缓存里的残缺文件重试；`download_model.py` 可在应用外下载。

### TTS 生成失败
- `logs/api/audio-latest.log`——第一个 traceback 才是真正的原因。
- AMD APU：应用自动用 fp32；如果你强制了 `bf16`，去掉它。
- “不出声”的合并适配器（目标 2.3）是训练声音的缺陷，身份门会在晋升前拦下；此时给该角色先用内置预设。

### 批量生成慢
开 Compile Codec、开子批次、Parallel Workers 设到显存允许的值；首批包含模型加载和编译预热。同一张卡上的 LLM 会把 TTS 能用的显存减半——托管 LLM 请标记为不在本机 GPU。

### 显存不足
降低 Parallel Workers 和 Max Items/Batch；显存余量检查会拒绝启动装不下的批次，所以运行中途 OOM 通常意味着检查之后有别的进程占了显卡。

### MP3 文件损坏或很小（428 字节）
ffmpeg 缺失或不在 PATH；安装器会把它装进环境，所以 428 字节的文件说明应用没在 `app/env` 里运行。每个生成文件都会被校验（目标 3.2）——Editor 会标记该分块。

### 音质问题
短于约 7 秒的克隆参考会得到不稳定的声音（目标 2.2 的发现）；导入门现在会规范化并测量它们。描述音色的 instruct（“低沉”“沙哑”）会和声音打架——身份锚点会剥掉这些词；审计发现 1–4% 的 instruct 带有它们。

### 文本乱码或缺字
上传时的来源修复处理常见编码；仍显示 `â€™` 的书，用已保存脚本里的修复预览。图形假名等不可读内容会在到达 TTS 前按设计丢弃（目标 5.1）。

## 提示词自定义

三遍各读 `app/` 下的一个提示词文件：`default_prompts_segment.txt`、`default_prompts_attribute.txt`、`default_prompts_instruct.txt`（系统消息、`---SEPARATOR---`、带 `{roster}` 和 `{batch}` 占位符的用户消息）。审阅用 `review_prompts.txt`。

- **变体**：`app/attribution_prompt_variants.py` 定义每个归属变体如何改写请求（`michel2_full` 的周围文本块、`michel2_shot` 的示例……）。Setup 的下拉选择其一；显示的文本就是将要发送的。
- **预设**：在 Setup 里编辑文本并 **Save as preset**；预设保存在 `config.json` 里，重启后仍在。**Reset to Defaults** 不重启即可重新读取文件。
- **What the model will see** 显示当前设置下第二遍会发送的确切系统与用户消息（`POST /api/prompts/attribution_preview`）。
- 改提示词就改变了测量：RECIPES 里的分数属于它的变体，适配器属于训练它的提示词。

## 项目结构

见英文页 [Project structure](README.md#project-structure)。

## 贡献

- 提交前运行 `./ready.sh`：它重新生成派生文件（API 契约快照、单元测试清单、结果索引、审计），然后运行发布校验器。CI 跑同样的检查。
- 派生文件从不手工合并——`.gitattributes` 把它们标为 `merge=ours`，`post-merge` 钩子重建它们。`./ready.sh` 会在新检出里安装钩子。
- GOALS 或 RECIPES 里的数字要指明产物；关于数字含义的断言是另一句话。改这两个文件前先读 `CLAUDE.md` 的规则 19–26。
- PR 提到 `on22s/alexandria-audiobook2` 的 `main`。

## 致谢

- [Finrandojin](https://github.com/Finrandojin/alexandria-audiobook)——原版 Alexandria，本分支建立在其上，绝大部分代码仍与之共享。
- [Ayush Naphade](https://github.com/aayushnaphade)——上游的人设生成、说话人别名解析和上下文审阅；以及从他的分支审阅采纳的 UX 修复。
- [buddies](https://github.com/buddies/alexandria-audiobook) 的分支（Xiao Zhang）——移植进 `instruct_lexicon.py` 的逐行 instruct 词表（MIT；声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)）。
- [Darkkingwill](https://github.com/Darkkingwill/alexandria-audiobook) 的分支——#534 中重做的可取消合并与 Result 页修复。
- [cjdell](https://github.com/cjdell/alexandria-audiobook) 的分支——#573 移植的 AMD APU fp32 修复。
- [XinchaoGou](https://github.com/XinchaoGou/alexandria-audiobook) 的分支——#574 移植的 OpenAI 推理模型请求格式与 `env:NAME` 密钥引用，以及外部 TTS 服务器池的思路（#532）。
- Qwen3-TTS、Qwen3 和 Muse-Glimmer 提供模型；Project Dialogism Novel Corpus（Vishnubhotla、Hammond、Hirst）、RiQuA（Papay & Padó）和 DraCor 提供每个归属数字所依赖的标注文本——PDNC 的标注未声明许可证，已向作者请求授权；Kokoro、LJSpeech、Hi-Fi TTS 和 AISHELL-3 提供声音上限所用的真人朗读。

## 许可证

MIT（见 [LICENSE](LICENSE)）。Hub 上的归属适配器为 Apache-2.0；训练数据的权利见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

### 第三方许可证

- [Qwen3-TTS](https://github.com/Qwen/Qwen3-TTS)——Apache License 2.0，阿里巴巴 Qwen 团队
- [Qwen3](https://huggingface.co/Qwen) 与 [Muse-Glimmer-30B](https://huggingface.co/meta-models/Muse-Glimmer-30B)——Apache License 2.0
- [llama.cpp](https://github.com/ggml-org/llama.cpp)、[whisper.cpp](https://github.com/ggml-org/whisper.cpp)——MIT
