[English](PREPARER_GUIDE.md) | [中文](PREPARER_GUIDE_CN.md) | 日本語 | [Español](PREPARER_GUIDE_ES.md)

# Alexandria Preparer ガイド

1 つのオーディオブックを、コマンド 1 回で注釈付きの TTS 学習データセットに変換します。

## 概要

Preparer は音声ファイル（オーディオブック）を受け取り、次を含む ZIP アーカイブを作ります。

- 単語単位で整列した音声セグメント（24 kHz の `sample_NNNN.wav`）
- テキストの注釈、長さ、時間範囲を含む `metadata.jsonl`
- TTS の学習データセットとしてそのまま使える形

# Alexandria Preparer ガイド

## このツールの役割（とても簡単な説明）

Preparer はオーディオブック（音声ファイル）を、Alexandria の TTS エンジン用の学習データセットに変換します。やることは 3 つです。

1. **音声を聴いて**、すべての単語を正確なタイムスタンプ付きで書き起こす（「文字起こし」または「ASR」と呼ばれます）
2. **任意で、単語を元の本と照合する**ので、テキストは AI が聞き間違えたものではなく本の綴りになる
3. **音声を小さなチャンクに切って**（各約 10 秒）、メタデータファイルとともに WAV ファイルとして保存する

出力は `.zip` ファイルで、Alexandria の LoRA Training タブに入れて、TTS エンジンに新しい声を教えられます。

### 対象となる人

- **オーディオブック（WAV ファイル）**があり、声をクローンしたい
- Training タブで**カスタムボイスを学習したい**
- 本からオーディオブックを生成するために Alexandria を使うだけなら、**これは不要です**。Web UI がすべて自動で行います

---

## プログラマーでない人向けのクイックスタート

### 準備

Preparer を実行する前に、次を確認してください。

1. **Pinokio がインストールされていて**、Pinokio に Alexandria が入っている
2. **オーディオブックの WAV ファイルがある** — MP3 なら先に WAV に変換します（オンラインの変換サービスか Audacity を使えます）
3. **元の本のファイル（.epub か .txt）がある** — 任意ですが、良い結果のために強く推奨します

### ステップ 1：ターミナルを開く

- **Windows：** `Win + R` を押し、`cmd` と入力して Enter。
- **Mac：** Spotlight を開き（Cmd + Space）、`terminal` と入力して Enter。
- **Linux：** アプリケーションメニューを開き、「Terminal」を検索します。

### ステップ 2：Alexandria のフォルダーへ移動する

次のコマンドを入力して Enter を押します。

```bash
cd ~/.pinokio/api/alexandria-audiobook.git
```

Alexandria を別の場所に入れた場合は、そのパスを使ってください。

### ステップ 3：Preparer を実行する

次のコマンドをコピーして貼り付けます（パスは実際のファイルのパスに置き換えてください）。

```bash
./app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "path/to/your/audiobook.wav" \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --source "path/to/your/book.epub"
```

**各部分の意味：**

- `./app/env/bin/python` — Alexandria に付属する Python（システムの Python は使わないでください）
- `--audio` — オーディオブックの WAV ファイルへのパス
- `--model` — 各チャンクに話者と演技指示を付ける AI モデル
- `--source` — 元の本のファイル（任意ですが、精度が上がります）

### ステップ 4：待つ

Preparer は次の処理をします。

1. 音声を文字起こしする（音声 5 時間あたり約 1 時間）
2. 単語を本と照合する（`--source` を指定した場合）
3. チャンクに切って ZIP を保存する

終わると次のように表示されます。

```
✓ Dataset written to: alexandria_dataset.zip
```

### ステップ 5：出力を使う

1. `alexandria_dataset.zip` を展開します。WAV ファイルと `metadata.jsonl` が入っています。
2. Alexandria の Web UI で **Dataset** タブを開きます。
3. `metadata.jsonl` と WAV ファイルをアップロードします。
4. **Training** タブでカスタムボイスの学習に使います。

---

## このツールの役割（とても簡単な説明）

