[English](BATCH_PROCESSOR_GUIDE.md) | [中文](BATCH_PROCESSOR_GUIDE_CN.md) | 日本語 | [Español](BATCH_PROCESSOR_GUIDE_ES.md)

# Alexandria バッチプロセッサーガイド

複数のオーディオブックのファイルを、コマンド 1 回で順番に処理します。

## このツールの役割（やさしい説明）

バッチプロセッサーは、**複数のオーディオブックファイルに対して一度に** Preparer（`PREPARER_GUIDE_JA.md` を参照）を実行します。Preparer を 1 ファイルずつ手で動かす代わりに、ファイルの一覧（またはファイルが入ったフォルダー）を渡すと、すべてを順番に 1 つずつ処理します。

### 対象となる人

- **オーディオブックが複数**あり、そのすべての学習データセットを作りたい
- オーディオブックが 1 つだけなら**これは不要です**。Preparer を使ってください

### Preparer との関係

バッチプロセッサーは Preparer のラッパーにすぎません。次のことをします。

1. 音声ファイルの一覧を受け取る
2. それぞれに対して Preparer を実行する
3. 上書きしないよう、各出力を一意の名前で保存する
4. すべて終わったらサマリーを表示する

---

## プログラマーでない人向けのクイックスタート

### ステップ 1：ファイルをまとめる

処理したいオーディオブックの WAV ファイルをすべて 1 つのフォルダーに入れます。たとえば：

```
/home/fakemitch/Desktop/my_audiobooks/
  ├── book1.wav
  ├── book2.wav
  └── book3.wav
```

### ステップ 2：ターミナルを開く

- **Windows：** `Win + R` を押し、`cmd` と入力して Enter。
- **Mac：** Spotlight を開き（Cmd + Space）、`terminal` と入力して Enter。
- **Linux：** アプリケーションメニューを開き、「Terminal」を検索します。

### ステップ 3：バッチプロセッサーを実行する

次のコマンドをコピーして貼り付けます（フォルダーのパスは実際のパスに置き換えてください）。

```bash
cd ~/.pinokio/api/alexandria-audiobook.git
./app/env/bin/python alexandria_batch_processor.py \
  --folder "/path/to/your/audiobooks" \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf
```

### ステップ 4：待つ

各ファイルは音声 5 時間あたり約 1 時間かかります。それぞれ 5 時間のオーディオブックが 3 冊なら、合計で約 3 時間です。

### ステップ 5：結果を確認する

終わると、各オーディオブックに `.zip` のデータセットファイルを含む出力フォルダーができます。

---

## 概要

バッチプロセッサーは、複数の音声ファイルを処理待ちのキューに登録できます。各ファイルは 1 つずつ（順番に）処理され、それぞれに完全な注釈付きデータセットを作ります。

**主な機能：**

- コマンド 1 回で複数のファイルを処理
- 順番に処理（1 度に 1 つ、並列のオーバーヘッドなし）
- 出力ファイル名の自動命名（元のファイル名を含む）
- 充実したログと進捗の追跡
- 統計を含む JSON の結果サマリー
- すべてのファイルで GPU を利用
- ファイルごとの所要時間の見積もり

## クイックスタート

3 つの入力方法から 1 つ選びます。

**A. ファイルを個別に並べる**（位置引数、空白区切り）

```bash
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model Qwen2.5-14B-Instruct-Q6_K.gguf
```

**B. フォルダー全体を走査する**（`--folder` フラグを使う。空白が重要）

```bash
python alexandria_batch_processor.py --folder /path/to/audiobooks --model Qwen2.5-14B-Instruct-Q6_K.gguf
```

**C. 両方を混ぜる**（フォルダー＋追加のファイル）

```bash
python alexandria_batch_processor.py extra.wav --folder /path/to/audiobooks --model Qwen2.5-14B-Instruct-Q6_K.gguf
```

> **空白を含むパスは引用符で囲む必要があります。** フォルダーが `/home/you/Desktop/New folder` なら、引用符で囲まないとシェルが 2 つの引数に分割します。
> ```bash
> # WRONG — shell splits at the space
> python alexandria_batch_processor.py --folder /home/you/Desktop/New folder --model model.gguf
>
> # RIGHT — quotes keep the path as one argument
> python alexandria_batch_processor.py --folder "/home/you/Desktop/New folder" --model model.gguf
> ```

