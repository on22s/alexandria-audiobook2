# 台本生成

[日本語ホーム](Home-JA.md) · [English](Script-Generation.md)

LLM は本を、話者と演技指示を持つ行の一覧に変換します。

## 処理の流れ

1. Script で `.txt`、`.md`、`.epub` をアップロードします。EPUB はプレーンテキストに変換されます。
2. **Generate Annotated Script** は 3 段階で処理します。
   - **Split**：地の文と会話に分割。Setup の **Step 1: dialogue detection** で **Auto**（明確に分割でき検査を通るときは引用符、そうでなければモデル）、**Quote marks only**（モデル不使用）、**Quote-aware**（地の文中の引用語を地の文のまま扱う）、**Model only**（常にモデル）を選べます。
   - **Attribute**：登場人物一覧と前後の文章を使い、会話の話者を決定。
   - **Instruct**：各行に演技指示を追加。
3. 必要に応じて **Review** で間違いを修正します。
4. Voices に各話者の声のカードが作られます。

各段階にチェックポイントがあり、一時停止、クラッシュ・停電後の再開、完成部分のスナップショット、設定を残したやり直しができます。Generate 下の活動表示は段階、処理単位、試行回数、残り時間を示します。Windows では Pause は無効です。

## 台本の形式

JSON 配列です。以下は形式を示すオリジナルの例です。

```json
[
  {"speaker": "NARRATOR", "text": "扉がゆっくり開いた。", "instruct": "Calm, even narration."},
  {"speaker": "ELENA", "text": "あっ、誰？", "instruct": "Startled and fearful, sharp whispered question, voice cracking with panic."},
  {"speaker": "MARCUS", "text": "はは、僕を待っていたの？", "instruct": "Menacing confidence, low smug drawl with a dark chuckle, savoring the moment."}
]
```

`speaker` は `NARRATOR` または一覧にある大文字の人物名、`text` は実際に読む文章、`instruct` は感情・読み方・声の状態を示す短い指示です。「彼女は言った」などの話者表示は地の文で、会話の括弧は外します。指示は 1–2 文、英語ならおよそ 8–15 語を目安にします。

## 演技指示を書く

必要に応じて、感情（恐怖、喜びなど）、読み方（ささやく、言葉を切るなど）、声の状態（声が震える、息が混ざるなど）を組み合わせます。穏やかな行は短くし、感情の強い場面で詳しくします。

モデルへ渡す英語の指示例：`Cold fury, barely contained, voice tight.`、`Bright eager excitement, words tumbling out.`、`Sharp whispered warning, urgent and hushed.`。ナレーションは通常 `Neutral, even narration.`。場面が変わるときだけ `Tense, clipped narration.` などに調整し、その場面内では一貫させます。

体の動き（前に身を乗り出すなど）、同義語の重複、弱い修飾、単純な fast / slow は避け、聞こえる声を記述します。deep / raspy など恒常的な声質は、選んだ声と競合するので **Character Style** 側に置きます。[声の説明と演技指示](Voice-Reference-JA.md)も参照してください。

笑い・ため息・驚きは、`Ah!`、`Haah...`、`Haha!`、`Hmm...` など発音できる文章と対応する指示で表します。括弧付きタグや特殊トークンで代用しません。

## LLM 設定

Setup で **Base URL**、**API Key**、**Model Name** を設定します。ローカルキーには `local`、環境変数には `env:NAME` が使えます。推論モデルは *low* とサーバー側の予算を使い、`<think>` を禁止しません。

| 設定 | 既定値 |
|---|---|
| 一般の Temperature | 0.6 |
| Step 1 / 2 / 3 の Temperature | 各 0.1 |
| Top P | 0.8 |
| Top K | 0（無効） |
| Min P / Presence penalty | 0 |
| Banned tokens | 空 |

各段階は独自の温度を使います。研究の temperature 0 の条件と、通常の UI の既定値は区別してください。

### リクエストの大きさ

第 1 段階は約 3,000 文字、第 2 段階は 25 行と前後約 2,000 文字が出発点です。`michel2_full` はこの前後の文脈を含めます。登場人物一覧は引き継がれます。失敗したリクエストでは context rescue が 2,000 / 4,000 / 6,000 文字の窓で各 2 回再試行します。大きくするとコンテキストとメモリーが必要です。[容量の選び方](Setup-and-Serving-JA.md#コンテキスト長の選び方)を確認してください。

## レビューと別名

**Review Script** は会話中の話者表示、人物に誤って割り当てられた地の文、地の文に埋もれた会話、細かく分割しすぎたナレーション、不適切な指示を修正します。

**Contextual Review** は既定で前後 4 行を含む重なる窓で処理します。保存したシリーズの **Batch review** は、任意で別名を先に検出し、前向き・後ろ向きの 2 回処理を行えます。**Find Nicknames** は人物の別名を探し、**Edit aliases** で修正して同じ声へまとめます。

## プロンプト

`app/` の `default_prompts_segment.txt`、`default_prompts_attribute.txt`、`default_prompts_instruct.txt`、`review_prompts.txt` にあります。システムメッセージ、`---SEPARATOR---`、`{roster}` と `{batch}` を含むユーザーメッセージで構成されます。

Setup でバリアントを選び、**Save as preset** で変更を保存します。**What the model will see** は第 2 段階の正確なメッセージ、**Reset to Defaults** は再起動なしの再読み込みです。プロンプト変更後の結果やアダプターの相性は、元の条件と区別してください。[プロンプトとアダプター（英語）](Prompts-and-Adapters.md)に詳細があります。

既定のプロンプトは英語向けです。日本語では `「」` や話者表示の慣習に合わせて調整し、TTS の **Language** を Japanese にします。モデルの比較は[英語の選択ガイド](Which-Model-For-Your-Card.md)を参照し、実際の本で確認してください。

## 保存した台本

**Saved Scripts** の **Save** は台本と声を `scripts/` に保存し、**Load** は復元します。**Repair** のプレビューでは話者・内容の修正と元ファイルのバックアップを確認できます。
