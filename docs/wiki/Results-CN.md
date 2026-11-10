# 结果

[中文主页](Home-CN.md) · [English](Results.md)

说话人归属结果的紧凑索引。原始 JSON 产物是事实来源；本页是一张地图，用来比较各次运行而不混淆测试集。所有产物都列在 [RESULTS_INDEX.md](../../RESULTS_INDEX.md) 中。

## 怎么读这些表

- **准确率**把未回答或无效的行计为错误。
- **base → adapter** 在同一测试集上比较同一个被服务的模型，分别关闭适配器（scale 0）和开启。
- **两个结果只有**在测试集、提示词变体、解码设置和输出格式全部一致时才可比较。提示词总是显示，因为适配器针对特定提示词。
- **默认设置**，除非某行另有说明：
  - temperature 0；
  - batch size 25，最多 4,096 个输出 token；
  - 每个请求带 JSON schema；
  - 每本书 40 个均匀分布的窗口。
- **推理：**"off" 表示不推理；"low" 表示提供方的 low 设置，有记录的地方服务器预算为 1,024 token。不包含高推理的结果。

> **2026-09-28 重评分。** PDNC 列出了一些从不出场的名字——例如《四签名》中 Wooden-Legged Man 旁边的 "Small"，其别名是 Jonathan Small。回答 SMALL 的模型指出了正确的人，却被判错。五个已审阅的名字对现在计为正确，下面的成对表使用该评分。十个先前的判定发生了变化；RECIPES.md §"Rescored 2026-09-28" 列出了它们。

## 成对适配器结果——九部 PDNC 小说

所有行：九部 PDNC 小说，2,655 行，`michel2_full` 提示词，两组使用同一个服务器。p 值是对两组判定不一致的行（修好 / 弄坏）做精确符号检验。适配器名称沿用 [Hugging Face 发布](Hugging-Face-Releases-CN.md)里的训练提示词标签。