> **`--folder` の後の空白を忘れないでください。** `--/home/...`（空白なし）と書くと、argparse はパス全体を未知のフラグ名として扱います。

## 使い方

### 単一ファイル（元のスクリプト）

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```

### 複数ファイル（バッチプロセッサー）

```bash
python alexandria_batch_processor.py \
  audiobook1.wav audiobook2.wav audiobook3.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```

### フォルダーの走査（バッチプロセッサー）

```bash
python alexandria_batch_processor.py \
  --folder "/path/to/audiobooks" \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```

フォルダーのパスに空白がある場合は引用符で囲んでください。

## コマンドラインオプション

### 位置引数

- `audio_files` - 処理する音声ファイル（1 つ以上、必須、空白区切り）

### 任意の引数

- `--folder DIR` - 対応するすべての音声ファイル（`.wav .flac .ogg`）をフォルダーから走査し、名前順に並べる。個別に並べたファイルと組み合わせられる
- `--model PATH` - 主経路の GGUF モデルファイルのパス（必須、推奨：`Qwen2.5-14B-Instruct-Q6_K.gguf`）
- `--fallback-model PATH` - `--model` の読み込みに失敗したときの予備の GGUF モデル（例：`Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf`）
- `--chunk-size SIZE` - チャンクごとの目標の長さ（秒、既定：10.0）
- `--lang CODE` - 文字起こしの言語コード（既定：en）
- `--force` - `alexandria_dataset_<name>.zip` がすでにあってもファイルを再処理する（既定：スキップ）

### ソースに導かれたモード（任意）

有効にすると、バッチプロセッサーはソースに導かれたチャンク分割のフラグを各 Preparer のサブプロセスに転送します。モードの詳しい動作は `PREPARER_GUIDE_JA.md` を参照してください。バッチ全体での要点は、登場人物の名前が（EPUB から）正しい綴りで出て、音声だけの箇所（クレジット、章の導入）が自動で捨てられることです。

- `--source-folder DIR` - ソースの `.epub` か `.txt` のフォルダー。各音声ファイルはベース名でソースと対応付けられます。`audio/Book1.wav` は `<source-folder>/Book1.epub`（優先）か `<source-folder>/Book1.txt` を探します。対応するソースがない音声ファイルは、警告を出して従来の ASR だけのモードで動きます。`--source` とは同時に使えません。
- `--source PATH` - バッチの**すべての**音声ファイルに適用する 1 つのソースファイル。同じ本の複数の録音（別のナレーターなど）を処理するときに便利です。`--source-folder` とは同時に使えません。
- `--source-threshold N` - チャンクを残すための最小の整列比（既定：0.65）。Preparer の `--source-threshold` に転送されます。
- `--keep-unaligned` - 信頼度の低いチャンクを捨てず、ASR のテキストを使う。Preparer の `--keep-unaligned` に転送されます。

バッチの開始時に、どのファイルが対応付けられ、何件が対応付けられなかったかを表示するので、60 時間の音声を処理し始める前に名前の付け間違いに気づけます。

```
├─ Source-guided: matching from /path/to/books/
│   3 matched, 1 no match (legacy ASR-only for those)
│     no match: 'Audiobook4'
├─ Source threshold: 0.65 (strict-drop)
```

## 中断と再開

### 実行の途中で中断する（ゲームをしたい、GPU を空けたいときなど）

いつでも **Ctrl+C** を押すとバッチプロセッサーを止められます。処理中のファイルのチェックポイントは `dataset_temp/` に自動で保存されます。続きをやるときは、まったく同じコマンドを再実行するだけです。追加のフラグは不要です。

```bash
# Stopped mid-run with Ctrl+C — just rerun the same command to resume
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf
```

バッチプロセッサーは次のように動きます。

- 出力の zip がすでにあるファイル（完了したファイル）を**スキップ**する
- 中断したファイルを最後のチェックポイントから**再開**する（同じ作業を繰り返さない）
- まだ始めていないファイルを**新規に処理**する

## 再開機能

バッチプロセッサーは**2 層の再開**に対応します。

**1. ファイル単位のスキップ** — 出力の zip（`alexandria_dataset_<name>.zip`）がすでにあるファイルは、次の実行でスキップされます。

**2. ファイル内の再開** — ファイルが注釈の途中で中断された（Ctrl+C かクラッシュ）場合、バッチプロセッサーは自動で Preparer に `--resume` を渡します。Preparer はチェックポイントを読み、最後に完了したセグメントから続けます。

Preparer の `dataset_temp/` の `.source` マーカーが、別のファイルの途中の作業に誤って再開しないことを保証します。マーカーが現在の音声ファイルと一致しなければ、`dataset_temp/` は消され、処理は最初から始まります。

```bash
# First run - processes all 3 files
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf
# (book1 completes, book2 crashes 50h into annotation)

