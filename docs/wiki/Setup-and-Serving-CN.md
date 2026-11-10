# Setup 与模型服务

[中文主页](Home-CN.md) · [English](Setup-and-Serving.md)

应用通过任何 OpenAI 兼容端点与语言模型通信，端点可以在本机，也可以在远程机器上。README 的[首次启动一节](../../README.md#first-launch-what-to-expect)说明了如何启动 llama.cpp、LM Studio、Ollama 或托管 API。本页说明经过测量、确实重要的服务端设置。

## 通用规则

- **推理强度用 low，不要关闭，也不要禁用 `<think>`。** 在每一个会推理的模型上，带服务端预算的 reasoning low 都优于关闭推理。
- **保持 JSON schema 开启。** 每个请求都会带上一个。
- **根据请求和可用内存选择上下文**，而不是照抄一个最大值。见下面的实际设置步骤。

## 选择上下文大小

**8,192 是本地起点；32,768 并非通用要求。** 上下文必须容纳完整提示词（指令、角色表和周围文本）、生成的回复以及所有推理 token。上下文越大，占用的 KV 缓存内存也越多。

1. 先用短文本和一个服务器槽位（`--parallel 1`）。在 llama.cpp 里，第一次运行可试 `-c 8192` 限制内存占用，然后检查请求预算。
2. 在 Script 页生成完整脚本前，先运行 **Test this book with the LLM**。它会采样开头、中间和一段对话密集的片段；通过是有用的证据，但并不保证之后每个请求都成功。
3. 如果请求超出上下文，查看服务器错误和 `logs/review_responses.log` 里的 token 数。按需调小 **Step 1: text per request**、**Step 2: lines per request**、周围文本或审阅窗口。要给输出留出空间：切分会重新输出文本，所以只看输入长度会低估所需空间。
4. 只有在模型支持且显存装得下时才增大上下文。`-c 32768` 是一种更大的可选设置，不是万能解法。如果模型和缓存无法同时放下，可能需要更小的量化或远程服务器。

**LM Studio 优化实际做了什么。** 在当前实现中，本地回退值是 8,192 token 和一个槽位。已知的模型配置档可以选择 16,384 或 32,768（有时是两个槽位），但要经过带 2 GiB 预留的实时显存保护检查。未知模型或读不到的指标会沿用回退值。远程优化器另有一个 98,304 token、两个槽位的目标；不要假设这个目标适合任意远程机器。启用远程优化前，先检查它的资源和设置。

来源：[加载配置档与保护检查](../../app/lmstudio_settings.py)和[书籍预检与请求预算](../../app/three_pass_generate.py)。它们描述的是当前代码，不是一次新的硬件基准测试。早先模型卡的说明提到 5,966 token 的归属窗口，但没有关联的测量能证明这是每本书、每个分词器、每个流水线遍或每个补全预算的上限。请把[模型卡的显存数字](Which-Model-For-Your-Card-CN.md)当作其在所述配置下的测量值，而不是所有请求都能装进 8k 的保证。

## 部署 Muse-Glimmer-30B

开启推理时，Muse 需要 DeepSeek 推理格式：

```bash
llama-server \
  -m Muse-Glimmer-30B-UD-Q3_K_XL.gguf \
  --reasoning on \
  --reasoning-format deepseek \
  --chat-template-kwargs '{"reasoning_strength":"low"}' \
  --parallel 1
```

- 每个请求都发送 JSON schema。
- 带推理评估 Muse 时，**绝不要用 `--skip-chat-parsing`**。

## 完整评估适配器之前

先运行服务契约预检。它必须在适配器的候选 scale 下**以及**在 base scale 0.0 下，成功解析一个 `michel2_full` 请求。

把候选 scale 记录到产物里。如果测量过的 scale 扫描选了更低的值，就不要假设是 1.0。
