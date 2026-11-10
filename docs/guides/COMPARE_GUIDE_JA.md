[English](COMPARE_GUIDE.md) | [中文](COMPARE_GUIDE_CN.md) | 日本語 | [Español](COMPARE_GUIDE_ES.md)

# Alexandria Compare ガイド

`metadata.jsonl` の文字起こしを、元の EPUB やテキストファイルと比較します。すべての変更にはあなたの承認が必要で、自動では何も書き込まれません。

## このツールの役割（やさしい説明）

Preparer がオーディオブックを聴いて聞こえたとおりに書き起こすと、ときどき言葉を間違えます。とくに名前、珍しい単語、似た響きの単語です。Compare ツールは、**AI が聞き取った内容を本の実際の記述と照らし合わせ**、間違いを 1 つずつ直すためのものです。

### 対象となる人

- **Preparer を実行して** `metadata.jsonl` を手に入れた。
- オーディオブックの元になった**本（.epub か .txt）**がある。
- 学習にデータを使う前に**文字起こしの誤りを直したい**。

### なぜ必要か

AI が「Sherlock」を「sure lock」と聞き間違えたら、TTS の学習は間違った単語を覚えてしまいます。こうした誤りを直すと、学習した声の精度が上がります。

---

## プログラマーでない人向けのクイックスタート

### ステップ 1：データセットを展開する

Preparer の出力が `.zip` ファイルなら、まず展開します。

```bash
unzip alexandria_dataset.zip -d my_dataset/
```

これで、`metadata.jsonl` と多数の `sample_NNNN.wav` を含むフォルダーができます。

### ステップ 2：ターミナルを開く

- **Windows：** `Win + R` を押し、`cmd` と入力して Enter。
- **Mac：** Spotlight を開き（Cmd + Space）、`terminal` と入力して Enter。
- **Linux：** アプリケーションメニューを開き、「Terminal」を検索します。

### ステップ 3：Compare ツールを実行する

次のコマンドをコピーして貼り付けます（パスは実際のものに置き換えてください）。

```bash
cd ~/.pinokio/api/alexandria-audiobook.git
./app/env/bin/python alexandria_compare.py \
  --jsonl /path/to/my_dataset/metadata.jsonl \
  --source /path/to/original/book.epub
```

### ステップ 4：誤りを確認して直す

ツールは、AI の文字起こしが本と異なるチャンクを 1 つずつ表示します。たとえば次のように出ます。

```
[ANNOTATED] sure lock looked at the clock
[ORIGINAL]  Sherlock looked at the clock
[a]ccept [m]erge [k]eep [e]dit [s]kip [q]uit
```

**各キーの動作：**

- **`a`（accept）：** AI が正しい。AI の版をそのまま使う
- **`m`（merge）：** 両方の版を合わせる。本の正しい綴りと、AI が付けた演技の指示の両方を残せるので、通常はこれが最良の選択
- **`k`（keep）：** AI の版を変えずに残す
- **`e`（edit）：** 自分で修正を入力する
- **`s`（skip）：** 今は飛ばす（後で戻れる）
- **`q`（quit）：** 進捗を保存して終了する

**コツ：** ほとんどの場合 **`m`（merge）** を押してください。本の正しい単語を残しつつ、AI が付けた演技の指示（`*whispered*` や `*excitedly*` など）も保てます。

### ステップ 5：修正済みのデータを使う

Compare ツールが終わると、修正済みの `metadata.jsonl` が保存されます。声を学習するときは、元のファイルではなくこの修正済みファイルを使ってください。

---

## 概要

Compare ツールは次の処理を行います。

1. JSONL と元のソーステキストを読み込みます。
2. 約 10 秒の各チャンクを、ソースの対応する箇所とあいまいに整列します。
3. すでによく一致する項目を**自動承認**します（しきい値は設定可能）。
4. 異なる項目を**表示して**、あなたの判断を待ちます。
5. 承認した変更だけを含む修正済み JSONL を書き出します。

## 依存関係

プレーンテキストのソースには追加のパッケージは不要です。EPUB の対応には次が必要です。

```bash
uv pip install ebooklib beautifulsoup4
# or
pip install ebooklib beautifulsoup4
```

Compare スクリプトは、ソースの読み込み、あいまいな整列、固有名詞の語彙のために `alexandria_alignment.py`（同じディレクトリーにある兄弟モジュール）も import します。2 つのファイルは隣り合ったままにしてください。

## Preparer で `--source` を使いましたか？

