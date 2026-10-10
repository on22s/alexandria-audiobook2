<p align="center">
  <img src="https://github.com/user-attachments/assets/fa2c36d3-a5f3-49ab-9dfe-30933359dfbd" alt="Alexandria のロゴ" width="200">
</p>

# Alexandria Audiobook2

[English](README.md) | [中文](README_CN.md) | 日本語 · [音声サンプル](https://github.com/user-attachments/files/25276110/sample.mp3)

Alexandria Audiobook2 は、本を登場人物ごとに異なる声で読み上げるオーディオブックに変換します。`.txt`、`.md`、`.epub` を読み込み、言語モデル（LLM）が話者と読み方を指定した台本を作成し、音声合成モデル（Qwen3-TTS）が読み上げます。完成した音声は MP3 や、章付きの M4B として書き出せます。

## はじめに

| やりたいこと | 読むところ |
|---|---|
| オーディオブックを作る | [インストール](#インストール) → [最初のオーディオブック](#最初のオーディオブック) |
| LLM と GPU の設定を確認する | [動作要件](#動作要件)、[初回起動](#初回起動)、[モデル選び](#モデル選び) |
| 詳しい使い方を読む | [日本語 Wiki](docs/wiki/Home-JA.md) · [公開 Wiki](https://github.com/on22s/alexandria-audiobook2/wiki/Home-JA) |
| 研究・開発に参加する | [研究と開発](#研究と開発)、[貢献](#貢献)、[文書一覧（英語）](docs/README.md) |

研究のページを先に読む必要はありません。まず短い文章で、台本作成から MP3 のダウンロードまでを試してください。

## 動作要件

- [Pinokio](https://pinokio.computer/)、または後述の Docker / Google Colab。
- アプリから OpenAI 互換 API で接続できる **LLM サーバー**。アプリに LLM は同梱されていません。
  - [llama.cpp](https://github.com/ggml-org/llama.cpp) の `llama-server`。本プロジェクトの測定に使われ、話者判定用アダプターの `--lora` と推論予算の `--reasoning-budget` に対応します。
  - [LM Studio](https://lmstudio.ai/) または [Ollama](https://ollama.ai/)。
  - ホスト型 API。本プロジェクトでは DeepSeek v4-pro を測定しています。OpenAI 形式のエンドポイントに接続できますが、モデルやプロバイダーごとの動作は接続テストで確認してください。
- **音声合成用 GPU**：VRAM は最低 8 GB、推奨 16 GB。TTS の重みはモデルごとに約 3.4 GB で、バッチ処理にも追加のメモリーが必要です。
- **RAM**：16 GB 推奨。**ディスク**：環境と TTS の重みに約 20 GB、さらに LLM のファイルと音声用の容量。

LLM にも独自のメモリーが必要です。16 GB の GPU に 9–13 GB の LLM を載せながら音声合成も実行できるとは限りません。アプリの GPU ロックは競合する処理の同時実行を防ぎます。LLM を別のマシンで動かす場合は、Setup でそのことを指定します。

### GPU と OS

| GPU | OS | TTS | 注意点 |
|---|---|---|---|
| NVIDIA | Windows / Linux | 対応 | `torch.js` で CUDA 版を導入。flash attention を利用し、Preparer 用 whisper.cpp も CUDA でビルド |
| AMD 単体 GPU | Linux | 対応 | ROCm。RX 9070 XT（RDNA4、ROCm 7.0）で継続的に検証。インストール時に `gfx` ターゲットに合わせて llama-cpp-python と whisper.cpp を HIP でビルド |
| AMD APU / 内蔵 GPU | Linux | 対応、低速 | 660M/680M/780M 系の bf16 の問題を避け、TTS は fp32 を使用 |
| AMD | Windows | CPU のみ | このアプリの ROCm 経路は Linux 用。GPU を使う場合は Linux を使用 |
| Apple Silicon | macOS | CPU のみ | Qwen3-TTS は MPS に非対応。whisper.cpp は Metal でビルド |
| Intel | macOS | CPU のみ | 音声生成は CPU で実行 |

## インストール

### A：Pinokio（推奨）

1. [Pinokio](https://pinokio.computer/) をインストールします。
2. **Download** をクリックし、`https://github.com/on22s/alexandria-audiobook2` を貼り付けます。
3. **Install** をクリックします。`app/env` を作成し、環境に合う torch を導入・固定して、llama-cpp-python と whisper.cpp を GPU 向けにビルドします。
4. **Start** → **Open Web UI** をクリックします。

新規の Pinokio 環境では、NVIDIA 用のビルド済み SageAttention / FlashAttention wheel に合わせて CPython 3.10 を使用します。既存の環境は維持されます。別の Python バージョンでこれらのオプション wheel を要求すると、互換性エラーでパッケージ導入前に停止します。

### B：Google Colab

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/on22s/alexandria-audiobook2/blob/main/alexandria_colab.ipynb)

無料の T4 GPU で実行する手順です。トンネル用に無料の [ngrok アカウント](https://dashboard.ngrok.com/signup) が必要です。詳しい操作はノートブックにあります。

### C：Docker（NVIDIA）

```bash
git clone https://github.com/on22s/alexandria-audiobook2.git
cd alexandria-audiobook2
docker compose up --build
```

[Docker](https://docs.docker.com/get-docker/) と [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) が必要です。UI は `http://localhost:4200` で開きます。TTS の重みは初回の音声生成時にボリュームへダウンロードされ、アップロード、声の設定、アダプター、音声はバインドマウントで保存されます。

Compose はホスト側のポートを `127.0.0.1` にだけ公開します。コンテナー内部は転送のため `0.0.0.0` にバインドします。ホスト側を `0.0.0.0` に公開する場合は、[認証](#認証と外部からのアクセス)を設定してください。

## 初回起動

### 1：LLM サーバーを起動する

台本生成前にサーバーを起動し、Setup の **Base URL** から到達できるようにします。llama.cpp の短い初回テスト用の例です。モデルのパスは実際にダウンロードしたものへ変更してください。

```bash
llama-server -m Qwen3-14B-Q4_K_M.gguf --host 127.0.0.1 --port 8090 \
  -ngl 99 -c 8192 --parallel 1 --flash-attn on \
  --reasoning on --reasoning-format deepseek --reasoning-budget 1024
```

- **llama.cpp**：Base URL は `http://127.0.0.1:8090/v1`、API Key は `local`。Model Name は `--alias` の値、またはファイル名です。
- **LM Studio**：モデルをロードしてサーバーを起動し、`http://localhost:1234/v1` を指定します。**Optimize LM Studio settings** は、モデルと空き VRAM に応じた設定を適用します。ローカルのフォールバックは 8,192 トークン・1 スロットで、一律に 32k にする機能ではありません。
- **Ollama**：`http://localhost:11434/v1` を指定し、`ollama list` に表示されるモデル名を入力します。
- **ホスト型 API**：**LLM Location** を *Remote* にし、URL とキーを入力します。`env:DEEPSEEK_API_KEY` のように環境変数からキーを読み取ることもできます。**Provider request options** の **Runs on this machine's GPU?** を **No** にすると、外部 LLM の処理中にもローカルで音声を生成できます。

推論モデルでは **Reasoning effort** を *low* にし、サーバー側で予算を制限します。`<think>` を禁止トークンに加えないでください。**Test Connection** で接続と応答を確認します。

8k は初回用の出発点であり、全リクエストが収まる保証ではありません。コンテキストにはプロンプト全体、登場人物一覧、前後の文章、出力、推論トークンが必要です。大きなバッチやレビュー範囲では追加の容量が必要になります。[コンテキストの設定](docs/wiki/Setup-and-Serving-JA.md#コンテキスト長の選び方)を参照してください。話者判定用アダプターは任意です。初回は使わず、後で導入する際は学習時のプロンプトと一致させます。

### 2：音声の初回生成にはダウンロードと準備がある

Qwen3-TTS の重みは必要になった時点でダウンロードされます。各バリアントは約 3.4 GB です。CustomVoice、Base（Clone / LoRA）、VoiceDesign は別のモデルです。Editor のログを確認してください。ダウンロードが 0% のままなら、まずネットワークを確認します。

最初のバッチではモデルのロードが必要です。**Compile Codec** が有効なら、さらに `torch.compile` の準備に 30–60 秒ほどかかります。活動表示で進行状況を確認できます。

### 3：困ったときの確認先

- Script の Generate ボタン下の活動表示：処理段階、試行回数、待機理由、推定残り時間。
- `logs/api/<task>-latest.log`：各バックグラウンド処理のログ。
- `logs/review_responses.log`：台本生成・レビュー時の LLM リクエストと応答、終了理由、トークン数、所要時間。
- 実行中の処理：`GET /api/status/eta`、`GET /api/status/<task>`。
- **Reports**：実行履歴、成果物、レビューのチェックポイント。

## 最初のオーディオブック

**Setup → Script → Voices → Editor → Result** の順に進みます。画面ラベルはビルドによって少し異なることがあります。最初の目標は、短い文章から MP3 をダウンロードし、行の欠落や話者の間違いがないか聴いて確認することです。

まず数段落の `.txt`、`.md`、`.epub` を使ってください。[日本語のオリジナル短編サンプル](docs/examples/first-book-ja.txt)を `.txt` として保存して使えます。[英語サンプル](docs/examples/first-book.txt)もあります。サーバーを起動し、モデルのダウンロードと準備の時間を見込んでください。所要時間は文章、モデル、ハードウェア、設定に依存し、処理が進むと Script に推定残り時間が表示されます。

### Step 1：Setup

![Setup：LLM 接続と TTS 設定](docs/screenshots/setup.png)

1. **LLM Location**（Local / Remote）を選び、**Base URL**、**API Key**、**Model Name** を入力します。更新ボタンでサーバーのモデル一覧を取得できます。
2. 推論モデルは **Reasoning effort** を *low* にし、**Test Connection** を実行します。
3. TTS は `local` / `auto` のまま始めます。**Auto-Configure** で GPU に合うバッチ設定を入れます。
4. **Prompt Settings** の話者判定は既定の `michel2_full` から始め、**Save Configuration** をクリックします。

### Step 2：Script

![Script：文章のアップロードと台本生成](docs/screenshots/script.png)

1. 短い文章をアップロードするか、以前のアップロードを選びます。一人称小説の場合は、語り手となる登場人物の正確な名前を指定します。
2. **Test this book with the LLM** でサンプルを確認します。成功しても、後続の全リクエストが成功する保証ではありません。
3. **Generate Annotated Script** をクリックします。文章の分割、話者判定、演技指示の順に処理します。
4. 終了後に文章と話者を確認します。必要に応じて **Review Script**、**Contextual Review (+/- N)**、**Find Nicknames**、**Edit aliases** を使い、**Save Current** で保存します。
5. 中断・復旧には **Pause**、**Save snapshot**、**Start over**、**Resume failed run** を使えます。Windows では Pause は無効です。

### Step 3：Voices

![Voices：登場人物ごとの声の割り当て](docs/screenshots/voices.png)

1. ナレーションを含む各話者のカードで **CustomVoice** のプリセットを選び、試聴します。初回にクローン音声や学習済みアダプターを用意する必要はありません。
2. 一連の操作ができたら **Clone**、**LoRA**、**Voice Design**、**Generate Personas** も試せます。
3. **Save to cast** / **Apply cast** で配役を再利用できます。人物の年齢などが変わる場合の状態別ペルソナと **Voice changes** は、[声の種類](docs/wiki/Voice-Types-JA.md)で説明しています。

### Step 4：Editor

![Editor：行ごとの編集・試聴・音声生成](docs/screenshots/editor.png)

1. **Render Pending** で未生成の行を生成します。初回はモデルのダウンロードと準備が入ります。
2. 試聴し、文章、話者、演技指示を修正して、問題のある行だけを再生成します。
3. 任意で **Check Voices** を実行し、**flagged only** で声が基準から外れた行を確認します。
4. **Merge All** で有効な音声を順番に結合します。

### Step 5：Result

1. MP3 を再生・ダウンロードし、文章全体が含まれるか、声が適切かを確認します。
2. 次に本全体を試します。**Export chapters**、**Export to Audacity**、メタデータと章マーカー付きの **Export M4B** も利用できます。

### 日本語の本を使うとき

既定の台本プロンプトは英語向けです。日本語の会話括弧 `「」` や話者を示す表現に合わせて調整し、TTS の **Language** を Japanese にします。日本語 README / Wiki があることと、英語向けのモデル比較が日本語の本にもそのまま当てはまることは別です。話者、読み、感情表現を実際に試聴・確認してください。

## 主な機能

### 台本作成

- ローカル・リモートの LLM プロファイル、タイムアウト、再試行とバックオフ、カスタムヘッダー・リクエスト本文、別プロファイルへのフェイルオーバー。
- 3 段階の処理：地の文と会話の**分割**、登場人物一覧と前後の文脈を使う**話者判定**、行ごとの**演技指示**。各段階に温度とリクエストサイズを設定できます。
- Setup で話者判定プロンプト（`default`、`michel`、`michel2`、`michel2_full`、`michel2_shot`）を選択・編集し、プリセットとして保存。
- レビューで会話に混ざった「彼女は言った」などを地の文に戻し、話者・分割・指示を修正。前後 ±N 行を使うレビュー、シリーズの一括レビュー、別名の検出。
- 各段階のチェックポイント、失敗した実行の再開、完成部分のスナップショット、設定を残したやり直し。
- **manual** 転送：API の代わりに、アプリが書き出したリクエストに利用者が応答を貼り付ける方式。

### 声と音声生成

- ローカル Qwen3-TTS は別サーバー不要。外部 Gradio サーバー・プールも **CustomVoice / Clone** に対応します。**Voice Design / LoRA はローカル TTS が必要**で、外部モードではモデルロード前に非対応エラーになります。
- **CustomVoice**：9 種のプリセット。演技指示に対応。
- **Clone**：5–15 秒ほどの参照録音から声を作成。インポート時に文字起こし、出典、利用権限の根拠を記録。Clone は行ごとの演技指示を使用しません。
- **LoRA**：学習済みの声。既製アダプターもあり、演技指示に対応。
- **Voice Design**：説明文から声を作成。行ごとに生成するため、同じ説明でも声の同一性は固定されません。継続する役には保存した音声を Clone の参照にする方法があります。
- 役ごとの **Character Style**（声の特徴を保つ固定記述）と、途中から特徴を変えるタイムライン。
- ペルソナ生成、声の候補推薦、配役ライブラリー、バッチ生成、長さ別のサブバッチ、任意のコーデックコンパイル、声のずれの検査。
- 対応言語：英語、中国語、フランス語、ドイツ語、イタリア語、日本語、韓国語、ポルトガル語、ロシア語、スペイン語、または自動判定。

### 編集と書き出し

行単位の編集・再生成・連続再生、話者変更時と同一話者内の間、結合時の先頭・末尾の無音除去に対応します。128 kbps の MP3、行別の音声、ファイル名テンプレートによる章別書き出し、話者別トラックの Audacity パッケージ、章付き M4B を作成できます。

### 声を作るツール

- **Designer**：声の説明からプレビューを生成し、保存。
- **Dataset**：サンプルを個別に生成・試聴して学習データを構築。
- **Training**：1 つの声の LoRA を学習・試聴。比較、ブラインド評価、採用とロールバック。
- **Preparer**：オーディオブックと EPUB / TXT を対応付け、音声とテキストのペアを作成。任意で LLM が話者、読み方、感情を補足。[Preparer ガイド（英語）](docs/guides/PREPARER_GUIDE.md)を参照。
- **Voice Lab**：ライブラリー向けの重複除去、品質監査、一括学習、声のプロファイル作成、命名。[ガイド（英語）](docs/guides/lora.md)、[一括処理（英語）](docs/guides/BATCH_PROCESSOR_GUIDE.md)を参照。
- **Reports**：実行履歴、レビューのチェックポイント、レポート、環境・LLM・TTS・学習のベンチマーク。

## モデル選び

GPU の容量だけでなく、重み、KV キャッシュ、リクエストと応答のコンテキスト、同じ GPU を使うほかの処理を考慮します。モデルファイルが収まっても、実行時のメモリーが足りるとは限りません。TTS の GPU 要件と LLM の GPU 要件も別です。

現在の英語ガイドは Qwen3.8-27B の量子化版、速度を重視する Qwen3.6-35B-A3B、Muse-Glimmer-30B、小規模な Qwen3-8B / Qwen3-14B、ホスト型 API を比較しています。容量別の量子化・プロンプト・アダプターの組み合わせは、[モデル選択ガイド（英語）](docs/wiki/Which-Model-For-Your-Card.md)を参照してください。これは記載されたテスト条件での比較であり、任意の本や日本語の品質を保証しません。

Muse を推論ありで使う場合は DeepSeek の reasoning format を使用し、`--skip-chat-parsing` を使わないでください。初回は既定の `michel2_full`、JSON schema 有効、推論 *low* から始めます。アダプターは学習時のプロンプトに依存します。`attrv1` は `default`、`michel2` 系は `michel2_full` で提供します。[導入手順（英語）](docs/guides/ATTRIBUTION_ADAPTER_SETUP.md)と各モデルカードを確認してください。

## 性能とメモリー

**Auto-Configure** から始め、VRAM に余裕がある範囲で **Parallel Workers** と **Max Items/Batch** を調整します。**Sub-batching** は長さの近い行をまとめ、無駄なパディングを減らします。長い本では **Compile Codec** が役立ちますが、最初に 30–60 秒ほどの準備が必要です。

英語版はミドルレンジ GPU で実時間の約 3–6 倍のバッチ生成速度を報告しています。これは測定条件での値で、文章、声の種類、ハードウェア、設定によって変わります。上流の 24 GB GPU の設定を 16 GB の GPU にそのまま使わないでください。[バッチ生成](docs/wiki/Batch-Generation-JA.md)を参照してください。

ROCm の調整はアプリが適用し、AMD APU は fp32 を使います。インストーラーが作った HIP 版に PyPI の `llama-cpp-python` wheel を上書きしないでください。CPU 版に置き換わる可能性があります。

## 台本の形式

注釈付き台本は JSON 配列で、1 要素が読み上げる 1 行です。以下は形式を示すオリジナルの例です。

```json
[
  {"speaker": "NARRATOR", "text": "雨は三日間降り続いていた。", "instruct": "Low, steady, unhurried."},
  {"speaker": "MARA", "text": "引き返したほうがいい。", "instruct": "Tense, quiet, near a whisper."},
  {"speaker": "TOMAS", "text": "まだだ。", "instruct": "Flat, resolved."}
]
```

- `speaker`：`NARRATOR` または登場人物一覧の大文字の名前。`UNKNOWN` は判定できなかった行で、レビューや別名の修正で確認します。
- `text`：実際に読む文章。「彼女は言った」などは地の文です。会話を示す括弧は除去し、話者フィールドで会話を区別します。
- `instruct`：第 3 段階で作る演技指示。音声生成時に声の固定記述を組み合わせます。英語の指示例はモデルに渡す語彙としてそのまま示しています。

息、笑い、ため息などは `Ahh!`、`Mmm…`、`Haha!` のように発音できる文章と指示で表します。読み上げられないタグや記号的な文字は TTS の前に除去されます。

## 保存されるファイル

データはアプリのデータ領域（指定した場合は `ALEXANDRIA_DATA_DIR`）に保存されます。組み込みアダプターと研究成果物のようなリポジトリ側の資産もあります。

| パス | 内容 |
|---|---|
| `uploads/` | アップロードした本 |
| `annotated_script.json`、`voice_config.json`、`character_aliases.json` | 現在の本、声、別名 |
| `scripts/<name>.json` と `<name>.voice_config.json` | 保存した台本と声の設定 |
| `voice_library.json` | 再利用する配役 |
| `chunks.json` | 現在の本の音声生成状態 |
| `voicelines/` | 行ごとの WAV / MP3 |
| `cloned_audiobook.mp3` | 結合した 128 kbps MP3 |
| `audiobook.m4b` | 章付き M4B |
| `chapter_exports/` | 章別ファイルと ZIP |
| `manual_llm/pending.json` / `response.json` | 手動転送のリクエストと応答 |
| `lora_models/`、`lora_datasets/`、`designed_voices/`、`clone_voices/` | 学習済みモデル、データセット、作成・参照音声 |
| `builtin_lora/` | リポジトリに同梱される読み取り専用のアダプター |
| `logs/api/`、`logs/review_responses.log`、`reports/` | 運用ログとレポート。実行履歴の API は `/api/runs` |
| `ab_test_runtime/experiments/` | `RESULTS_INDEX.md` が索引を作る研究成果物 |

## プロンプトの変更

3 段階のプロンプトは `app/default_prompts_segment.txt`、`app/default_prompts_attribute.txt`、`app/default_prompts_instruct.txt`、レビュー用は `app/review_prompts.txt` です。システムメッセージ、`---SEPARATOR---`、`{roster}` と `{batch}` を含むユーザーメッセージで構成されます。

Setup でバリアントを選び、表示された文章を編集して **Save as preset** で保存できます。プリセットは `config.json` に保存され、再起動後も残ります。**Reset to Defaults** は再起動せずに既定のファイルを読み直します。**What the model will see** は現在の設定で第 2 段階に送る正確なメッセージを表示します（`POST /api/prompts/attribution_preview`）。プロンプトを変えた結果は、元のプロンプトで測定した結果と区別してください。

## トラブルシューティング

### Pinokio が Open Web UI まで進まない

**Start** の横の **Terminal** で最初の traceback / 起動エラーを確認します。ナビゲーションバーのビルド表示で実行中のリビジョンを確認でき、カーソルを重ねると Python とパッケージのバージョンが見えます。

Pinokio の **Logs** で最新セッションを選び、必要なら **Get Help** を使います。ランチャーのログは `logs/api/start.js/latest`、関連するセッションは `logs/sessions/`、アプリの処理ログは `logs/api/*-latest.log` です。

最初の起動エラーを直して既存の `start.js` を停止・再起動します。ポート競合を避けるために二重起動しないでください。`env_doctor.py` は `app/env` の欠落や CPU 版 torch への置き換えを診断します。Pinokio は空きポートを選び、`127.0.0.1` にバインドします。

### 台本生成が長時間 Idle のまま

Generate 下の活動表示で、モデル応答待ち、レート制限の待機、再試行のどれかを確認します。応答がなければ Setup で接続テストをします。再試行上限で一時停止した実行は **Resume** を待っています。Windows で Pause が無効なのは仕様で、Cancel と Resume failed run は使えます。

### 台本生成に失敗する・応答が不正

Base URL、ロードされた Model Name、`logs/review_responses.log` の実際の応答を確認します。JSON schema を有効にし、推論を *low* にします。コンテキスト超過なら文章量、行数、前後の文脈、レビュー範囲を縮めるか、モデルと VRAM が許す範囲でコンテキストを増やします。8k が常に不足するわけでも、32k が常に安全なわけでもありません。

### ダウンロード・音声生成・書き出しの問題

- 重みのダウンロードが止まる場合はネットワーク、プロキシ、Hugging Face キャッシュの未完了ファイルを確認します。`download_model.py` でも取得できます。
- 音声生成エラーは `logs/api/audio-latest.log` の最初の traceback を確認します。AMD APU に bf16 を強制している場合は解除します。
- VRAM 不足は **Parallel Workers** と **Max Items/Batch** を下げます。同じ GPU を使う他のプロセスも確認します。
- 428 バイトほどの MP3 は ffmpeg が見つからない、または MP3 をエンコードできない可能性があります。Pinokio または `app/env` で起動し、再生成します。
- 短すぎるクローン参照は声が不安定になりやすいため、ノイズのない十分な長さの音声と正確な文字起こしを使います。
- 文字化けはアップロード時の修復や **Saved Scripts** の修復プレビューを確認します。

詳しくは [Wiki のトラブルシューティング](docs/wiki/Troubleshooting-JA.md)を参照してください。

## 認証と外部からのアクセス

既定では認証なしで `127.0.0.1` にバインドします。トンネル、リバースプロキシなどで外部に公開する場合は HTTP Basic Auth を有効にします。

```bash
export ALEXANDRIA_AUTH_PASSWORD=your-secret
export ALEXANDRIA_AUTH_USERNAME=alexandria
curl -u alexandria:your-secret http://localhost:4200/api/config
```

`GET /api/config` の API キーは伏せられます。別サイトからの変更リクエスト（POST / PUT / PATCH / DELETE）は既定で拒否され、未知の Host も拒否されます。ドメインや別のフロントエンドを使う場合は、必要なホストと Origin を明示します。

```bash
export ALEXANDRIA_ALLOWED_HOSTS=audiobooks.example.com
export ALEXANDRIA_ALLOWED_ORIGINS=https://audiobooks.example.com
```

`ALEXANDRIA_ALLOWED_HOSTS` はアクセス先のホスト名、`ALEXANDRIA_ALLOWED_ORIGINS` は API を呼ぶページの Origin です。[正確な許可条件（英語）](README.md#requests-from-other-sites)も確認してください。HTTP の API は [日本語 API ガイド](docs/wiki/API-Reference-JA.md)、全ルートの一覧は [英語 README](README.md#api-reference)を参照してください。

## 研究と開発

これは [Alexandria](https://github.com/Finrandojin/alexandria-audiobook) の研究フォークです。話者判定のモデル・プロンプト比較、声の類似度、声の一貫性などを測定し、成果と失敗を記録しています。`michel2_full` は 2026-09-19 に製品の既定値となりました。

日本語版は利用ガイドを中心にしています。測定条件や研究中の目標、実験の結果は次の正本を参照してください。ベンチマークの結果は、あなたの本の品質保証ではありません。製品にある操作、研究で得た知見、まだ達成していない目標を区別して読んでください。

- [GOALS.md](GOALS.md)：目標の定義と測定状況。
- [RECIPES.md](RECIPES.md)：学習・推論の設定と対照実験。
- [RESULTS_INDEX.md](RESULTS_INDEX.md)：個別の成果物の索引。
- [研究結果の Wiki（英語）](docs/wiki/Results.md)、[評価手順（英語）](docs/wiki/Evaluation-Recipes.md)。
- [公開アダプター](https://huggingface.co/Om22s/alexandria-qwen3-attribution)と [HF_MODEL_GUIDE.md](HF_MODEL_GUIDE.md)：リリース条件と制約。

## 貢献

PR の宛先は `on22s/alexandria-audiobook2` の `main` です。コミット前に `./ready.sh` を実行します。これは派生ファイルを再生成してリリース検証を行い、新規チェックアウトの Git hooks も設定します。派生ファイルの競合は手作業で統合せず、`.gitattributes` と post-merge hook の再生成経路に従います。測定値と解釈は別々に記述し、GOALS / RECIPES を変更する前に `CLAUDE.md` の規則 19–26 を確認してください。構成と全 API は [英語 README](README.md#project-structure)にあります。

## 謝辞

- [Finrandojin](https://github.com/Finrandojin/alexandria-audiobook)：元の Alexandria。本フォークはそのコードを基盤としています。
- [Ayush Naphade](https://github.com/aayushnaphade)：上流のペルソナ生成、話者の別名解決、文脈付きレビュー、およびフォークからレビューされた UI 修正。
- [buddies](https://github.com/buddies/alexandria-audiobook)（Xiao Zhang）：`instruct_lexicon.py` に移植した行別の指示語彙（MIT）。
- [Darkkingwill](https://github.com/Darkkingwill/alexandria-audiobook)：中止可能な結合と Result の修正（#534 で再実装）。
- [cjdell](https://github.com/cjdell/alexandria-audiobook)：AMD APU の fp32 修正（#573）。
- [XinchaoGou](https://github.com/XinchaoGou/alexandria-audiobook)：OpenAI 推論モデルのリクエスト形式、`env:NAME` の API キー（#574）、外部 TTS プールの着想（#532）。
- モデル：Qwen3-TTS、Qwen3、Muse-Glimmer。
- 注釈データ：Project Dialogism Novel Corpus（Vishnubhotla、Hammond、Hirst）、RiQuA（Papay & Padó）、DraCor。PDNC の注釈には明示されたライセンスがなく、利用許可を申請しています。
- 参照朗読：Kokoro、LJSpeech、Hi-Fi TTS、AISHELL-3。

利用したものとライセンスの詳細は [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)に記載されています。

## ライセンス

本プロジェクトは MIT（[LICENSE](LICENSE)）。Hugging Face の話者判定用アダプターは Apache-2.0 です。学習データの権利は [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)を確認してください。

Qwen3-TTS、Qwen3、Muse-Glimmer-30B は Apache License 2.0、llama.cpp と whisper.cpp は MIT です。ソフトウェアのライセンスが、入力する本や参照録音の権利まで付与するわけではありません。