# Second run:
#  - book1.wav skipped (zip exists)
#  - book2.wav resumes from last checkpoint (continues from segment ~4000)
#  - book3.wav processed fresh
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf

# Force reprocess everything (no skip, no resume)
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf --force
```

## リアルタイムの出力

バッチプロセッサーは主要なスクリプトの出力を**リアルタイムで**流すので、次のことを監視できます。

- 文字起こしの進捗（Wav2Vec2 が処理中のチャンク）
- GPU の使用率のパーセント
- 経過時間と残り時間の見積もりを含む注釈の進捗
- 発生した警告やエラー

処理が止まっているのではないかと何時間も待つ必要はもうありません。

## 例

### オーディオブック 3 冊を処理する

```bash
python alexandria_batch_processor.py \
  book1.wav book2.wav book3.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf
```

### チャンクの長さを指定して処理する

```bash
python alexandria_batch_processor.py \
  book1.wav book2.wav \
  --model model.gguf \
  --chunk-size 15.0
```

### 別の言語で処理する

```bash
python alexandria_batch_processor.py \
  spanish_book.wav french_book.wav \
  --model model.gguf \
  --lang es
```

### ソースに導かれた処理：オーディオブックごとに 1 つの EPUB

各音声ファイルは、ベース名で同じ階層のソースと対応付けられます。`audio/MyHappyMarriage.wav` は `books/MyHappyMarriage.epub` と組になり、以下同様です。対応するソースがない音声ファイルも処理されます。従来の ASR だけのモードになり、起動時に警告が出ます。

```bash
app/env/bin/python alexandria_batch_processor.py \
  --folder /home/fakemitch/Desktop/audiobooks/ \
  --source-folder /home/fakemitch/Desktop/books/ \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf
```

### ソースに導かれた処理：同じ EPUB をバッチの全ファイルに適用する

同じ本の複数の録音（別のナレーター、別の版）がある場合は、`--source-folder` ではなく `--source` を使い、同じソースファイルをすべてに適用します。

```bash
app/env/bin/python alexandria_batch_processor.py \
  narrator_a.wav narrator_b.wav narrator_c.wav \
  --source "/home/fakemitch/Desktop/books/My Happy Marriage - Volume 01.epub" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf
```

### ソースに導かれた処理でしきい値を厳しくする

既定の 0.65 のしきい値が残したい箇所を捨てているなら、緩めます（または `--keep-unaligned` で一切捨てないようにします）。

```bash
# Loosen threshold to 0.55 — keep more borderline chunks
app/env/bin/python alexandria_batch_processor.py \
  book.wav --model model.gguf \
  --source book.epub \
  --source-threshold 0.55

# Or: keep everything (use ASR text for low-confidence chunks)
app/env/bin/python alexandria_batch_processor.py \
  book.wav --model model.gguf \
  --source book.epub \
  --keep-unaligned
```

## 出力ファイル

各入力ファイルについて、バッチプロセッサーは次を作ります。

- `alexandria_dataset_<filename>.zip` - 注釈付きのデータセット
- 衝突を避けるため、`alexandria_dataset.zip` から自動で名前を変える

例：

```
Input:  audiobook1.wav
Output: alexandria_dataset_audiobook1.zip