この JSONL を `alexandria_preparer_rocm_compatible.py --source ...` で作った場合、登場人物名の ASR の誤変換や方言の綴りは準備の時点ですでに修正されています。チャンクのテキストは ASR が書き起こしたものではなく、ソースの綴りです。その場合の Compare レビューは、主に韻律の健全性チェックです。`*emphasis*` と `...` のポーズ記号で不自然なものを拾い読みし、外れ値をいくつか `[e]dit` で直せば終わりです。

`--source` なしで JSONL を作った場合、各チャンクのテキストは生の ASR 出力です。Compare レビューでは、名前、方言、Preparer が捨てるべきと知らなかった音声だけの要素を直すことになります。これがこのスクリプトがもともと想定していた典型的な流れです。

## 重要：実際のターミナルで実行する

このスクリプトは**対話式**で、各項目であなたのキー入力を待ちます。AI のコーディングツールなど、stdin に対応しない環境ではなく、実際のターミナルセッションで実行してください。

```bash
cd /path/to/alexandria-audiobook2
app/env/bin/python alexandria_compare.py \
  --jsonl /path/to/metadata.jsonl \
  --source /path/to/original.txt
```

そして `>` のプロンプトで、選んだキー（`a`、`k`、`e`、`s`、`q`）を直接入力します。

## 使い方

```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source original_book.epub
```

プレーンテキストファイルの場合は次のとおりです。

```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source original_book.txt
```

データセットがすでに ZIP なら、先に `metadata.jsonl` を取り出します。

```bash
unzip alexandria_dataset.zip dataset_temp/metadata.jsonl
```

## オプション

| フラグ | 既定値 | 説明 |
|---|---|---|
| `--jsonl PATH` | 必須 | `metadata.jsonl` へのパス |
| `--source PATH` | 必須 | 元の `.epub` か `.txt` |
| `--output PATH` | `<jsonl>_corrected.jsonl` | 修正済み JSONL の書き出し先 |
| `--threshold N` | `0.90` | これを超える類似度の項目を自動承認 |
| `--review-all` | オフ | 類似度にかかわらずすべての項目を表示 |
| `--reset` | オフ | 保存した進捗を捨てて最初からやり直す |
| `--source-start N` | オフ | ソースの N 番目の単語から手動で開始（自動アンカーを飛ばす） |
| `--source-start-text "..."` | オフ | ソース内のフレーズをあいまい検索してそこから開始 |
| `--no-auto-anchor` | オフ | 自動アンカーを無効にし、ソースの 0 番目の単語から開始 |
| `--review-preanchor` | オフ | アンカーより前の項目を 1 つずつレビュー（既定：自動で保持） |

## ソースの読み込み

起動時に、次のような診断行が 1〜2 行表示されます。

```
Loading source  : /path/to/book.epub
  284,125 characters
  29 recurring proper nouns (gifted, godou, grotesqueries, hana, kanoko, kaya, keiko, kiyoka +21 more)
```

固有名詞の行は、スクリプトがソースから正しい登場人物名を特定できたかを素早く確認するためのものです。文の途中に何度も現れる、先頭が大文字の単語を走査します。登場人物名、地名、`Grotesqueries` や `Spirit Sight` のような物語固有の用語です。整列では、ASR で崩れたチャンクの単語（`coodo`、`youth`、`caia`）がソース内でこれらの名前の隣にあるとき、境界の受け入れ基準を緩めるためにこれらを使います。サンプルがおかしい場合（主要人物が欠けている、一般名詞ばかり）でも整列は動きますが、固有名詞の境界で `[e]dit` が多く必要になることがあります。

スクリプトはソースの読み込み時に、EPUB 抽出でよく起きる 2 種類の不具合も自動で直します。

- **文字-数字-文字の乱れ**（`thos1e` → `those`、`Kars1a` → `Karsa`）。OCR が文字の間に余計な数字を挿入したものです。
- **分かれたダイアクリティカルマーク**（`fianc é` → `fiancé`、`fiancé e` → `fiancée`）。EPUB の抽出が、合成済みのラテン文字のダイアクリティカルマークを stem から切り離したものです。

これらには何もする必要はありません。自動で処理されます。

## 初期の整列（音声の冒頭とテキストの前付）

オーディオブックとソーステキストは、めったに同じ位置から始まりません。

- 音声は、テキストにない**クレジット**（「Audible presents…」）、**ナレーターの導入**、**章のアナウンス**で始まることがあります。
- テキストは、ナレーターが読まない**著作権ページ**、**目次**、**献辞**で始まることがあります。

