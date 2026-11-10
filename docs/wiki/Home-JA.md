# Alexandria Audiobook2 日本語 Wiki

[English](Home.md) · [中文入門](../../README_CN.md) · 日本語

Alexandria Audiobook2 は本を登場人物ごとに異なる声で読み上げ、MP3 や章付き M4B に書き出すアプリです。LLM が台本の話者と演技指示を付け、Qwen3-TTS が音声を生成します。

この Wiki の日本語ページは、アプリの利用ガイドと研究ページの日本語版です。研究成果と実験条件の正本は英語の文書にリンクしています。研究中の目標や特定のテストでの結果は、任意の本の品質を保証しません。

## ここから始める

[日本語 README](../../README_JA.md#インストール)でインストールし、[最初のオーディオブック](../../README_JA.md#最初のオーディオブック)を試してください。[オリジナルの短い日本語サンプル](../examples/first-book-ja.txt)を保存してアップロードし、CustomVoice のプリセットで音声を生成・結合して MP3 をダウンロードします。研究の文書は初回利用の前提ではありません。

## オーディオブックを作る

- [Setup と LLM サーバー](Setup-and-Serving-JA.md)：接続、推論、コンテキストとメモリー。
- [台本生成](Script-Generation-JA.md)：3 段階の処理、レビュー、プロンプト、復旧。
- [声の種類](Voice-Types-JA.md)：CustomVoice、Clone、LoRA、Voice Design、状態別ペルソナ。
- [声の説明と演技指示](Voice-Reference-JA.md)：説明文と `instruct` の使い分け。
- [バッチ生成](Batch-Generation-JA.md)：音声生成の仕組みと設定。
- [編集と書き出し](Editor-&-Export-JA.md)：行の編集、試聴、結合、MP3 / M4B / Audacity。
- [トラブルシューティング](Troubleshooting-JA.md)：ログ、LLM、GPU、音声の問題。

## 自分の声を作る

- [Dataset Builder](Dataset-Builder-JA.md)：1 サンプルずつ試聴して学習データを構築。
- [LoRA 学習ガイド](Training-Guide-JA.md)：録音の形式、学習設定、試聴と採用。

## リファレンス

- [API ガイド](API-Reference-JA.md)：curl の例、状態確認、認証、正本への案内。
- [文書一覧（英語）](../README.md)：追加の利用・運用ガイド。

## 研究と開発

- [モデル選び（GPU 別）](Which-Model-For-Your-Card-JA.md)：GPU メモリー別の推奨モデルと速度。
- [結果](Results-JA.md)：モデル、量子化、プロンプト、アダプターの結果を 1 か所に。
- [プロンプトとアダプター](Prompts-and-Adapters-JA.md)：アダプターが従うルールと、効く条件・コストになる条件。
- [評価手順](Evaluation-Recipes-JA.md)：アダプター評価の実施方法。
- [Thunder 運用](Thunder-Operations-JA.md)：リモート GPU でのジョブ運用。
- [Hugging Face リリース](Hugging-Face-Releases-JA.md)：公開リリースに含めるもの。

[GOALS](../../GOALS.md)、[RECIPES](../../RECIPES.md)、[実験索引](../../RESULTS_INDEX.md)は、目標、再現手順、個別の成果物を記録します。

アダプターは学習したプロンプトで評価してください。異なるプロンプトでの結果は、元の条件での結果とは区別する必要があります。

## インストールの確認

アプリには実 API を使う検証スイートがあります。通常は、空の専用データ領域で使い捨てのアプリを起動する方式を選びます。

```bash
cd app
python run_isolated_api_tests.py
python run_isolated_api_tests.py --full
```

通常モードは TTS / LLM 不要で、設定、アップロード、台本ライブラリー、声、行、状態確認、Designer、LoRA 一覧、Dataset Builder、エラー処理などを検証し、実行・スキップ件数を表示します。`--full` は生成も検証するため、GPU と LLM が必要です。

起動中のアプリを対象にする `python -m tests.test_api --url http://127.0.0.1:<port>` は、対象アプリのアップロード、台本、声の設定を作成・削除します。普段使うデータで安易に実行しないでください。`app` から `-m` で実行する必要があり、`python tests/test_api.py` の直接実行は `ModuleNotFoundError: utils` になります。

通常モードの成功が確認するのは、そのスイートが通るモデル不要の API 経路です。モデルのロード、GPU、生成品質、すべてのプロバイダーや本の動作までは保証しません。

---

利用ガイドは元の Alexandria Wiki を基に、このフォークの現行動作に合わせています。[リポジトリ](https://github.com/on22s/alexandria-audiobook2) · [Issues](https://github.com/on22s/alexandria-audiobook2/issues)