| 模型 | 量化 | 适配器 | 推理 | 行数 | 基座 | 适配器 | 变化 | 修好 / 弄坏，p | 产物 |
|---|---|---|---|---:|---:|---:|---:|---|---|
| Qwen3-8B | Q8_0 | window25 `michel2v2` | low | 2,655 | 77.2% | 83.9% | **+6.7** | +378/−201, p=2e-13 | `qwen3-8b-window25-michel2v2-q8_0-paired-michel2_full-tnr-0-pdnc9-20260928` |
| Qwen3-8B | Q4_K_M | window25 `michel2v2` | low | 2,655 | 76.6% | 82.8% | **+6.2** | +372/−207, p=7e-12 | `qwen3-8b-q4km-window25-paired-michel2_full-post616-pdnc9-local9070xt-20260926` |
| Qwen3-8B | UD-Q3_K_XL | window25 `michel2v2` | low | 2,655 | 71.8% | 80.8% | **+9.0** | +456/−218, p=3e-20 | `qwen3-8b-ud-q3kxl-window25-michel2v2-paired-michel2_full-pdnc9-local9070xt-20260928` |
| Qwen3-8B | Q3_K_M | window25 `michel2v2` | low | 2,655 | 72.3% | 83.4% | **+11.0** | +476/−183, p=6e-31 | `qwen3-8b-window25-michel2v2-q3km-paired-michel2_full-tnr-0-pdnc9-20260928` |
| Qwen3-8B | UD-IQ3_XXS | window25 `michel2v2` | low | 2,655 | 66.7% | 79.9% | **+13.2** | +539/−189, p=8e-40 | `qwen3-8b-window25-michel2v2-ud-iq3xxs-paired-michel2_full-tnr-4-pdnc9-20260928` |
| Qwen3-8B | Q2_K | window25 `michel2v2` | low | 2,655 | 62.5% | 73.0% | **+10.5** | +575/−296, p=2e-21 | `qwen3-8b-window25-michel2v2-q2k-paired-michel2_full-tnr-2-pdnc9-20260928` |
| Qwen3-8B | UD-Q2_K_XL (seed 1) | window25 `michel2v2` | low | 2,655 | 62.3% | 74.5% | **+12.2** | +597/−274, p=2e-28 | `qwen3-8b-window25-michel2v2-ud-q2kxl-paired-michel2_full-tnr-2-pdnc9-20260928` |
| Qwen3-8B | UD-Q2_K_XL (seed 2, A100) | window25 `michel2v2` | low | 2,655 | 65.0% | 77.0% | **+11.9** | +530/−213, p=5e-32 | `qwen3-8b-window25-michel2v2-seed2-ud-q2kxl-paired-michel2_full-tnr-1-a100-pdnc9-20260928` |
| Qwen3-8B | UD-IQ2_M | window25 `michel2v2` | low | 2,655 | 60.0% | 75.9% | **+15.9** | +644/−223, p=4e-48 | `qwen3-8b-window25-michel2v2-ud-iq2m-paired-michel2_full-tnr-2-pdnc9-20260928` |
| Qwen3.8-27B | Q4_K_M | `michel2v1` | off | 2,655 | 94.4% | 95.9% | **+1.5** | +68/−29, p=9e-5 | `qwen38-q4km-off-michel2_full-pdnc9-tnr4-20260922` |
| Qwen3.8-27B | Q4_K_M | `michel2v1` | low | 2,655 | 95.8% | 96.4% | **+0.6** | +59/−44, p=0.17 | `qwen38-q4km-on-michel2_full-pdnc9-tnr4-20260922` |
| Qwen3.8-27B | UD-Q3_K_XL | `michel2v1` | low | 2,655 | 95.3% | 96.1% | **+0.8** | +66/−45, p=0.057 | `qwen38-q3kxl-on-michel2_full-pdnc9-tnr2-20260922` |
| Qwen3.8-27B | Q2_K_XL | `michel2v1` | low | 2,655 | 93.8% | 93.9% | **+0.1** | +91/−88, p=0.88 | `qwen38-rightsclean-michel2-q2kxl-paired-michel2_full-tnr-0q-pdnc9-20260924` |
| Qwen3.8-27B | UD-IQ2_XXS | `michel2v1` | low | 2,655 | 89.0% | 90.1% | **+1.0** | +126/−99, p=0.083 | `qwen38-rightsclean-michel2-iq2xxs-paired-michel2_full-tnr-0-pdnc9-20260924` |
| Qwen3.6-35B-A3B | UD-IQ1_M | window25 `michel2v2` | low | 2,655 | 90.7% | 92.1% | **+1.5** | +138/−99, p=0.013 | `a3b-window25-rightsclean-michel2-iq1m-paired-michel2_full-tnr-1-pdnc9-20260926` |
| Qwen3.6-35B-A3B | UD-IQ2_XXS | window25 `michel2v2` | low | 2,655 | 90.9% | 91.6% | **+0.7** | +135/−117, p=0.28 | `a3b-window25-rightsclean-michel2-iq2xxs-paired-michel2_full-tnr-1-pdnc9-20260927` |
| Qwen3.6-35B-A3B | UD-IQ3_XXS | window25 `michel2v2` | low | 2,655 | 92.5% | 92.8% | **+0.4** | +96/−86, p=0.5 | `a3b-window25-rightsclean-michel2-iq3xxs-paired-michel2_full-tnr-2-pdnc9-20260927` |
| Qwen3.6-35B-A3B | UD-Q4_K_XL | window25 `michel2v2` | low | 2,655 | 93.2% | 91.1% | **−2.1** | +78/−134, p=1e-4 | [`a3b-window25-rightsclean-michel2-q4kxl-paired-michel2_full-tnr-1-pdnc9-20260927`](../../ab_test_runtime/cloud_pull_20260928/lora_serving_eval__a3b-window25-rightsclean-michel2-q4kxl-paired-michel2_full-tnr-1-pdnc9-20260927.json) |
| Gemma 4 12B | Q4_K_M | window25 | low | 2,655 | 84.4% | 87.0% | **+2.6** | +174/−104, p=3e-5 | [`gemma4-12b-w25-q4km-paired-michel2_full-tnr-0-pdnc9-20260927`](../../ab_test_runtime/cloud_pull_20260928/lora_serving_eval__gemma4-12b-w25-q4km-paired-michel2_full-tnr-0-pdnc9-20260927.json) |
| Gemma 4 E4B | Q4_K_M | window25 (on its training prompt) | low | 2,655 | 63.5% | 66.5% | **+3.0** | +267/−188, p=2e-4 | `gemma4-e4b-w25-newprompt-q4km-paired-michel2_full-tnr-4-pdnc9-20260927` |
| Gemma 4 E2B | Q4_K_M | window25 (on its training prompt) | low | 2,655 | 42.3% | 53.1% | **+10.8** | +460/−174, p=1e-30 | `gemma4-e2b-w25-newprompt-q4km-paired-michel2_full-tnr-0-pdnc9-20260927` |
| Muse-Glimmer-30B | IQ3_XXS | window25b | low | 2,655 | 93.6% | 93.5% | **−0.1** | +87/−89, p=0.94 | `muse-window25b-iq3xxs-paired-michel2_full-tnr-2-post616-pdnc9-20260926` |
| Muse-Glimmer-30B | Q3_K_XL | window25b | low | 2,655 | 94.1% | 94.2% | **+0.1** | +81/−79, p=0.94 | [`muse-window25b-q3kxl-paired-michel2_full-tnr-4-pdnc9-20260926`](../../ab_test_runtime/cloud_pull_20260927/lora_serving_eval__muse-window25b-q3kxl-paired-michel2_full-tnr-4-pdnc9-20260926.json) |
| Muse-Glimmer-30B | Q4_K_M | window25b | low | 2,655 | 94.7% | 93.6% | **−1.1** | +58/−86, p=0.024 | `muse-window25b-q4km-paired-michel2_full-tnr-0-pdnc9-20260924` |
| Muse-Glimmer-30B | Q3_K_XL | KL seed 1 / seed 2 (see RECIPES) | low | 2,655 | 94.6% | 95.1% | **+0.6** | +61/−46, p=0.18 | `muse-kl-window25-q3kxl-paired-michel2_full-tnr-2-pdnc9-20260926` |
| Muse-Glimmer-30B | IQ3_XXS | KL seed 2 | low | 2,655 | 93.1% | 94.6% | **+1.5** | +103/−63, p=0.0024 | `muse-kl-seed2-window25-iq3xxs-paired-michel2_full-tnr-1-pdnc9-20260927` |
| Qwen3-14B | Q4_K_M | rights-clean `attrv1`, `default` prompt | low | 2,655 | 68.4% | 73.2% | **+4.8** | +433/−306, p=3e-6 | `qwen3-14b-rightsclean-default-tnr2-pdnc9lite-low-schema-20260917` |
| Qwen3-14B | Q4_K_M | rights-clean `attrv1`, `michel2_full` prompt | low | 2,655 | 85.3% | 84.4% | **−1.0** | +193/−219, p=0.22 | `qwen3-14b-rightsclean-michel2_full-tnr2-pdnc9lite-low-schema-20260917` |