初回の実行では、Compare ツールが**自動アンカー検索**を行います。ソース全体を走査し、最初の確かな JSONL 項目に最もよく一致する位置を見つけて、そこから整列を始めます。

次のような出力が出ます。

```
🔍 Searching for initial alignment anchor...
  ✓ JSONL entry 3 anchors at source word 287 (62.1% match)
  Source preview: "It was a warm Saturday morning when Sakuta..."

  ⚠ 3 JSONL entries before the anchor have no matching source text
     (likely audio intro/credits not present in the text)
     ✓ Auto-kept as-is (use --review-preanchor to review them individually)
```

### 自動アンカーが間違える場合

アンカーが誤った位置に着いた場合（たとえば本の中でフレーズが繰り返され、別の箇所を見つけた場合）は、手動で上書きします。

```bash
# Skip the first 412 words of source (e.g., copyright + TOC + dedication)
app/env/bin/python alexandria_compare.py \
  --jsonl metadata.jsonl --source book.txt --source-start 412

# Or search for a distinctive opening phrase
app/env/bin/python alexandria_compare.py \
  --jsonl metadata.jsonl --source book.txt \
  --source-start-text "It was a warm Saturday morning"

# Or disable auto-anchor entirely and start at word 0
app/env/bin/python alexandria_compare.py \
  --jsonl metadata.jsonl --source book.txt --no-auto-anchor
```

### アンカーより前の項目

アンカーの位置より前の JSONL 項目には、対応するソーステキストがありません。既定ではそのまま自動で保持されます（通常は音声だけの導入部分で、上書きしたくないため）。`--review-preanchor` を使うと、代わりに 1 つずつレビューできます。

注意：自動アンカーは**初回**の実行でのみ動きます。チェックポイントから再開するときは、保存されたカーソル位置がそのまま使われ、以前の整列の判断が保持されます。

## 途中での再整列

最初のアンカーが良くても、本の途中でソースと音声がずれることがあります（音声がテキストにない章タイトルを読む、テキストにある箇所を音声が飛ばす、など）。スクリプトはこれを自動で検出します。

- 項目がカーソル付近のソース範囲に対して低いスコアになると、スクリプトは**前方をより広く検索**します（最大 3,000 語）。
- 確信できる一致が見つかれば、カーソルはそこへ**前方にジャンプ**します。
- 前方のどこにも確信できる一致がなければ、その項目にはソースの対応箇所がないと印を付け、次のように表示します。

  ```
  ORIGINAL  :  (no matching passage found in source within search range)
  ```

  こうした項目では `[a]`（original を受け入れる）は無効です。`[k]` keep、`[e]` edit、`[s]` skip を使ってください。

これは主に、音声だけで読まれる章のアナウンス、音声の末尾のクレジット、音声だけのナレーターの挿入を捉えます。

## レビューの画面

しきい値を下回る各項目は、次のように表示されます。

```
────────────────────────────────────────────────────────────────────────
Entry 42/4713  sample_0042.wav  7m 1s → 7m 11s  Match: 78.3%
────────────────────────────────────────────────────────────────────────
ANNOTATED :  He *walked* ... slowly across the room .... pausing to look
ORIGINAL  :  He walked slowly across the room, pausing to look

  TRANS diff:  He walked [slowly] across the room [pausing] to look
  ORIG  diff:  He walked [slowly] across the room [,] [pausing] to look

  [a] accept original   [k] keep annotation   [e] edit manually   [s] skip for now   [q] quit & save
  >
```

**差分の色：**

- `RED` の単語 — 文字起こしにあって元のテキストにない（余分／誤り）
- `GREEN` の単語 — 元のテキストにあって文字起こしにない（欠落）
- 通常の色 — 一致する単語

**ANNOTATED** は、TTS の記号（`...`、`*word*`）を含む全文を表示します。差分の行は記号を除いて正規化した単語を比べるので、句読点のノイズが比較を曇らせません。

## 選択肢

| キー | 動作 |
|---|---|
| `a` | JSONL のテキストを素のソーステキストに置き換える。**韻律の記号はすべて除かれる。** |
| `m` | **Merge** — ソースの単語を使いつつ、LLM の `*emphasis*` と `...` のポーズを残す。注釈に残す価値のある記号がある場合だけ表示される。 |
| `k` | 現在の注釈テキストをそのまま保つ |
| `e` | 自分で置き換えを入力する（JSONL に入る内容をそのまま書く） |
| `s` | この項目を飛ばす。変更されず、このセッションでは再表示されない（次の実行で表示される） |
| `q` | 進捗を保存して終了する。同じコマンドを再実行すると続きから再開できる |

