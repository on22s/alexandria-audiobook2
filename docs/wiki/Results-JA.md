# 結果

[日本語ホーム](Home-JA.md) · [English](Results.md)

話者判定の結果をまとめた索引です。生の JSON の成果物が正本で、このページはテストセットを混同せずに実行を比較するための地図です。すべての成果物は [RESULTS_INDEX.md](../../RESULTS_INDEX.md) に一覧があります。

## これらの表の読み方

- **精度**は、未回答や不正な行を誤りとして数えます。
- **base → adapter** は、同じ提供モデルを同じテストセットで、アダプターを切った状態（scale 0）と入れた状態で比較します。
- **2 つの結果が比較できるのは**、テストセット、プロンプトのバリアント、デコード設定、出力形式がすべて一致する場合だけです。アダプターはプロンプトに依存するため、プロンプトは常に示します。
- 行に別段の記載がない限り**既定の設定**です。
  - temperature 0
  - バッチサイズ 25、出力は最大 4,096 トークン
  - すべてのリクエストに JSON schema
  - 本ごとに均等な 40 ウィンドウ
- **推論：** "off" は推論なし、"low" はプロバイダーの low 設定で、記録がある場合はサーバーの予算は 1,024 トークンです。推論 high の結果は含みません。

> **2026-09-28 に再スコアリング。** PDNC には実際には話さない名前がいくつかあります。たとえば The Sign of the Four の Wooden-Legged Man の横にある "Small" で、その別名は Jonathan Small です。SMALL と答えたモデルは正しい人物を挙げていましたが、誤りと採点されていました。レビュー済みの 5 組の名前を正しいと数えるようにし、以下のペア表はその採点を使っています。以前の 10 件の判定が変わりました。RECIPES.md §"Rescored 2026-09-28" に一覧があります。

## ペア比較のアダプター結果 — PDNC の 9 小説

すべての行：PDNC の 9 小説、2,655 行、`michel2_full` プロンプト、両方の側で同じサーバー。p 値は 2 つの側が食い違った行（修正／破壊）に対する正確な符号検定です。アダプター名は [Hugging Face リリース](Hugging-Face-Releases-JA.md)の学習プロンプトのラベルに従います。

| モデル | 量子化 | アダプター | 推論 | 行数 | base | adapter | 変化 | 修正／破壊、p | 成果物 |
|---|---|---|---:|---:|---:|---:|---|---|
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

注記：

- **The Sun Also Rises** は rights-clean `michel2v1` アダプター（Qwen3.8 の行）の学習小説の 1 つです。正直な未学習の数値は残り 8 小説で、Q4_K_M、UD-Q3_K_XL、UD-IQ2_XXS で +1.0〜+1.3 です。window25 と KL のアダプターは 9 小説のどれでも学習していません。
- **Muse KL の seed 1（IQ3_XXS）** は A6000 で −1.5（p=0.003）、上の seed 2 は A100 で +1.5 でした。seed と GPU を切り分けるため、A6000 で seed 2 を試す予定です。
- いくつかの成果物は実験フォルダーにだけあり、コミットされていません。名前を示しているので [RESULTS_INDEX.md](../../RESULTS_INDEX.md) で見つけられます。

## ベースのみの結果

これらの行はアダプターなしでモデルと量子化を測ったものです。ベースを選ぶために使い、アダプターを判断するためには使わないでください。