長いオーディオブックがあるとします。誰かが小説全体を声に出して読んでいて、60 分か 6 時間かもしれません。コンピューターにそのナレーターのように話す方法を教えるには、その人が話している**短いクリップ**がたくさん必要で、それぞれに**実際に話した正確な単語**のラベルが付いている必要があります。このツールがそのクリップを自動で作ります。

次のように考えてください。

1. **オーディオブック（1 つの大きな音声ファイル）を渡します。**
2. **ツールが聴いて**、すべての単語がどこで始まりどこで終わるかを割り出します。
3. **オーディオブックを約 10 秒の一口サイズのクリップに刻みます。** 自然な間（文の終わりなど）で切るようにして、クリップが単語の途中で終わらないようにします。
4. **各クリップについて、そこに話された単語を書き出します。**
5. **小さな AI アシスタントが単語にちょっとした手がかりを加えます。** *重要な*単語に印を付けたり、ナレーターが長く間を置いた場所に `...` を入れたりします。
6. **クリップと、各クリップが何を言っているかの一覧が入った zip ファイルが返ります。**

この zip を後で TTS 学習プログラムに渡し、コンピューターがそのナレーターの声を真似られるようにします。

### 大きな新しい工夫：本を使う

ツールに**実際の本**（EPUB かテキストファイル）を渡すこともできます。

なぜか。聴き取りの AI（音声を単語にする部分）は間違えるからです。とくに登場人物の名前で。ナレーターが「**Yurie**」と言ったのに「**Yudy**」と書いたり、「**Kudou**」が「**Coodo**」になったりします。

本を渡すと、ツールは**クリップごとに正しい綴りを調べられます**。宿題の横に答えがあるようなものです。聴き取りの AI が音声を聞き、ツールが本を照合して「ああ、この部分は『Miyo Saimori』と書いてある。推測したものではなくこの綴りを使おう」と判断します。

さらに、**本にないクリップを捨てる**こともできます。ナレーターが「Audible presents…」と言ったり、章のアナウンスを声に出して読んだり、物語に一致しない部分を除けます。

## 最小限の例（コピー＆ペースト）

### とにかくオーディオブックをクリップにする（本のファイルなし）

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "/home/fakemitch/Desktop/audiobook.wav" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf
```

これがやること（やさしい言葉で）：

- `/home/fakemitch/Desktop/audiobook.wav` のオーディオブックを読みます。
- `models/Qwen2.5-14B-Instruct-Q6_K.gguf` の AI ヘルパーでクリップにラベルを付けます。
- 結果を現在のフォルダーに `alexandria_dataset.zip` として保存します。

### 同じことを本も使ってやる（推奨）

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "/home/fakemitch/Desktop/audiobook.wav" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf \
  --source "/home/fakemitch/Desktop/books/My Happy Marriage - Volume 01.epub"
```

違いは最後の行だけです。本の場所をツールに伝えています。名前は正しい綴りで出てきて、音声だけの部分（クレジット、導入）は捨てられます。

### 「本は渡したいけれど、何も捨てないでほしい」

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "/home/fakemitch/Desktop/audiobook.wav" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf \
  --source "/home/fakemitch/Desktop/books/book.epub" \
  --keep-unaligned
```

`--keep-unaligned` は「クリップが本と一致しなくてもかまわない。聴き取りの AI の最善の推測を使って、そのクリップは残す」という意味です。

### 「どこまで進んだか分からなくなった。やり直したい」

`--resume` を使わないだけです。すべての実行は既定で最初から始まります。前回の試行が残した不要なファイルは自動で片付けられます。

### 「3 時間でクラッシュした。止まったところから再開したい」

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "/home/fakemitch/Desktop/audiobook.wav" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf \
  --resume
```

同じコマンドで、末尾に `--resume` を足すだけです。すでに作ったクリップを飛ばし、止まったところから続けます（最初に `--source` を使ったなら、ここでも付けてください。元の実行と一致させる必要があります）。

## どのくらい時間がかかるか

AMD Radeon RX 9070 XT でのおおよその目安です。