说明：

- **《太阳照常升起》** 是版权干净 `michel2v1` 适配器（Qwen3.8 那些行）的训练小说之一。它们诚实的留出数字是另外八部：Q4_K_M、UD-Q3_K_XL 和 UD-IQ2_XXS 上 +1.0 到 +1.3。window25 和 KL 适配器从未在这九部中的任何一部上训练。
- **Muse KL seed 1 在 IQ3_XXS** 上在 A6000 上得 −1.5（p=0.003），而上面的 seed 2 在 A100 上得 +1.5。一次在 A6000 上测试 seed 2、以区分 seed 与 GPU 的测试已排队。
- 一些产物只存在于实验文件夹里、未提交；给出名称是为了能在 [RESULTS_INDEX.md](../../RESULTS_INDEX.md) 里找到它们。

## 纯基座结果

这些行测量不带适配器的模型和量化。用它们选基座，而不是评判适配器。

| 模型 | 量化 | 适配器 | 提示词 | 设置 | 测试集 | 行数 | 准确率 | 条件准确率 | 产物 / 说明 |
|---|---|---|---|---|---|---:|---:|---:|---|
| Muse-Glimmer-30B | UD-Q3_K_XL | 无 | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 9 部 PDNC 小说 | 2,655 | **93.33%** | 95.21% (1,869/1,963) | [原始结果](../../ab_test_runtime/experiments/lora_serving_eval__muse-q3-michel2_full-tnr1-pdnc9lite-low-schema-20260917.json) |
| Muse-Glimmer-30B | Q4 K-quant | 无 | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 9 部 PDNC 小说 | 2,655 | **94.61%** | 95.42% (1,873/1,963) | [原始结果](../../ab_test_runtime/experiments/lora_serving_eval__muse-q4-kquant17-michel2_full-tnr4-pdnc9lite-low-schema-20260921.json) |
| Qwen3.8-27B | Q4_K_M | 无 | `michel2_full` | temp 0; reasoning off; batch 25; schema | 9 部 PDNC 小说 | 2,655 | 93.5% | — | 见下面的成对行 |
| Qwen3.6-35B-A3B | UD-Q4_K_XL | 无 | `michel2_full` | temp 0.6; low / 1,024; batch 25; schema | 四本书产品测试集 | 768 | **89.6%** | 89.6% | 量化阶梯见 [RECIPES.md](../../RECIPES.md) |
| Qwen3.6-35B-A3B | UD-IQ3_XXS | 无 | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 四本书产品测试集 | 768 | 88.0% | 88.0% | 量化阶梯见 [RECIPES.md](../../RECIPES.md) |
| Qwen3.8-27B | UD-Q4_K_M | 无 | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 四本书产品测试集 | 768 | 89.8% | 89.8% | 量化阶梯见 [RECIPES.md](../../RECIPES.md) |