| モデル | 量子化 | アダプター | プロンプト | 設定 | フィクスチャ | 行数 | 精度 | 条件付き精度 | 成果物／注記 |
|---|---|---|---|---|---|---|---:|---:|---:|---|
| Muse-Glimmer-30B | UD-Q3_K_XL | なし | `michel2_full` | temp 0; low / 1,024; batch 25; schema | PDNC の 9 小説 | 2,655 | **93.33%** | 95.21% (1,869/1,963) | [生の結果](../../ab_test_runtime/experiments/lora_serving_eval__muse-q3-michel2_full-tnr1-pdnc9lite-low-schema-20260917.json) |
| Muse-Glimmer-30B | Q4 K-quant | なし | `michel2_full` | temp 0; low / 1,024; batch 25; schema | PDNC の 9 小説 | 2,655 | **94.61%** | 95.42% (1,873/1,963) | [生の結果](../../ab_test_runtime/experiments/lora_serving_eval__muse-q4-kquant17-michel2_full-tnr4-pdnc9lite-low-schema-20260921.json) |
| Qwen3.8-27B | Q4_K_M | なし | `michel2_full` | temp 0; reasoning off; batch 25; schema | PDNC の 9 小説 | 2,655 | 93.5% | — | 下のペアの行を参照 |
| Qwen3.6-35B-A3B | UD-Q4_K_XL | なし | `michel2_full` | temp 0.6; low / 1,024; batch 25; schema | 4 冊の製品フィクスチャ | 768 | **89.6%** | 89.6% | 量子化ラダーは [RECIPES.md](../../RECIPES.md) |
| Qwen3.6-35B-A3B | UD-IQ3_XXS | なし | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 4 冊の製品フィクスチャ | 768 | 88.0% | 88.0% | 量子化ラダーは [RECIPES.md](../../RECIPES.md) |
| Qwen3.8-27B | UD-Q4_K_M | なし | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 4 冊の製品フィクスチャ | 768 | 89.8% | 89.8% | 量子化ラダーは [RECIPES.md](../../RECIPES.md) |

ベースのみのより多くのラダー（28 小説の比較とモデル速度を含む）は [GPU 別のモデル選び](Which-Model-For-Your-Card-JA.md) にあります。

## ホスト型 API のベースライン

API の結果はベースのみです。同じ実験であるかのようにアダプターの変化と比較しないでください。

- **DeepSeek v4-pro**（thinking off）、9 小説：再スコアリング後で **98.0%**（以前は 96.7%）、すべての行に回答。
- **Nemotron 3 Ultra 550B A55B** を有料 OpenRouter 経由で、本ごとに。これらは独立した単一の本のベースラインです。1 つのスコアにまとめないでください。OpenRouter は要求したモデルを報告しましたが、プロバイダーとハードウェアは観測していません。

| 本 | 行数 | 正解 | 精度 | 生の成果物 |
|---|---:|---:|---:|---|
| Mansfield Park | 734 | 710 | **96.7%** | [生の結果](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-mansfieldpark-michel2full-b8-low-20260922.json) |
| Northanger Abbey | 672 | 656 | **97.6%** | [生の結果](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-northangerabbey-michel2full-b8-low-20260922.json) |
| Persuasion | 309 | 300 | **97.1%** | [生の結果](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-persuasion-michel2full-b8-low-20260922.json) |
| The Sign of the Four | 353 | 301 | **85.3%** | [生の結果](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-thesignofthefour-michel2full-b8-low-20260922.json) |
| The Sun Also Rises | 1,759 | 1,536 | **87.3%** | [生の結果](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-thesunalsorises-michel2full-b8-low-20260922.json) |

Nemotron の設定：temperature 0、1,024 トークン予算の推論 low、バッチサイズ 8、出力は最大 4,096 トークン、`michel2_full`、構造化出力 off。これらのスコアは再スコアリング前のものです。The Sign of the Four は SMALL を数えると 85.3% から 96.6% に上がります。[評価手順](Evaluation-Recipes-JA.md)を参照してください。

## 出所

公開されたすべての成果物は、プロンプトのバリアント、モデル、量子化、実行環境、アダプターのパスとハッシュ、gold ファイルのハッシュ、デコード設定、検証状態、すべての行の結果を記録します。実行を繰り返した場合は両方の成果物を保持し、繰り返しにはラベルを付けます。以前の測定を置き換えることはありません。