- **音声を聴く**（音を単語にする）：オーディオブック 1 時間あたり約 1 分。
- **クリップにラベルを付ける**（遅い部分）：オーディオブック 1 時間あたり約 10 時間。典型的な 6 時間のオーディオブックなら、コンピューター時間で約 60 時間です。一晩動かしておけます。GPU を返してほしいときは **Ctrl+C** で止め、後で `--resume` を使ってください。



**主な機能：**

- **Wav2Vec2 ASR（主経路）**と、真の CTC 整列による単語タイムスタンプ
- whisper.cpp Small.en / WhisperX-CPU へのフォールバック
- **LLM 前処理（Enrichment）** — ローカルの GGUF LLM（Gemma-4-E2B など）を使い、文字起こしのチャンクに話者判定、ナレーションのスタイル、感情のトーンのメタデータを加える任意の段階
- GPU 上での Gemma 4 / Qwen LLM による注釈
- **ソースに導かれたチャンク分割**（`--source`）：ASR を EPUB/TXT と整列し、チャンクのテキストにソースの綴りを使い、音声だけの要素を捨てる
- **間を意識したチャンクの境界**：チャンクは各チャンクの最後の 30% にある文の句読点か自然なナレーターの間で終わり、長さのしきい値に達した場所で終わるわけではない
- **3 段階のパイプライン**（ASR → Enrich → Annotate）と、ROCm の安全のためのサブプロセス分離
- 再開機能 — クラッシュで作業を失わない
- 50 セグメントごとの定期的なチェックポイント
- 実際に測定したスループットに基づく動的な ETA
- 音声の読み込みは 1 回だけ（メモリー内でリサンプリング）
- 注釈中は 24 kHz の音声をディスクに退避（長いオーディオブックで約 1.8 GB の RAM を節約）
- `rocm-smi` による GPU メモリーと使用率の記録

## 推奨モデル

Preparer は LLM を使ってテキストのチャンクに TTS 用の注釈を付けます。AMD の 16 GB 以上の GPU 向けの推奨です。

| 役割 | モデル | VRAM (Q6_K) | 理由 |
|---|---|---|---|
| **主経路** | `Qwen2.5-14B-Instruct-Q6_K.gguf` | 約 12 GB | 指示への追従が強く、構造化された簡潔な出力 |
| **フォールバック** | `Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf` | 約 5 GB | Qwen が読み込めない場合の予備 |

## 使い方

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```

### LLM の enrichment あり（登場人物の多い本に推奨）

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --enrich-with-llm \
  --llm-model-path /path/to/gemma-4-E2B-it-Uncensored-MAX.BF16.gguf \
  --enrich-speaker-attribution --enrich-narration-style --enrich-emotional-tone
```

これは**3 段階のパイプライン全体**（ASR → Enrich → Annotate）を実行します。enrichment の段階では、小さなローカルの LLM が、注釈の前にすべての文字起こしチャンクへ `speaker_attribution`、`narration_style`、`emotional_tone` のメタデータを加えます。

## コマンドラインオプション

### 必須

- `--audio PATH` — 入力の音声ファイル（.wav、.mp3、.m4a、.flac、.ogg）
- `--model PATH` — 主経路の GGUF モデルのパス（必須）

### 任意

- `--fallback-model PATH` — `--model` の読み込みに失敗したときに使う予備の GGUF モデル
- `--chunk-size SECONDS` — チャンクごとの目標の長さ（秒、既定：`10.0`）
- `--lang CODE` — 文字起こしの言語コード（既定：`en`）
- `--output PATH` — 出力する ZIP のパス（既定：`alexandria_dataset.zip`）
- `--resume` — 最初からやり直さず、既存の `dataset_temp/` から再開する
- `--skip-annotation` — 予約済み。文字起こしだけの書き出しが未実装のため、現在は拒否されます

### LLM 前処理（Enrichment）

ローカルの GGUF LLM を使い、注釈の前に文字起こしのチャンクへメタデータを加えます。

