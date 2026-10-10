# API ガイド

[日本語ホーム](Home-JA.md) · [English](API-Reference.md)

UI の操作は HTTP API からも実行できます。このページは基本の流れを日本語で案内します。声の全フィールド、ペルソナの状態指定、Dataset Builder、学習、Python / JavaScript の本全体の処理例は[英語の完全なガイド](API-Reference.md)、全ルートは[英語 README](../../README.md#api-reference)を参照してください。起動中のアプリの `/docs` は対話式ドキュメント、`/openapi.json` はスキーマです。

## 基本

アドレスは `http://127.0.0.1:<port>` です。Pinokio は空きポートを選び、Docker は 4200 を使用します。以下の例の 4200 を実際のポートに変更してください。認証を設定した場合はすべての curl に `-u alexandria:your-secret` などの資格情報が必要です。[認証と公開設定](../../README_JA.md#認証と外部からのアクセス)を参照してください。

長時間の処理はバックグラウンドで開始します。`GET /api/status/<task>` の `running` が false になるまで確認し、成功・エラーのログも確認します。`GET /api/status/eta` は進捗と残り時間です。リクエスト受付だけで完了とは判断しません。

| task | 処理 |
|---|---|
| `script` | 台本生成 |
| `review` / `batch_review` | レビュー |
| `nicknames` / `persona` / `voices` | 別名、ペルソナ、LoRA 声推薦 |
| `audio` / `drift_check` | 音声生成、声のずれ検査 |
| `audacity_export` / `chapter_export` | 書き出し |
| `lora_training` / `lora_test` | 学習・試聴 |
| `voice_design` / `dataset_builder` | 声デザイン、データ生成 |
| `preparer` / `batch_preparer` / `voicelab` | データ準備・パイプライン |
| `benchmark` | ベンチマーク |

## 設定

```bash
curl http://127.0.0.1:4200/api/config
curl http://127.0.0.1:4200/api/default_prompts
curl -X POST http://127.0.0.1:4200/api/config \
  -H "Content-Type: application/json" \
  -d @config.json
```

設定の保存は変更部分だけでなく、取得した設定オブジェクト全体に変更を加えて送ります。GET の API キーは伏せられます。`config.json` は送信する設定ファイルの例です。

## 台本生成

```bash
curl -X POST http://127.0.0.1:4200/api/upload -F "file=@mybook.txt"
curl -X POST http://127.0.0.1:4200/api/generate_script \
  -H "Content-Type: application/json" \
  -d '{"first_person_narrator": null, "strip_front_matter": true, "start_over": false}'
curl http://127.0.0.1:4200/api/status/script
curl http://127.0.0.1:4200/api/status/eta
curl -X POST http://127.0.0.1:4200/api/review_script
curl http://127.0.0.1:4200/api/status/review
curl http://127.0.0.1:4200/api/annotated_script
```

`generate_script` は最後にアップロードした本、または `POST /api/uploads/select` で選んだ現在の本を処理します。ファイル名を渡す API ではありません。設定の例のフィールドは任意です。

## 声の割り当て

```bash
curl http://127.0.0.1:4200/api/voices
curl -X POST http://127.0.0.1:4200/api/save_voice_config \
  -H "Content-Type: application/json" \
  -d '{"NARRATOR": {"type": "custom", "voice": "Ryan", "character_style": "calm, measured narration"}}'
```

これはナレーターだけを示す形式例です。本全体の実行では、取得した話者一覧を確認し、必要な各話者の設定を用意してください。`type` は `custom`、`builtin_lora`、`clone`、`lora`、`design`、`ensemble` です。[全フィールドと例（英語）](API-Reference.md#voice-settings-fields)を参照してください。

ペルソナの状態指定には `speaker`、`state_version`、現在の `book_token` などが必要です。年齢 override と状態指定は組み合わせません。[状態ペルソナの API（英語）](API-Reference.md#personas)と[声の種類](Voice-Types-JA.md#人物の状態別ペルソナ)を確認してください。

## 音声生成と結合

声を全話者に設定してから、`GET /api/chunks` で行を確認し、生成する行の番号を `indices` に指定します。以下は先頭の 1 行（番号 0）だけを生成する例です。本全体を作る場合は必要な全行の番号を指定してください。

```bash
curl http://127.0.0.1:4200/api/chunks
curl -X POST http://127.0.0.1:4200/api/generate_batch \
  -H "Content-Type: application/json" \
  -d '{"indices": [0]}'
curl http://127.0.0.1:4200/api/status/audio
curl -X POST http://127.0.0.1:4200/api/merge
```

`generate_batch` の完了と音声の成功を確認してから `merge` を実行します。[行の編集・個別生成（英語）](API-Reference.md#chunks-and-rendering)、[ダウンロードと書き出し（英語）](API-Reference.md#downloads-and-export)に他の例があります。

## LoRA 学習

[学習ガイド](Training-Guide-JA.md)に従ってデータセットを用意します。

```bash
curl -X POST http://127.0.0.1:4200/api/lora/upload_dataset -F "file=@dataset.zip"
curl http://127.0.0.1:4200/api/lora/datasets
curl -X POST http://127.0.0.1:4200/api/lora/train \
  -H "Content-Type: application/json" \
  -d '{"name": "soldier_voice", "dataset_id": "gruff_soldier", "epochs": 5, "lr": 1e-6, "lora_r": 32, "lora_alpha": 128, "batch_size": 1, "gradient_accumulation_steps": 8, "language": "english"}'
curl http://127.0.0.1:4200/api/status/lora_training
curl http://127.0.0.1:4200/api/lora/models
```

`dataset_id` は取得した実際の ID に変え、`language` はデータと一致させます（日本語なら `japanese`）。例は英語データの既定値です。**lr を 5e-6 に上げないでください**。試聴・削除を含む[完全な例（英語）](API-Reference.md#voice-lora-training)も確認してください。

## Dataset Builder

[プロジェクト、行の定義、個別・一括生成、学習データへの保存の API（英語）](API-Reference.md#dataset-builder)を参照してください。メタデータ変更と行更新は既存の生成状態・音声を保持します。`ref_index` で学習用の参照を選びます。Emotion は音声生成のプロンプトで、学習ラベルではありません。

## 追加の例

[保存した台本](API-Reference.md#saved-scripts)、[Designer](API-Reference.md#voice-designer)、[Python](API-Reference.md#a-whole-book-from-python)、[JavaScript](API-Reference.md#the-same-from-javascript)の例は英語の正本にあります。新しい機能の正確なリクエスト契約は実行中の `/docs` と `/openapi.json` でも確認してください。
