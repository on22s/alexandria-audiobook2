# Alexandria Audiobook2 Wiki

[English](Home.md) · [日本語](Home-JA.md) · 中文

Alexandria Audiobook2 把一本书变成有声书，每个角色用不同的声音。语言模型判断每句话是谁说的、应该怎么读；text-to-speech 模型（Qwen3-TTS）把它读出来；你得到 MP3 或带章节的 M4B。

本分支对它做出的每一个选择都做测量。下面的研究页面说明测了什么、怎么测的；用户指南页面说明如何使用应用。

## 从这里开始

- **使用应用：**[安装](../../README.md#installation)，连接一个 LLM，然后按[你的第一本有声书](../../README.md#your-first-audiobook)操作。先用原本的[短示例](../examples/first-book.txt)，分配 CustomVoice 预设，渲染、合并并下载 MP3，然后再试整本书。
- **中文入门：**[安装与第一本有声书](../../README_CN.md#从这里开始)。下面各页都有中文版。
- **研究或开发：**下面的[研究页面](#研究)，以及 [GOALS](../../GOALS.md)、[RECIPES](../../RECIPES.md) 和[贡献说明](../../README.md#contributing)保留了测量、复现步骤和开发流程。这些都不是做一本书的前提。

- **日本語：**[日语 Wiki](Home-JA.md)与[安装与第一本有声书](../../README_JA.md#インストール)。

## 使用应用

- [Setup 与模型服务](Setup-and-Serving-CN.md)——连接语言模型，以及测过确实有效的服务端设置。
- [脚本生成](Script-Generation-CN.md)——把文本变成脚本的三遍处理、审阅遍和提示词。
- [声音类型](Voice-Types-CN.md)——CustomVoice、Clone、LoRA 和 Voice Design，以及各自的适用场景。
- [声音参考](Voice-Reference-CN.md)——声音描述和演绎指令的词汇，以及测试结果。
- [批量生成](Batch-Generation-CN.md)——渲染如何批量处理，以及如何调优。
- [编辑与导出](Editor-&-Export-CN.md)——编辑行、渲染模式、合并和导出。
- [故障排查](Troubleshooting-CN.md)——常见问题与修复。

## 构建声音

- [Dataset Builder](Dataset-Builder-CN.md)——构建声音训练数据集，每个样本都可试听。
- [训练指南](Training-Guide-CN.md)——训练 LoRA 声音，以及确实有效的设置。

## 参考

- [API 参考](API-Reference-CN.md)——curl、Python 和 JavaScript 的 HTTP 示例。

## 研究

- [适合你显卡的模型](Which-Model-For-Your-Card-CN.md)——每种显存容量下最好的语言模型，含速度。
- [结果](Results-CN.md)——模型、量化、提示词和适配器结果汇总在一处。
- [提示词与适配器](Prompts-and-Adapters-CN.md)——适配器必须遵守的规则，以及适配器何时有用。
- [评估配方](Evaluation-Recipes-CN.md)——适配器评估如何运行。
- [Thunder 运维](Thunder-Operations-CN.md)——在远程 GPU 上运行任务。
- [Hugging Face 发布](Hugging-Face-Releases-CN.md)——公开发布必须包含什么。

## 适配器的一条规则

适配器必须用它训练时的提示词来评估。在另一个提示词下得到的结果是迁移测试，而不是该适配器的结果。

## 检查安装

仓库包含一个针对真实 API 的测试套件。安全的运行方式是启动一份使用独立空数据文件夹的一次性应用副本，这样你的书和声音永远不会被碰到：

```bash
cd app
python run_isolated_api_tests.py          # quick tests — no TTS or LLM needed
python run_isolated_api_tests.py --full   # also tests generation — needs a GPU and an LLM
```

要测试已经在运行的应用，在 `app` 文件夹里用 `python -m tests.test_api --url http://127.0.0.1:<port>`——但要注意，它会在该应用的数据里创建和删除上传、脚本和声音设置。（直接运行 `python tests/test_api.py` 会以 `ModuleNotFoundError: utils` 失败；必须从 `app` 用 `-m` 运行。）

- **快速模式**会报告自己的运行/跳过计数，覆盖设置往返、上传、脚本库、声音设置、分块、状态轮询、声音设计、LoRA 列表、Dataset Builder 和错误处理，不加载任何模型。
- **完整模式**额外包含脚本生成、音频生成、批量渲染、声音设计预览和 LoRA 测试。

通过快速模式只说明该套件所跑的无模型 API 路径正常。它不能排除生成、模型加载、GPU 执行或提供方集成中的应用缺陷。用失败任务的日志来区分这些原因；完整模式覆盖更多路径，但不是对每个模型或每本书的穷尽测试。