- `--enrich-with-llm` — enrichment の段階を有効にする（ASR と注釈の間に実行）。下の 3 つのカテゴリーフラグのうち少なくとも 1 つが必要で、どれも選ばなければコマンドはエラーで終了します。
- `--llm-model-path PATH` — enrichment 用の GGUF LLM モデルファイルのパス（`--enrich-with-llm` を指定すると必須。Gemma-4-E2B などの小さなモデルを推奨）
- `--enrich-speaker-attribution` — LLM に話者判定を抽出させる（例：「主人公」「ナレーター」「脇役」）
- `--enrich-narration-style` — LLM にナレーションのスタイルを抽出させる（例：「穏やか」「元気」「悲しい」「疑問」）
- `--enrich-emotional-tone` — LLM に感情のトーンを抽出させる（例：「嬉しい」「不安」「中立」「興奮」）

### 話者のダイアライゼーション（任意）

pyannote.audio で、文字起こしした単語をダイアライゼーションした話者に割り当てます。Install が作る分離された AMD Linux/ROCm 用の `preparer_env`（`requirements-diarization.txt` を含む）と、`pyannote/speaker-diarization-community-1` の利用規約に同意した Hugging Face トークンが必要です。

- `--diarize` — 常に音声全体に対して完全な話者のダイアライゼーションを実行する
- `--auto-detect-speakers` — まず安価な抽出による事前チェック（120 秒のウィンドウ 3 つ、約 2〜3 分）を行い、あるウィンドウでそのウィンドウの発話時間の 10% 以上を 2 人目の話者が占めている場合にだけ、完全で高価なダイアライゼーションを実行します。15 分未満の音声や、事前チェックを実行できない場合はスキップします（完全なダイアライゼーションにフォールバック）。明示的な `--diarize` がこのフラグより優先されます。
- `--hf-token TOKEN` — Hugging Face のトークン（既定：`HF_TOKEN` 環境変数）。上のどちらのフラグにも必要
- リポジトリーのルートで `python3 env_doctor.py` を実行すると、これから Preparer を動かすインタープリターに `pyannote.audio` が入っているか確認できます。

### ソースに導かれたチャンク分割（任意）

オーディオブックに対応する EPUB かテキストファイルを渡すと、ASR の誤変換をソースで直し、LLM の段階の前に音声だけの要素を捨てます。

- `--source PATH` — 音声が朗読しているのと同じ本の EPUB か TXT
- `--source-threshold N` — チャンクを残すための最小の整列比（既定：`0.65`）
- `--keep-unaligned` — チャンクが `--source-threshold` を下回ったとき、捨てずに ASR のテキストを書く（既定：捨てる）
- `--source-start N` — ソースの N 番目の単語から整列を始める（自動アンカーを飛ばす）
- `--source-start-text TEXT` — ソース内で TEXT をあいまい検索してそこから始める
- `--no-auto-anchor` — ソースの自動アンカー検出を無効にする（0 番目の単語から始める）

## 例

### 標準的な実行（Qwen が主、Gemma が予備）

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf
```

### 出力先の指定

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio book1.wav \
  --model model.gguf \
  --output datasets/book1.zip
```

### 中断と再開（ゲームをしたい、GPU を空けたいときなど）

いつでも **Ctrl+C** を押すとスクリプトを止められます。進捗は自動で保存されます。

```
⚠ Process interrupted by user
Partial results preserved in dataset_temp/ - rerun with --resume to continue
```

続きをやるときは、同じ引数に `--resume` を足して再実行します。

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model model.gguf \
  --resume
```

Preparer は止まったところから正確に再開し、同じ作業を繰り返しません。

### クラッシュ後の再開

60 時間の注釈の 50 時間目でスクリプトがクラッシュしても、復旧は同じです。`--resume` を付けて再実行します。

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model model.gguf \
  --resume
```

Preparer は次の処理をします。

1. 既存の `dataset_temp/metadata.jsonl` を読む
2. 最後に完了したセグメントとその終了時刻を見つける
3. すでに処理した音声を飛ばす
4. その地点から新しいセグメントで続ける

### 別の言語

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio spanish_book.wav \
  --model model.gguf \
  --lang es