Input:  audiobook2.wav  
Output: alexandria_dataset_audiobook2.zip
```

## ログ

バッチプロセッサーは `logs/` ディレクトリーに詳細なログを作ります。

- `logs/alexandria_batch_YYYYMMDD_HHMMSS.log` - 処理の完全なログ

## 結果のサマリー

処理が終わると、JSON のサマリーが作られます。

- `batch_results_YYYYMMDD_HHMMSS.json` - 処理の結果

次を含みます。

- ファイルごとの結果（成功／失敗）
- 出力ファイルのパスとサイズ
- ファイルごとの処理時間
- 処理の合計時間
- タイムスタンプ

例：

```json
{
  "timestamp": "2026-05-16T16:30:00",
  "total_time_seconds": 14400,
  "results": {
    "succeeded": [
      {
        "file": "book1.wav",
        "output": "alexandria_dataset_book1.zip",
        "output_size_mb": 250.5,
        "time": "2h 15m",
        "time_seconds": 8100
      }
    ],
    "failed": [],
    "skipped": []
  }
}
```

## 処理の流れ

1. **検証** - すべてのファイルが存在し、有効な音声形式か確認
2. **モデルの確認** - Gemma モデルにアクセスできるか確認
3. **GPU の監視** - 開始時に GPU のメモリーと使用率を記録
4. **順番に処理** - 各ファイルを 1 つずつ処理：
   - 音声を読み込む（16kHz＋24kHz）
   - **Wav2Vec2 で文字起こし**（GPU が主経路、コンテキストを保つため 30 秒の重なりのあるチャンク）
     - 必要なら whisper.cpp Small.en にフォールバック
     - 必要なら WhisperX-CPU にフォールバック
   - **Gemma でチャンクに注釈**（GPU を利用）
     - 10 セグメントごとに GPU のメモリーと使用率を監視
     - 経過時間を時間／分の形式で表示
     - 残り時間をリアルタイムに更新して見積もる
   - 注釈付きの音声セグメントを作る
   - ZIP ファイルにまとめる
   - 衝突を避けるため出力の名前を変える
   - ファイルの間に GPU の統計を記録
5. **サマリー** - バッチの統計を含む処理レポートを生成

## GPU の監視と進捗の追跡

### リアルタイムの監視

- **GPU メモリー**：確保済み／合計のメモリーとその割合を次の時点で追跡：
  - Wav2Vec2 モデルの読み込み後
  - Wav2Vec2 のチャンク処理の前と最中（50 チャンクごと）
  - Gemma モデルの読み込み後
  - 注釈中（10 セグメントごと）
  - バッチ処理中でファイルの間

- **GPU の使用率**：rocm-smi で実際の GPU 負荷の割合を表示
  - GPU が実際に使われているかを確認する助けになる
  - GPU を使っていると言いながら使っていない偽陽性を検出する
  - 例：`Utilization: 45.3%`

### 進捗の追跡

- **時間の形式**：すべての時間の見積もりを時間／分の形式で表示
  - 合計の見積もり：秒数ではなく `~60h 13m`
  - 経過時間：`Elapsed: 7m 40s`
  - 残り時間：`Remaining: ~60h 12m`
  - 10 注釈セグメントごとに更新

### 進捗の出力例

```
GPU Usage (chunk 50/725):
  ├─ Memory: 4.52GB / 16.00GB (28.3%)
  └─ Utilization: 87.5%

