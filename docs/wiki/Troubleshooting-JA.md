# トラブルシューティング

[日本語ホーム](Home-JA.md) · [English](Troubleshooting.md)

まず Script の Generate 下の活動表示、`logs/api/<task>-latest.log`、`logs/review_responses.log` を確認します。活動表示は待機理由、試行、残り時間を示します。ランチャーのログは `logs/api/start.js/latest`、関連セッションは `logs/sessions/` です。Pinokio の起動問題は [README](../../README_JA.md#pinokio-が-open-web-ui-まで進まない)を参照してください。

## 台本生成

### 失敗する・応答が不正

**Base URL** でサーバーに到達できるか、**Model Name** がロード中のモデルと一致するか確認し、**Test Connection** を実行します。更新ボタンでモデル一覧を確認できます。実際の応答は `logs/review_responses.log` にあります。JSON schema を有効にし、推論モデルは *low* とサーバー側の予算を使います。`<think>` を禁止しないでください。

### コンテキストが足りない

プロンプト、出力、推論の合計を確認します。8k は常に不十分なのではなく、32k も常に安全ではありません。エラーとトークン数を見て、対象段階の文章量、行数、文脈、レビュー範囲を減らすか、モデルと VRAM が許す範囲で容量を増やします。

LM Studio の最適化はモデル別のガード付き設定で、ローカルのフォールバックは 8,192 です。[コンテキスト設定](Setup-and-Serving-JA.md#コンテキスト長の選び方)を参照し、**Test this book with the LLM** を使ってください。サンプル成功は全行の保証ではありません。

### Idle・一時停止

活動表示でモデル待ち、レート制限のバックオフ、再試行を確認します。長く応答しない場合は Setup で接続テストをします。**When retries run out → pause** で一時停止した実行は **Resume** を待ちます。Windows の **Pause** は無効ですが、Cancel と **Resume failed run** は使用できます。

### 名前の揺れ

登場人物一覧はリクエスト間で引き継がれます。**Find Nicknames** と **Edit aliases** を使うか、Editor や Saved Scripts の話者修復で修正します。

### API が使えない

**How requests are sent** を `manual` にします。`manual_llm/pending.json` にリクエストを書き出し、Script のパネルでプロンプトをコピーして応答を貼り付けます。

## 音声生成

### ダウンロードが遅い・止まる

初回に Hugging Face からモデルを取得します。0% のままならネットワークやプロキシを確認します。未完了ファイルをキャッシュから削除して再試行するか、`download_model.py` でアプリ外から取得できます。HTTP 429 は Hugging Face アカウントのアクセストークンを `HF_TOKEN` に設定する方法があります。

制限のあるネットワーク向けに、英語ガイドは起動前の `HF_ENDPOINT=https://hf-mirror.com`、または `start.js` の `shell.run` の `env: { HF_ENDPOINT: "https://hf-mirror.com" }` を紹介しています。必要な場合に利用するダウンロード先を指定してください。

### ロード失敗

`logs/api/audio-latest.log` の最初の traceback と空き GPU メモリーを確認します。最後の手段として **Device** を `cpu` にできますが、低速です。AMD APU は自動で fp32 を使うため、bf16 の強制指定を解除してください。

### Clone / LoRA / Voice Design

- Clone：参照のパス、正確な文字起こし、5–15 秒の明瞭な音声を確認。約 7 秒より短い参照は不安定になりやすく、Designer の声はプレビューだけでなく保存が必要です。
- LoRA：学習完了、プロジェクトからの相対パス `lora_models/<adapter_id>`、Voices の更新を確認します。
- 学習した声が止まらない：学習率が高すぎる・過学習の可能性があります。既製の声を使い、1e-6 で再学習して epochs を見直します。[学習ガイド](Training-Guide-JA.md)を参照してください。
- Voice Design：説明は年齢・声質など、指示は感情・読み方に分け、矛盾を避けます。
- 外部 TTS：Gradio の URL と起動状態を確認。1 行ずつのリクエストで、CustomVoice / Clone に対応します。Voice Design / LoRA やローカルのバッチ処理には `local` が必要です。

## 速度と VRAM

**Auto-Configure** → **Compile Codec** → **Sub-batching** から始め、VRAM に余裕があれば **Parallel Workers** を増やします。初回はロードとコンパイルに時間がかかります。[バッチ生成](Batch-Generation-JA.md)を参照してください。

LLM が別の場所にある場合は **Provider request options** の **Runs on this machine's GPU?** を **No** にします。同じ GPU 上にあるならメモリーを共有するため、その分を見込んでください。

メモリー不足なら **Parallel Workers** と **Max Items/Batch** を下げ、他の GPU アプリを閉じます。開始前の検査後に別プロセスがメモリーを使う場合もあります。学習は GPU を専有し、アプリがモデルをアンロードします。声の種類の切り替え時は追加のメモリーが一時的に必要になることがあります。

2 回目のバッチが止まる場合はメモリー断片化も確認します。アプリはサブバッチ間で解放処理をしますが、続く場合はバッチを小さくするか、大きな実行の間に再起動します。AMD の MIOpen workspace 警告はアプリが自動対処します。

## 出力音声と文章

428 バイト程度の MP3 は ffmpeg の欠落・エンコード不可を確認します。Pinokio または `app/env` で起動してください。手動確認の例：

```bash
ffmpeg -encoders 2>/dev/null | grep mp3
```

CustomVoice は seed を変えるか、ランダムにして試せます。指示の矛盾や体の動作の記述を避け、恒常的な声質は固定記述へ、感情は行の指示へ置きます。LoRA は学習データに感情の幅を含めます。

文字化けはアップロード時の修復、Saved Scripts の修復プレビュー、UTF-8 を確認します。`â€™` のような文字化けが対象です。記号的な仮名など読み上げられない文字を TTS の前で除く動作もあります。

## 学習と Windows

loss が 10 より高い場合は WAV、文字起こし、参照音声を確認し、学習率を上げて解決しないでください。単調な声は感情を増やし、rank 8–16 も試します。学習の停止・クラッシュは最初の CUDA / ROCm エラーと GPU の専有状態を確認し、バッチ設定を見直します。

Windows のファイルロックエラーが続く場合は **Parallel Workers** を下げ、プロジェクトファイルを開いているエディターやファイルブラウザーを閉じます。アプリは原子的な保存とバックオフ付き再試行を行います。

## インストールの検証

```bash
cd app
python run_isolated_api_tests.py
python run_isolated_api_tests.py --full
```

空の専用データ領域のアプリで検証します。通常モードはモデル不要の API 経路、`--full` は GPU と LLM が必要な生成も対象です。通常モードが通っても生成や GPU の動作を保証しません。普段使う起動中のアプリを検証する方式はデータを変更するため、[ホームの注意点](Home-JA.md#インストールの確認)を先に確認してください。
