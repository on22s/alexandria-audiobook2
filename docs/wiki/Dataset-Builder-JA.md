# Dataset Builder

[日本語ホーム](Home-JA.md) · [English](Dataset-Builder.md)

Dataset は LoRA 学習用の音声を 1 サンプルずつ作り、保存前に試聴できる画面です。声の説明と感情を調整したい、複数回に分けて作りたい、文章と感情の組み合わせを聴き比べたいときに使います。Training の **Build New Dataset** からも開けます。

## 1：プロジェクトを作る

**New Dataset** で名前を入力します。`dataset_builder/` に作業領域を作り、セッションをまたいで保存します。

## 2：声を説明する

**Root Voice Description** に声の恒常的な特徴を入力します。例：`A warm, deep male narrator with a calm baritone`。[声の説明](Voice-Reference-JA.md)も参照してください。**Global Seed** は任意で、再現したい場合に指定し、ランダムなら空にします。

## 3：サンプルを追加する

**Add Row** で行を追加します。**Text** は読み上げる文章、**Emotion** は `cheerful`、`tense whisper` など、声の説明に加える読み方です。

**Emotion は VoiceDesign で音声を生成するためのプロンプトで、学習ラベルとして保存されません。** 実録音を用意する場合は[録音 ZIP の形式](Training-Guide-JA.md#録音を-zip-でアップロードする)を参照してください。

平静、喜び、怒り、悲しみ、緊張、命令などを含め、短い一言と長い文章を混ぜます。参照音声用に、長めの穏やかな文章を少なくとも 1 つ用意します。

## 4：生成して試聴する

- 行の **Generate**：そのサンプルだけ生成。
- **Generate Pending**：音声のないサンプルを生成。
- **Regen All**：すべて再生成。
- **Cancel**：バッチを中止し、完成したサンプルは保持。

各サンプルを聴き、必要に応じて Emotion や説明を変えて再生成します。

## 5：学習データとして保存する

代表的で明瞭な **reference sample** を選びます。通常は長めの平静な文章が向き、学習時の話者埋め込みに使う `ref.wav` になります。**Save as Training Dataset** で `lora_datasets/` へコピーし、Training に表示します。

保存はコピーなので、作業中のプロジェクトも残ります。**Import / Export JSON** で行の定義を別のマシンへ移せます。

## 保存場所と API

`dataset_builder/{name}/state.json` に行、説明、seed を保存します。ブラウザーを閉じても後で続けられます。[API ガイド](API-Reference-JA.md#dataset-builder)から HTTP の操作例へ進めます。