Progress: 10/4713 chunks | Elapsed: 7m 40s | Remaining: ~60h 12m
```

## 性能のヒント

- **GPU の利用** - 文字起こしと注釈はすべて GPU を使い、リアルタイムで監視
- **順番に処理** - 並列のオーバーヘッドがなく、各ファイルが GPU をすべて使える
- **ディスクの空き** - オーディオブック 1 冊あたり約 5〜10 GB の空きを確保
- **メモリーの監視** - ファイルの間で GPU のキャッシュを消し、メモリーの使用量を記録

## 所要時間の見積もり

19,500 秒のオーディオブック 1 冊あたり約 5.5 時間を基準に：

- 文字起こし：約 15〜20 分（GPU 利用）
- 注釈：約 4〜5 時間（GPU 利用、1 チャンク約 46 秒）

合計 = オーディオブック 1 冊あたり約 4.5〜5.5 時間

オーディオブック 3 冊なら：合計約 13〜16 時間

## トラブルシューティング

### 「unrecognized arguments」または「--folder path is not a directory」

```
error: unrecognized arguments: --/home/you/Desktop/New
Error: --folder path is not a directory: /home/you/Desktop/New
```

ほとんどはシェルの引用符の問題で、バグではありません。

- **空白がない：** `--/home/...` は `--folder /home/...` であるべき（フラグとパスの間に空白）
- **引用符のない空白：** 空白を含むパスは二重引用符で囲む：`--folder "/home/you/Desktop/New folder"`
- **行末のバックスラッシュと空白：** 複数行のコマンドでは、`\` が行の最後の文字であることを確認する（末尾に空白を置かない）

### ファイルが見つからない

```
Error: File not found: audiobook.wav
```

- ファイルのパスが正しいか確認する
- ファイルが別のディレクトリーにあるなら絶対パスを使う
- 空白を含むパスは引用符で囲む：`"/path with spaces/book.wav"`

### 非対応の形式

```
Warning: Unsupported format: audiobook.aac
```

- 対応形式：`.wav`、`.flac`、`.ogg`（soundfile/libsndfile でネイティブに対応）
- `.mp3` と `.m4a` は確実には対応していません。先に `.wav` へ変換してください：`ffmpeg -i input.mp3 output.wav`

### モデルが見つからない

```
Error: Model file not found
```

- モデルのパスが正しいか確認する
- なければ Gemma-4 モデルをダウンロードする

### GPU が使えない

```
Warning: GPU not available - running on CPU (slower)
```

- CUDA/ROCm のインストールを確認する
- GPU のドライバーが入っているか確認する
- CPU では 1 ファイルにずっと時間がかかる

### 空き容量がない

```
Error: No space left on device
```

- 古いデータセットを片付ける
- ディスクの空きを増やす（1 ファイルあたり約 10GB 必要）
- `du -sh alexandria_dataset*.zip` で古いファイルを確認する

### データセットは完全に見えるが、各オーディオブックのごく一部しかない

`.wav` ファイルがそれぞれ約 4 GiB を超え、数時間のはずの本で Preparer のサマリーが「Total audio in dataset: 1.5 min」のようになっている場合、4 GiB 超の WAV ヘッダーの折り返しの不具合に当たっています。WAV の data-chunk-size の項目は 32 ビットで、折り返しより後のすべてを黙って切り詰めます。Preparer は現在これを自動で検出し、読み込みを ffmpeg 経由にしますが、古いローダーで作ったデータセットは再実行してください。詳しくは PREPARER_GUIDE_JA.md の「大きすぎる WAV」を参照してください。

## 高度な使い方

### フォルダー内のすべてのファイルを処理する

```bash
python alexandria_batch_processor.py \
  --folder /path/to/audiobooks \
  --model model.gguf
```

ファイルは名前順に取り込まれます。対応形式：`.wav .flac .ogg` — soundfile/libsndfile がネイティブに対応します。`.mp3` や `.m4a` のファイルは先に `.wav` へ変換してください（例：`ffmpeg -i input.mp3 output.wav`）。

フォルダーと個別に並べたファイルを混ぜることもできます。

```bash
python alexandria_batch_processor.py extra.wav \
  --folder /path/to/audiobooks \
  --model model.gguf
```

### エラーからの復旧付きで処理する

バッチプロセッサーは自動で次のことをします。

- 開始前にすべてのファイルを検証する
- 1 つのファイルが失敗しても続ける
- どのファイルが成功／失敗したかを報告する
- 結果を JSON に保存する

### 進捗を監視する

```bash
# In another terminal
tail -f logs/alexandria_batch_*.log
```

## 開発メモ

バッチプロセッサーは：

- `alexandria_preparer_rocm_compatible.py` をラップする
- ファイル名の衝突を扱う
- 時間と結果を追跡する
- すべての操作を記録する
- 一時ファイルを片付ける

動作を変えるには、`alexandria_batch_processor.py` を直接編集します。