```

### より大きなチャンク

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model model.gguf \
  --chunk-size 15.0
```

## ソースに導かれたチャンク分割

`--source <epub-or-txt>` を渡すと、Preparer は各 ASR チャンクをソーステキストとあいまいに整列し、次のどちらかを行います。

- 整列比が `--source-threshold`（既定 0.65）を満たすとき、チャンクの ASR テキストを**ソースの綴りに置き換える**。LLM はすでに正しいテキストに注釈を付けます。登場人物名、方言の綴り、句読点は EPUB のとおりに出ます。
- 整列がしきい値を下回るとき、チャンクを**完全に捨てる**。WAV ファイルも JSONL の項目も作りません。これが音声だけの要素を除きます。ナレーターの導入、「このオーディオブックはフィクションです」というクレジット、テキストにない章のアナウンスなどです。

信頼度の低いチャンクを（ASR のテキストで）残したい場合は `--keep-unaligned` を渡します。そうしたチャンクではカーソルが進まないので、次のチャンクは散文が最後に一致したと分かっている位置から整列を試みます。

### 例

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --source "/home/fakemitch/Desktop/books/My Happy Marriage - Volume 01.epub"
```

### ログに表示されるもの

起動時に、Preparer はソースの読み込みと語彙の構築を報告します。

```
▶ Loading source for guided chunking: /path/to/book.epub
  ├─ Source: 284,125 characters
  ├─ 29 recurring proper nouns (gifted, godou, grotesqueries, hana, kanoko, kaya +23 more)
  ├─ 49,686 source words
  └─ Auto-anchor: entry 3 → source word 287 (62.1% match)
```

注釈中に捨てたチャンクごとに：

```
↪ DROPPED chunk at 421.36s (source ratio 0.42 < 0.65)
```

信頼度が低く保持したチャンクごとに（`--keep-unaligned` のときだけ）：

```
↪ chunk 42 kept (ASR text); source ratio 0.51 < 0.65
```

### 誤ったソースファイルの検出

LLM の注釈が始まる前に、Preparer は最初の約 30 個の ASR チャンクをソースと照合して事前に走査します。平均の整列が 50% 未満、または 40% を超えるチャンクが 60% を下回る場合、Preparer は一致しないソースに何時間もの LLM 時間を費やすのではなく、**エラーで中止します**。

```
⚠ Source/audio divergence too high to proceed:
  Sampled 30 chunks — avg alignment 38%, 21 (70%) below 60%.
  Usually means a wrong edition or different translation.
  Re-run without --source, or pass --keep-unaligned to accept the ASR
  text for low-confidence chunks.
