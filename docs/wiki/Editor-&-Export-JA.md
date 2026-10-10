# 編集と書き出し

[日本語ホーム](Home-JA.md) · [English](Editor-&-Export.md)

Editor は行単位の「chunk（分割された行）」を編集・生成・試聴する画面、Result は完成した音声を書き出す画面です。

## 行の編集

各行には **Status**（Pending / Done / Error）、**Speaker**、**Text**、**Instruct** があります。フィールドを編集すると自動保存されます。行の **generate** ボタンでその行だけ再生成するか、**Render Pending** で未生成の行をまとめて生成します。行の追加・削除もできます。

## 音声生成

- **Render Pending**：音声が必要な行を生成。
- **Regenerate All**：全行を再生成。
- `local` はバッチ処理、`external` は 1 行ずつ並列リクエスト。[バッチ生成](Batch-Generation-JA.md)を参照。
- GPU ロックに従い、同じ GPU の LLM が台本を作成している間は開始しません。LLM が別の GPU / マシンの場合は設定で明示します。
- **Cancel**：処理を中止し、完成した行は保持。

## 確認と試聴

**Check Voices** は参照音声との ECAPA 類似度でずれを検出し、**flagged only** で該当行だけ表示します。**Text integrity** は元の文章との差を語ごとに表示します。生成したファイルは音声として検証され、破損した行にフラグが付きます。これらの検査に加えて、実際に聴いて確認してください。

行の再生ボタンで個別に、**Play Sequence** で順番に再生します。Setup の **Speaker Change Pause** は既定 500 ms、**Same Speaker Pause** は既定 250 ms です。

## 結合

**Merge All** は有効な音声を順番に結合し、Result に表示します。各行の先頭・末尾の不要な無音を除いてから、設定した間を入れます。未完成や Pending の行がある場合は、書き出す前に文章の欠落がないか確認してください。

## 書き出し

### MP3

Result から全体の 128 kbps MP3 をダウンロードします。ファイルは `cloned_audiobook.mp3` です。

### M4B

**Export M4B** で章マーカー付きのファイルを作ります。**Title**、**Author**、**Narrator**、**Year**、**Description**、**Cover Image** を設定し、**Per-chunk chapters** も選べます。

### 章別ファイル

**Export chapters** で章ごとの MP3 / WAV を作ります。`{chapter_number} - {chapter_name}` のようなテンプレート、番号のゼロ埋め、本・シリーズ名、巻、章一覧、行別ファイル、変更した章だけの書き出し、ファイル名プレビューに対応します。ファイルと ZIP は `chapter_exports/` に保存されます。

### 行別ファイル

`voicelines/` に読み順の番号と話者名で保存されます。たとえば `voiceline_0001_narrator.mp3`、`voiceline_0002_elena.mp3` のような名前です。

### Audacity

**Export to Audacity** は話者ごとの WAV トラックを含む `audacity_export.zip` を作ります。

```text
audacity_export.zip
├── project.lof
├── labels.txt
├── narrator.wav
├── elena.wav
├── marcus.wav
└── ...
```

1. ZIP をダウンロードして展開します。
2. Audacity で `project.lof` を開くと、全トラックを読み込めます。
3. **File > Import > Labels** で `labels.txt` を読み込みます。

各トラックは同じ長さになるよう無音で埋められ、同時再生すると結合 MP3 と同じ配置になります。人物ごとの音量・効果、行間のタイミング調整に使えます。

## 小さい・壊れた MP3

428 バイト程度の MP3 は ffmpeg が見つからない、または MP3 をエンコードできない場合に生じます。インストーラーは環境に ffmpeg を導入するため、`app/env` の外で起動していないか確認し、Pinokio または正しい環境で再生成してください。[トラブルシューティング](Troubleshooting-JA.md)に詳細があります。
