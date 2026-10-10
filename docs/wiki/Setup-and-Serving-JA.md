# Setup と LLM サーバー

[日本語ホーム](Home-JA.md) · [English](Setup-and-Serving.md)

アプリはローカルまたはリモートの LLM に OpenAI 互換 API で接続します。サーバー起動の例は [README の初回起動](../../README_JA.md#初回起動)を参照してください。

## 基本設定

推論モデルは **Reasoning effort** を *low* にし、サーバー側で予算を制限します。`<think>` を禁止トークンに入れないでください。各リクエストの JSON schema は有効のままにします。コンテキスト長は、リクエスト全体と利用可能なメモリーに合わせて選びます。

## コンテキスト長の選び方

**8,192 はローカルの出発点です。32,768 が一律に必要なわけではありません。** コンテキストには指示、登場人物一覧、前後の文章、応答、推論トークンが含まれます。大きくすると KV キャッシュのメモリーも増えます。

1. 短い文章と 1 スロット（`--parallel 1`）から始めます。llama.cpp では `-c 8192` を初回の例として使い、実際のリクエストの予算を確認します。
2. Script の **Test this book with the LLM** で、冒頭・中ほど・会話の多い部分を確認します。成功は参考になりますが、後続のすべてのリクエストを保証しません。
3. 超過したら、サーバーのエラーと `logs/review_responses.log` のトークン数を確認します。**Step 1: text per request**、**Step 2: lines per request**、前後の文章、レビュー範囲を必要に応じて減らします。分割処理は文章を再出力するので、入力だけで容量を見積もらないでください。
4. モデルが対応し、VRAM に収まる場合にコンテキストを増やします。`-c 32768` は選択肢の一つで、万能な解決策ではありません。重みとキャッシュが同時に収まらない場合は、より小さい量子化版やリモートサーバーを検討します。

**Optimize LM Studio settings** のローカルのフォールバックは 8,192 トークン・1 スロットです。既知のモデルでは 16,384 / 32,768、場合によっては 2 スロットを選びますが、2 GiB の余裕を残す VRAM ガードが働きます。モデル不明や計測値を読めない場合はフォールバックを維持します。リモートの最適化は別の 98,304 トークン・2 スロット目標を持つため、任意のリモート機で安全と考えず、資源と設定を確認してください。

これは [現行のロード設定](../../app/lmstudio_settings.py)と[事前確認・予算処理](../../app/three_pass_generate.py)の説明で、今回新しく測定したハードウェア性能ではありません。[モデル選択ガイド（英語）](Which-Model-For-Your-Card.md)のメモリー値も、記載された条件での測定です。

## Muse-Glimmer-30B

推論を有効にするときは DeepSeek の reasoning format を使います。

```bash
llama-server \
  -m Muse-Glimmer-30B-UD-Q3_K_XL.gguf \
  --reasoning on \
  --reasoning-format deepseek \
  --chat-template-kwargs '{"reasoning_strength":"low"}' \
  --parallel 1
```

各リクエストに JSON schema を送り、推論を使う評価では **`--skip-chat-parsing` を使用しないでください**。この例に必要なモデルパス・接続設定も合わせて指定します。

## アダプターの評価前

本格的な評価の前に served-contract preflight を実行し、候補の scale と基準の scale `0.0` の両方で `michel2_full` の応答を正しく解析できることを確認します。候補の scale は成果物に記録し、測定で小さい値を選んだ場合に `1.0` と決めつけないでください。詳しくは[評価手順（英語）](Evaluation-Recipes.md)を参照してください。