## 声を平板にしないために：`[m]erge` を優先する

Preparer の LLM は JSONL に韻律の記号を加えます。

- `*word*` — 強調する単語への**強調**
- `...` — 自然なポーズ、`....` — より長いポーズ

これらの記号が、学習した TTS の声を表現豊かにします。多くの項目で `[a]ccept original` を選ぶと、注釈付きのテキストを素のソースの単語に置き換えることになり、**韻律の手がかりをすべて除いて**、声を平板で単調な出力へ近づけます。

`[m]erge` は単語を直しつつ記号を残します。

```
ANNOTATED :  HE *WALKED* SLOWLY... ACROSS THE ROOM
ORIGINAL  :  He walked slowly across the room,
MERGED    :  He *walked* slowly ... across the room,
            (correct spelling + capitalisation + the LLM's emphasis and pause)
```

Merge はいくつかの厄介な形も自動で扱います。

- **複数語の強調** — `*Trull Sengar*` は整列の前に `*Trull* *Sengar*` に展開され、両方の名前が記号を保ったままソースの綴りになります。
- **ドットでつながった連鎖** — `YOU...*WALKED*...HOME` のようなチャンク（LLM が空白の代わりに `...` を単語の区切りに使ったもの）は正しく分割され、各 `*word*` が Merge を生き残ります。
- **ASR でつぶれた名前** — ソースの 2 語のフレーズ `Trull Sengar` に対応する `*Tralesengar*` のような 1 語のチャンクは、連結したあいまい一致によって、強調が両方のソースの単語に広がります。

ASR の誤変換がひどく、単語ごとのあいまい一致も連結したあいまい一致もしきい値を超えない場合、強調が落ちることがあります。そうしたケースはレビューに現れるので、`[e]dit` で直せます。単語間のポーズはどちらの場合も保たれます。

セッションの最後に、Merge せずに original を数多く受け入れた場合は、次回は `[m]erge` を使うよう促す警告が表示されます。

## 中断と再開

どのレビューのプロンプトでも `q` を押すと停止します。チェックポイントファイルが JSONL の隣に自動で保存されます。

```
.metadata_compare_progress.json
```

同じコマンドをもう一度実行すると、中断したところから再開します。

```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source original_book.epub
```

完全に最初からやり直す（以前の判断をすべて捨てる）には次のとおりです。

```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source original_book.epub \
  --reset
```

## 出力

修正済みの JSONL は既定で `metadata_corrected.jsonl` に書き出されます（入力 JSONL の隣）。`[k]` で保持した項目と自動承認された項目は変更されません。テキストが変わるのは `[a]` accept と `[e]` edit の項目だけです。

出力先は別に指定できます。

```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source book.epub \
  --output my_reviewed_dataset.jsonl
```

## しきい値の調整

既定のしきい値 `0.90` は、単語の 90% 以上が元のテキストと一致する項目を黙って承認するという意味です。下げるとより多くの項目を確認できます。

```bash
# See everything with less than 95% match
python alexandria_compare.py --jsonl ... --source ... --threshold 0.95

# See everything, no auto-approve
python alexandria_compare.py --jsonl ... --source ... --review-all
```

## 整列の仕組み

ツールはソースの単語リストの中に**カーソル**を保ちます。各 JSONL 項目について次の処理をします。

1. 文字起こしから TTS の注釈記号（`...`、`*word*`）を除きます。
2. 文字起こしとソースの単語の両方を正規化します（小文字化、句読点を除去）。
3. カーソルの周囲 ±200 語のウィンドウを滑らせ、各位置をあいまいな系列一致で採点します。
4. カーソルを最良一致の末尾へ進めます。

つまり、**整列の精度は文字起こしが順番どおりであることに依存**します。音声から順に作られているので、常に順番どおりです。個々のチャンクに非常に酷い ASR の誤りがあると、整列が少しずれることがあります。周囲の項目で予想外に低い一致スコアとして気づきます。

## 完了時のサマリー

```
Complete!
  Auto-approved (≥90%) : 4512
  Kept annotation      : 83
  Accepted original    : 78
  Edited manually      : 12
  Skipped              : 28
  Re-run to review 28 skipped entries.
```