```

これは通常、間違った EPUB（別の翻訳、短縮版（抄訳）、まったく別の本）を渡したという意味です。`--source-start N`／`--source-start-text "..."` で手動で再アンカーするか、正しいソースに差し替えるか、`--source` なしで実行して従来の ASR だけの流れにフォールバックしてください。

### 使うべきときと飛ばすべきとき

**`--source` を使うのは：** オーディオブックに対応する EPUB/TXT があり、学習データセットにきれいな登場人物名と方言の綴りが欲しく、ソースに現れないチャンク（章の導入、ナレーターの注記、本文以外の要素）を捨ててもよい場合です。

**`--source` を飛ばすのは：** ソーステキストがない場合、または処理する音声がソースにゆるく基づくと分かっている場合（翻案、書き直し、短縮版）で、厳格な削除が残したい内容を失う場合です。

### 後段の compare-review への影響

ソースに導かれたモードは、`alexandria_compare.py` で結果の JSONL をレビューするときに目にするものを大幅に減らします。利用者がよく行う修正（登場人物名の ASR の修正、方言の綴りの修正）の多くは、準備の時点ですでに適用されています。compare-review は韻律の健全性チェックになり、通常は本 1 冊あたり 1 時間ではなく数分で済みます。

## 間を意識したチャンクの境界

チャンカーは、積み上がった長さが `--chunk-size` に達するとチャンクを出しますが、まさにその単語で切るわけではありません。チャンクの長さの最後の 30% を振り返り、次の場所で切ることを優先します。

1. 文の句読点（`.`、`!`、`?`）で終わる単語 — TTS のつながりにとって最良。ウィンドウ内で最も**後ろ**のものを選びます。
2. 同じウィンドウ内で最も長い自然な間（ASR の単語の終わりと次の単語の始まりの間が 0.25 秒以上）。
3. 最後の単語（しきい値の時点の現在の単語） — 上のどちらも使えないときだけ。

振り返りで切り落とされた単語は次のチャンクの種になります。音声は失われません。結果として、チャンクはフレーズの途中ではなく、息継ぎ、節、文の区切りで終わります。`--source` モードと組み合わせれば、できた WAV はほとんど手を加えずに TTS の学習に使えます。

## 処理の流れ

1. **入力の検証** — 音声ファイルとモデルが存在するか確認
2. **音声の読み込み** — 元のサンプルレートで 1 回だけ読み、16 kHz（ASR 用）と 24 kHz（出力用）にリサンプリング
3. **24 kHz をディスクへ退避** — `.alexandria_audio_24k.wav` を書き、RAM を解放
4. **音声の文字起こし**（Wav2Vec2 → IFW → WhisperX-CPU）：
   - Wav2Vec2 は音声を 30 秒のチャンクに 3 秒の重なりで処理
   - 各チャンクは真の CTC フレーム整列のために `output_word_offsets=True` でデコード
   - 重なりの領域の単語は単語の中心の所有権で重複を除く
5. **チャンクへの注釈**：
   - Gemma 4 モデルを GPU に完全に読み込む（`n_gpu_layers=-1`）
   - 単語のセグメントをたどり、約 10 秒のチャンクに積み上げる
   - 3 セグメントのコンテキストウィンドウで、チャンクごとに TTS の注釈を生成
   - 各 `sample_NNNN.wav` を書き、`metadata.jsonl` にすぐ追記（クラッシュに強い）
   - 10 セグメントごとに動的な ETA を記録
6. **出力データセットの作成** — `dataset_temp/` の中身を出力パスの ZIP にする
7. **後片付け** — 一時音声と一時ディレクトリーを削除（成功時のみ）

## 再開機能

Preparer は各セグメントのメタデータを、処理の直後に（終わりまでためずに）`dataset_temp/metadata.jsonl` に書きます。各項目は次を含みます。

```json
{
  "audio_filepath": "sample_0042.wav",
  "text": "annotated text...",
  "duration": 10.04,
  "start": 421.36,
  "end": 431.40
}
```

`--resume` のときは：

- 既存の項目を読み込みます
- 最も大きい `end` の時刻が再開の地点になります
- `start < resume_time` の単語は飛ばします
- 新しい項目を追記します（既存の項目は変更しません）
- 最後の 5 項目のテキストが LLM のコンテキストになります

**意図的な中断（Ctrl+C）やクラッシュのとき**：`dataset_temp/` は保持されます（削除されません）。`--resume` と、元と同じ `--audio` と `--model` の引数で再実行すると、止まったところから続きます。

**成功したとき**：ZIP を作った後に `dataset_temp/` は削除されます。

### ソースファイルの安全チェック

処理中の音声ファイルの絶対パスを含む `.source` マーカーが `dataset_temp/` に書かれます。`--resume` のときは：

- マーカーが現在の `--audio` と一致する → 安全に再開。
- マーカーがないか別のファイルを指している → **`dataset_temp/` を消して最初からやり直す**（警告として記録）。

これにより、別のオーディオブックで作業ディレクトリーを再利用したときの偶発的な破損を防ぎます。バッチプロセッサーはこの保証を前提に、再試行のたびに安全に `--resume` を渡します。

## GPU の監視

Preparer は要所で GPU の統計を記録します。

- Wav2Vec2 モデルの読み込み後
- チャンク処理の前と最中（50 チャンクごと）
- Gemma モデルの読み込み後
- 注釈中（10 セグメントごと）

各ログの項目は次を表示します。

```
GPU Usage (chunk 50/725):
  ├─ Memory: 4.52 GB / 17.10 GB (26.4%)
  └─ Utilization: 87.5%
