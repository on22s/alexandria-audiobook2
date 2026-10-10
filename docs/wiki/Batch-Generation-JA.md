# バッチ生成

[日本語ホーム](Home-JA.md) · [English](Batch-Generation.md)

複数の行をまとめて音声生成する仕組みと、メモリーに合わせた調整方法です。

## 生成経路

| Setup の TTS Mode | Render Pending / Regenerate All |
|---|---|
| `local` | Qwen3-TTS に複数行をまとめて渡すバッチ処理 |
| `external` | Gradio サーバー・プールへ 1 行ずつ並列リクエスト |

ローカルのバッチ経路は Setup のバッチ seed（空ならランダム）を使います。行単位の経路は話者別 seed を使います。外部モードは CustomVoice / Clone のみ対応し、Voice Design / LoRA はローカルが必要です。

## 声ごとのまとめ方

- **Custom**：話者ごとに集め、長さで並べ、サブバッチに分割。
- **Clone**：参照音声が共通の話者ごとにまとめ、参照プロンプトを一度作ってキャッシュ。
- **LoRA**：アダプターごとにまとめ、各行の指示と Character Style を渡す。
- **Voice Design**：バッチ対応の種類が終わってから 1 行ずつ生成。

通常は行数の多い種類を先に処理します。すべての種類のモデルを同時に載せる必要はなく、最大のグループがピークメモリーを決めます。

## サブバッチ

バッチ中で最も長い文章が終わるまで生成するため、短い行にパディングの無駄が生じます。長さの近い行をまとめて減らします。

短い順に並べ、グループを作り、最長が最短の **Length Ratio** 倍を超え、かつグループが **Min Sub-batch Size** 以上になった時点で分けます。

| 設定 | 既定値 | 意味 |
|---|---|---|
| Sub-batching | 有効 | 長さ別に分割 |
| Min Sub-batch Size | 4 | これより小さいグループを分割しない |
| Length Ratio | 5 | 最長・最短の比率のしきい値 |

## 調整

まず **Auto-Configure** をクリックします。GPU メモリーからバッチ設定を決めます。

| 設定 | 用途 |
|---|---|
| TTS Mode | バッチ処理は `local` |
| Parallel Workers | ローカルではバッチサイズ。自動設定は GPU に応じて 1–4。余裕があれば増やす |
| Max Items/Batch | 1 バッチの行数の上限 |
| Compile Codec | コーデックのデコードを最適化。初回に約 30–60 秒の準備 |
| Sub-batching | 長さの違いによる無駄を減らす |

英語版はミドルレンジ GPU で実時間の約 3–6 倍の速度、コンパイル後のデコードで約 3–4 倍の改善を報告しています。これは条件付きの測定で、全体の所要時間や全 GPU に対する保証ではありません。RX 7900 XTX（24 GB）の上流測定と RX 9070 XT の本フォークの測定は別です。上流の 20–60 workers を小さい GPU にそのまま適用しないでください。[測定表（英語）](Batch-Generation.md#upstream-benchmarks-rx-7900-xtx-24-gb-rocm-63)を参照してください。

## GPU メモリー

サブバッチ間で `gc.collect()` と `torch.cuda.empty_cache()` を行い、開始前の余裕の検査で収まらないバッチを拒否します。メモリー不足なら **Parallel Workers** と **Max Items/Batch** を下げます。

同じ GPU の LLM は TTS が使えるメモリーを減らします。LLM が外部にある場合だけ、**Provider request options** の **Runs on this machine's GPU?** を **No** にします。GPU ロックを回避する目的で事実と違う設定をしないでください。

## コーデックと AMD

**Compile Codec** はセッション中の最初の使用時に `torch.compile` でデコーダーを最適化します。バッチごとにコンパイルするのではなく、アプリ再起動後に再度準備します。

ROCm では MIOpen fast-find、Whisper エンコーダー用 Triton AMD flash attention、pytorch-triton-rocm の `triton_key` 互換処理をアプリが適用します。660M/680M/780M 系 APU の TTS は fp32 です。
