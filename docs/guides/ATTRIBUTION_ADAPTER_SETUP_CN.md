[English](ATTRIBUTION_ADAPTER_SETUP.md) | [中文](ATTRIBUTION_ADAPTER_SETUP_CN.md) | [日本語](ATTRIBUTION_ADAPTER_SETUP_JA.md)

# 启用归属适配器

现在适配器存在的理由只有一个：**让更小的量化档位也能上线。** 提升基座模型分数的是提示词（`michel2_full`）；只有在测出确实有收益的那一档，加载适配器才值得。下表全部来自四本书的产品测试集（768 行，在同一台服务器上、通过切换适配器 scale 做基座 → 适配器的成对比较，temperature 0，JSON schema，`michel2_full`），出自公开合集
[Om22s/alexandria-qwen3-attribution](https://huggingface.co/Om22s/alexandria-qwen3-attribution)
（RECIPES §"Attribution adapters"，2026-09-20）：

| 基座 | 档位（文件） | 推理 | 基座 → 适配器 | 是否加载？ |
| --- | --- | --- | ---: | --- |
| Qwen3.8-27B | IQ2_XXS (7.3 GB) | 开 | 83.1 → **85.7 (+2.6)** | **加载**——这个适配器就是为这一档做的 |
| Qwen3.8-27B | Q3_K_XL (13.1 GB) | 开 | 87.5 → **88.8 (+1.3)** | 加载 |
| Qwen3.8-27B | Q4_K_M (16.5 GB) | 开 / 关 | 89.8 → 89.8 (0) / 87.4 → 88.3 (+0.9) | 开推理时不加载；关推理时收益很小 |
| Qwen3.6-35B-A3B | IQ1_M (10.0 GB) | 关 | 82.9 → **85.9 (+3.0)** | **加载，但只能在关推理时** |
| Qwen3.6-35B-A3B | IQ1_M | 开 | 87.5 → 85.0 (−2.5) | 不加载 |
| Qwen3.6-35B-A3B | IQ2_XXS (10.8 GB) | 开 | 88.3 → 87.9 (−0.4) | 不加载 |
| Qwen3.6-35B-A3B | IQ3_XXS (13.2 GB) | 开 | 87.5 → 86.6 (−0.9) | 不加载 |
| Qwen3.6-35B-A3B | Q4_K_XL (22.4 GB) | 开 | 90.8 → 88.5 (−2.2) | 不加载 |
| Qwen3-14B | Q4_K_M (9.0 GB) | low，预算 1024 | `default` 下 66.1 → **74.7 / 74.9 (+8.6 / +8.8)**；九部 PDNC 小说上 `michel2_full` 下 **84.3 → 84.3 (0)** | 只在用 `default` 提示词时加载；用 `michel2_full` 时请用基座 |

表里还有两件事没说清楚。A3B 适配器在 PDNC 文本（带别名的角色表）上会把角色表那一行照抄回来——`EMMA (also: EMMA WOODHOUSE, …)`——最多出现在 12% 的行上；从 #625 起应用会把它读成 `EMMA`，但测试框架会判错，所以适配器自己的模型卡上两个数字都列了。另外 Qwen3-14B 的增益与提示词绑定：它是在 `default` 的形态上训练的，换成产品提示词后增益为零。

## 这是服务端的改动，不是应用的改动

适配器由 `llama-server` 施加。`app/` 里没有任何东西用它做计算。

```bash
llama-server \
  -m /path/to/Qwen3.8-27B-UD-IQ2_XXS.gguf \
  --lora /path/to/qwen38-rightsclean-michel2.f16.gguf \
  --host 127.0.0.1 --port 8090 -c 32768 --parallel 1
```

合集里每个 `adapters/<name>/` 目录下的 `.f16.gguf` 就是被测过的那个文件（合集里的 `examples/serve_llamacpp.sh` 做的就是上面这些）；本页早期版本提到的、用语料库轻小说训练的 `adapter_mixed.gguf` 不可分发，只留在私有归档里。

然后让应用指向它，写在 `app/config.json` 里、你正在运行的那种模式的配置块内：

```json
"llm_local": {
  "base_url": "http://127.0.0.1:8090/v1",
  "model_name": "qwen3.8-27b",
  "attribution_adapter": {
    "path": "models/qwen38-rightsclean-michel2.f16.gguf",
    "scale": 1.0,
    "require": false
  }
}
```

适配器和 `base_url` 故意放在同一个块里：它属于那个端点，如果放在第二个位置，就可能对“指的是哪台服务器”产生分歧。

## 这项检查做什么

`generate_script.py` 启动时会打印：

```
LLM adapter: attribution_adapter=qwen38-rightsclean-michel2.f16.gguf scale=1.0 require=False
```

如果服务器实际上没有在提供它，会给出警告。**整件事的意义就在这里。** 没有这项检查，一个没带 `--lora` 启动、或者 scale 被调成 0 的服务器，会兴高采烈地以基座水平回答每个请求。什么都不会失败。书照样生成。但得到的只是基座的数字，而且没有任何产物记录原因——和种子 bug 同一种形态，那个 bug 在有人用耳朵听出来之前，污染了六次对比。

`require: true` 会把警告变成拒绝启动。默认只警告，因为一本夜里生成到一半的书不该因为服务器重启就死掉——但这次运行必须带上这个事实。

## 三条诚实的限制

**端点必须是 llama.cpp。** LM Studio 和 Ollama 没有实现 `/lora-adapters`，所以检查报告的是“无法验证”，而不是假装适配器不存在。这是有意的：每次配置都会触发的警告，没有人会读。

**scale 没有调过。** 1.0 是评测用的值。`POST /lora-adapters` 可以把它设成任何值，但这里没有测过 0.7 还是 1.3 更好。

**增益是在轻小说上、外加两部 PDNC 小说上测的。** 四本金标书是英译的日本轻小说；PDNC 的两本书格子（《爱玛》，未参与训练；《太阳照常升起》，是训练小说）在模型卡上。训练数据是英语公有领域散文，所以轻小说上的增益属于迁移，而且不大。