```

メモリーは `torch.cuda.memory_allocated()` による値です。使用率は `rocm-smi --showuse --json`（AMD GPU）による値です。注意：Gemma だけが GPU を使っている場合、llama-cpp-python は PyTorch の外で GPU メモリーを管理するため、`torch.cuda.memory_allocated()` は 0 を返します。実際の状況は `rocm-smi` で確認してください。

## 進捗の追跡

パイプラインは **3 つの段階**で動き、ROCm の分離のためにそれぞれ別のサブプロセスとして起動します。

### 段階 1：Wav2Vec2（ASR）

```
↳ Chunk 50/725 | avg 0.51s/chunk | ETA 5m 47s
```

### 段階 2：LLM の enrichment（任意、`--enrich-with-llm` が必要）

```
▶ Created 72 chunks for LLM enrichment
▶ Running LLM enrichment: python llm_enricher.py --model-path ... --input-file ... --output-file ...
Enriching chunk: 10.00s - 20.14s
...
✓ LLM Enrichment Phase completed successfully.
```

### 段階 3：注釈

```
↳ Progress: 50/4713 chunks | Avg: 46.2s/chunk | Elapsed: 38m 42s | ETA: 59h 31m
```

時間の形式は適応します。1 時間を超えるなら `Nh Mm`、1 分を超えるなら `Mm Ss`、それ以外は `Ns` です。

## ログ

詳細なログは `logs/alexandria_preparer_YYYYMMDD_HHMMSS.log` に書かれます。

- すべての手順の完全な DEBUG レベルの記録
- GPU の統計のスナップショット
- サブプロセスのコマンドラインと出力
- 例外のトレースバック（あれば）

別のターミナルでログを追うには：

```bash
tail -f logs/alexandria_preparer_*.log
```

## ASR の方式の優先順位

| # | 方式 | デバイス | コンテキスト | 単語のタイムスタンプ |
|---|---|---|---|---|
| 1 | **Wav2Vec2**（主経路） | GPU | 30 秒のチャンク＋3 秒の重なり | 真（CTC フレーム整列） |
| 2 | whisper.cpp Small.en | ネイティブ GPU/CPU | 連続 | ネイティブ。長さ 0 の単語は保守的にまとめる |
| 3 | WhisperX-CPU | CPU | 該当なし | 強制アラインメント（forced alignment）で整列 |

Wav2Vec2 が最初に選ばれるのは、重なりのあるチャンクでコンテキストを保ち、CTC で整列した単語ごとのタイムスタンプが出るためです。利用できない場合や、出力がタイムスタンプの検証に失敗した場合、Preparer は `install.js` が入れる固定版の whisper.cpp Small.en を使います。WhisperX は最後の CPU のフォールバックです。Small.en は意図的に英語専用なので、英語以外の要求では黙って間違ったモデルを使うのではなく、これを飛ばします。

## 性能のヒント

- **GPU メモリー** — Gemma は約 8 GB、Wav2Vec2 は約 2 GB を使います。どちらも 16 GB 以上の GPU に余裕で収まります。
- **RAM** — 24 kHz の音声をディスクに退避すると、数時間に及ぶ注釈の段階で約 1.8 GB を節約します。
- **ディスクの空き** — オーディオブック 1 冊あたり約 5〜10 GB の空きを確保します（一時 WAV、セグメントの WAV、最終の ZIP）。
- **実行時間** — AMD Radeon RX 9070 XT では、約 5.4 時間のオーディオブックを 10 秒のチャンクで処理すると、Wav2Vec2 が約 6 分、Gemma の注釈が約 60 時間の見込みです。

## トラブルシューティング

### 音声ファイルが見つからない

```
Error: Audio file not found: audiobook.wav
```

- パスが正しいか確認します。スクリプトの CWD は、起動した場所です。

### モデルファイルが見つからない

```
Error: Model file not found: ...
```

- Gemma の GGUF モデルをダウンロードするか（`download_model.py` など）、`--model PATH` で指定します。

### `rocm-smi unavailable`

- `/opt/rocm/bin/rocm-smi` が存在し、実行できるか確認します。
- それがなくても GPU メモリーは動きます。使えないのは使用率の % だけです。

### `expandable_segments not supported`

- PyTorch からの情報提供の警告です。AMD ROCm はそのメモリー最適化に対応していません。PyTorch は標準の割り当てにフォールバックします。対応は不要です。

### 再開で間違ったファイルを選んだ

- Preparer は `dataset_temp/.source` マーカーでソースを追跡します。マーカーが一致しないと、新しい処理の前に `dataset_temp/` が自動で消されます（警告付き）。
- それでも強制的に最初からやりたい場合は、`dataset_temp/` を手動で削除するか、`--resume` を省きます。

### ディスクの空きがない

- `du -sh dataset_temp/ .alexandria_audio_24k.wav` で確認します。
- 空きを増やしてから、`--resume` で再実行します。

### 大きすぎる WAV（4 GiB 超） — ヘッダーがごく短い長さを報告する

症状：数 GB のオーディオブックの WAV が、数時間ではなく数分の長さを報告します。起動ログの例：

```
[WARNING] ⚠ Oversized WAV detected: header says 135.3s (2.3 min) but file size
implies 24477.2s (6.80 hr). WAV data-chunk-size field is 32-bit and has
wrapped — ffmpeg `-ignore_length 1` will be used to read the full audio.
```

起きる理由：標準の WAV は `data` チャンクのサイズに 32 ビットの符号なしの項目を使うため、4 GiB でオーバーフローします。44.1 kHz ステレオの PCM_16 では約 6.8 時間でそこに達するので、RF64/WAV64 に切り替えていないツールが作ったフルレングスのオーディオブックの WAV は、ヘッダーが折り返しています。`soundfile` と `librosa.load` はその偽のヘッダーに従い、黙って音声の `filesize mod 4 GiB` まで切り詰めます。大きすぎるファイルでの「完了した」実行は、実際には最初の切片だけです（たとえば 6.8 時間のファイルの 2 分、27 時間のファイルの 22 分）。

Preparer の対応：`validate_inputs` がファイルサイズとヘッダーが示すバイト数を比べ、食い違うと上の警告を出して、読み込みを ffmpeg（`-ignore_length 1`）経由にします。24 kHz の一時ファイルは直接ディスクにストリームされ、16 kHz の ASR 用バッファーは float32 の numpy 配列にパイプされます。元のレートの中間ファイルは作らないので、音声の長さにかかわらずピークの RAM は抑えられます。

通常は何もする必要がありません。Preparer が折り返しを検出すれば自動で直ります。重複した ffmpeg のデコードを避け、恒久的に直したい場合は、WAV を一度 FLAC か RF64-WAV に再エンコードします。たとえば：

```
ffmpeg -ignore_length 1 -i input.wav -c:a flac output.flac
```

## API（プログラムからの呼び出し）

### Python（サブプロセス）

```python
import subprocess

result = subprocess.run([
    "python", "alexandria_preparer_rocm_compatible.py",
    "--audio", "audiobook.wav",
    "--model", "Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf",
    "--chunk-size", "10.0",
    "--lang", "en",
    "--output", "my_dataset.zip",
], check=True)
```

### JavaScript（Node.js）

```javascript
const { execFile } = require("child_process");

execFile("python", [
  "alexandria_preparer_rocm_compatible.py",
  "--audio", "audiobook.wav",
  "--model", "model.gguf",
  "--output", "my_dataset.zip",
], { maxBuffer: 1024 * 1024 * 100 }, (err, stdout, stderr) => {
  if (err) throw err;
  console.log(stdout);
});
```

### Curl（該当なし）

Preparer は CLI ツールであり、HTTP サービスではありません。ネットワークから使いたい場合は Flask/FastAPI のサーバーで包んでください。

## 関連項目

- `BATCH_PROCESSOR_GUIDE_JA.md` — 複数のオーディオブックを順に処理し、ファイルをまたいで再開する
- `../../README.md` — プロジェクトの概要