更多纯基座阶梯，包括 28 部小说的比较和模型速度，见[适合你显卡的模型](Which-Model-For-Your-Card-CN.md)。

## 托管 API 基线

API 结果是纯基座的。不要把它们与适配器变化当作同一个实验来比较。

- **DeepSeek v4-pro**（关闭思考），九部小说：重评分后 **98.0%**（之前 96.7%），回答了每一行。
- **Nemotron 3 Ultra 550B A55B** 通过付费 OpenRouter，按书。这些是独立的单本书基线：不要把它们合并成一个分数。OpenRouter 报告了被请求的模型，但提供方和硬件未被观测。

| 书 | 行数 | 正确 | 准确率 | 原始产物 |
|---|---:|---:|---:|---|
| 《曼斯菲尔德庄园》 | 734 | 710 | **96.7%** | [原始结果](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-mansfieldpark-michel2full-b8-low-20260922.json) |
| 《诺桑觉寺》 | 672 | 656 | **97.6%** | [原始结果](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-northangerabbey-michel2full-b8-low-20260922.json) |
| 《劝导》 | 309 | 300 | **97.1%** | [原始结果](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-persuasion-michel2full-b8-low-20260922.json) |
| 《四签名》 | 353 | 301 | **85.3%** | [原始结果](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-thesignofthefour-michel2full-b8-low-20260922.json) |
| 《太阳照常升起》 | 1,759 | 1,536 | **87.3%** | [原始结果](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-thesunalsorises-michel2full-b8-low-20260922.json) |

Nemotron 设置：temperature 0，low reasoning，1,024 token 预算，batch size 8，最多 4,096 个输出 token，`michel2_full`，关闭结构化输出。这些分数早于重评分；一旦把 SMALL 计为正确，《四签名》从 85.3% 升到 96.6%。见[评估配方](Evaluation-Recipes-CN.md)。

## 来源信息

每个已发布的产物都记录其提示词变体、模型、量化和运行时、适配器路径和哈希、金标文件哈希、解码设置、校验状态，以及每一行的结果。运行被重复时，两份产物都会保留，重复的那次会标注——较早的测量永远不会被替换。
