# 评估配方

[中文主页](Home-CN.md) · [English](Evaluation-Recipes.md)

适配器评估如何运行。测量过的设置及其证据见 [RECIPES.md](../../RECIPES.md)。

## 成对设置

使用仓库里的评估器，**一个服务器**，两个适配器 scale：

- **base 组：**适配器 scale 0.0；
- **适配器组：**适配器声明的 scale——1.0，除非有记录的 scale 扫描选了别的值。

两组都使用：

- temperature 0；
- batch size 25；
- 每个请求带 JSON schema；
- 相同的提示词变体。

## 要汇报什么

- 准确率，以及未回答的行数和解析失败数；
- 每本书的分数；
- 适配器哈希和基座模型哈希；
- 提示词变体；
- 运行时详情（服务器构建、GPU、上下文长度）。

## 什么时候可以晋升适配器

- **绝不能凭 batch size 1 的冒烟测试晋升。** 晋升需要产品窗口评估和干净的服务契约预检（见 [Setup 与模型服务](Setup-and-Serving-CN.md)）。
- **如果试点选了 1.0 以外的 scale**，就要在整个测试集上重复那个确切的 scale，并汇报尝试过的每一个 scale。绝不要把 scale 1 的失败改标成成功。
- **每次都要注明测试集。** 同一个适配器可能在一个测试集上提升，在另一个上没有（见[提示词与适配器](Prompts-and-Adapters-CN.md)）。
- **除了模型，也要检查评分器。** 2026-09-28 的重评分改掉了十个判定，因为原本正确的短名字被评错了。

## 付费 API 基线

托管 API 的结果只有基座模型，要把它与适配器评估分开。

2026-09-22 的 Nemotron 3 Ultra 550B A55B 运行通过 OpenRouter 进行，使用 `michel2_full`、low reasoning、temperature 0、batch size 8，且没有结构化输出。各书分数：

| 书 | 准确率 |
|---|---:|
| 《曼斯菲尔德庄园》 | 96.7% |
| 《诺桑觉寺》 | 97.6% |
| 《劝导》 | 97.1% |
| 《四签名》 | 85.3%（2026-09-28 重评分后为 96.6%） |
| 《太阳照常升起》 | 87.3% |

- 这些是相互独立的书：不要把它们的分数合并成一个模型分数，也不要把它们当作适配器效果。
- OpenRouter 报告的是被请求的模型。路由优先 Baseten，其次 Venice，并有回退；提供方的硬件未被观测。
- 分母与产物：[RECIPES.md](../../RECIPES.md#september-22-follow-up-low-quant-adapter-and-nemotron-api-baselines)。
