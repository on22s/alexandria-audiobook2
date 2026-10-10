# 声の説明と演技指示

[日本語ホーム](Home-JA.md) · [English](Voice-Reference.md)

VoiceDesign の **description** は声そのものを、行の **instruct** は感情・読み方・テンポを指定します。このページは使い方と代表的な語彙を説明します。完全な語彙表と比較実験は[英語の正本](Voice-Reference.md)にあります。

## 使い分け

| 入力欄 | 記述するもの | 例 |
|---|---|---|
| Description / Root Voice Description | 声域、音色、質感など恒常的な特徴 | `female mezzo-soprano, silky even tone, soft rounded edges, gentle clarity` |
| Instruct / Emotion（データ生成時） | その行の感情、読み方、勢い | `Sharp whispered warning, urgent and hushed.` |

英語の語句はモデルに渡す表現として残しています。日本語の訳は意味を理解するための案内です。description に場面・行動・テンポまで混ぜると、声の同一性と演技が競合する場合があります。

## 声そのものの語彙

まず声域（bass、baritone、tenor、alto、mezzo-soprano、soprano）、次に 2–3 個の明確な音色・質感を指定します。

| 系統 | 英語の例 | 意味の目安 |
|---|---|---|
| 粗さ | gravelly、raspy、husky、gruff | ざらつく、かすれる、ハスキー、ぶっきらぼうな響き |
| 滑らかさ | silky、velvety、mellow、rounded | 滑らか、柔らか、落ち着いた、丸みのある響き |
| 共鳴 | chesty、full-bodied、deep、resonant | 胸の響き、厚み、深さ、よく響く声 |
| 軽さ・高さ | airy、breathy、thin、falsetto | 空気感、息の混ざり、細さ、裏声 |
| 鼻・鋭さ | nasal、twangy、sharp、strident | 鼻にかかる、特徴的な鼻音、鋭い、耳に強い響き |

英語ガイドの観察では `silky`、`even`、`precise`、`firm` など明確な質感・制御の語が安定に役立ちました。これは試した条件での観察で、全言語・全人物への保証ではありません。

## 行の感情と演技

感情（cheerful、fearful、somber、seething、defeated）、読み方（whispered、clipped、measured）、声の状態（voice cracking、tight、strained）を必要に応じて組み合わせます。人物の基本音色を毎行変える指示を避けます。

ナレーションは `Neutral, even narration.` から始め、場面ごとに必要な修飾を加えます。体の動き、弱い修飾、同じ意味の重複より、実際に聞こえる声を指定してください。[台本生成](Script-Generation-JA.md#演技指示を書く)にも例があります。

## 語の組み合わせに注意する

英語ページの bright tenor の比較では、`bright` と `youthful energy` を同じ description に入れた場合に大きなばらつきが観察されました。片方だけなら安定した条件もあり、単語単体の良し悪しと組み合わせの効果は別です。音色は description、行動的な勢いは instruct に分け、試聴して調整します。

`hollow` は感情の空虚さとしても音響的な空洞感としても解釈されるため、落胆を指示したい場合は `defeated` など明確な語も試せます。曖昧な語を増やすより、短く明確にすることが役立つ場合があります。

## 完全な参照表

[英語ページの Part 1](Voice-Reference.md#part-1-the-directors-vocabulary)には、音色、感情、テンポ、広告・役柄、録音現場の用語、拡張語彙があります。録音現場の指示語がそのまま TTS のパラメーターになるわけではありません。[Part 2](Voice-Reference.md#part-2-what-voicedesign-does-with-these-words)のモデル観察と、実際の音声を分けて確認してください。
