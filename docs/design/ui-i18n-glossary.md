# UI translation glossary

The single source for how this interface says things in Simplified Chinese
(`zh-CN`), Japanese (`ja`) and Latin American Spanish (`es-419`). Catalogs under
`app/static/i18n/` and the translated docs take repeated terms from this file, so
the on-screen label and the doc label cannot drift (design section 6.3).

## Status

- P1 ships the catalog infrastructure and the first 16 keys. P2 adds the 30
  server refusal keys below. The three locales are **machine drafts**
  (`_meta.review_status.all = "machine-draft"`) and the language picker says so;
  no native reviewer has signed off.
- `safety.*` keys are held to a higher bar: a locale cannot be offered as
  reviewed while its `_meta.review_status.safety` is not `"human-reviewed"`.
  P2 adds eight `safety.*` keys, all still `not-reviewed`, so every translated
  locale keeps its draft label. A safety refusal shows its translated line with
  the authored English beneath it (`tSafety` / `renderUiSafety`), and the parity
  test enforces the review rule.
- The task log transcript (`process_state[...]["logs"]`) and subprocess output
  stay English in every locale. The UI chrome around them is translated.
- P3 adds 746 `help.*` keys: every entry of the seven maps in
  `app/static/js/app-help.js`, the `recommended` framing message, and the inline
  `getItemHelpText` fallbacks. Keys are derived from the stable help ids
  (`help.field.llm-context-length.what`), so an English typo cannot invalidate a
  translation; `recommend` and `source` are separate keys, and a `source` value
  stays English because it names a PR, a `RECIPES.md` section or a measurement.
  All three locales remain machine drafts.
- P4 adds 451 keys: 439 in the Setup pane and app shell (text nodes,
  `title`/`placeholder`/`aria-label` attributes, top nav and tab names, the
  shared modals) and 12 for the settings-search and preview text in
  `app-settings.js`. Four of them are `safety.*` keys. Nine paragraphs that must
  keep `<strong>`/`<code>`/`<em>` use `data-i18n-html`; everything else is plain
  text. The help texts that named **Save Configuration** now name the label as
  it appears, English in parentheses on first mention. All three locales remain
  machine drafts.

## P2 server-refusal keys

These English sentences come from `raise AppError(..., key=...)` sites, are
harvested into `app/static/i18n/ui.en.json` by
`tools/i18n/extract_ui_catalog.py`, and are pinned byte-for-byte by
`app/tests/fixtures/api_detail_golden.json`. `safety.*` marks the eight that
protect user data (stale book/voice identity, an unverified adapter, a changed
DOCX preview, and an unconfirmed book switch).

| Key | English source |
| --- | --- |
| `error.task.already_running` | {task} is already running. |
| `error.task.gpu_busy` | Cannot start {task}: {running} is currently running. Please wait for it to finish or cancel it to free up GPU VRAM. |
| `error.book.switch_tasks_running` | Cannot {operation} while tasks are running: {tasks}. Wait for them to finish or cancel them. |
| `error.book.switch_other_process` | Cannot {operation} while another process owns a task. Wait for it to finish or cancel it. |
| `error.book.owned_by_batch_review` | This saved book is owned by batch review. Wait for it to finish or cancel it. |
| `error.book.owned_or_saving` | This saved book is owned by batch review or another save. Wait for it to finish or cancel it. |
| `error.book.busy_preview` | Book state is busy; retry the preview shortly. |
| `error.book.snapshot_busy` | Book or saved snapshot is busy; retry shortly. |
| `error.upload.no_input_active` | No input file selected for the active book. |
| `error.upload.source_too_large` | Source exceeds the upload storage limit. |
| `error.upload.source_empty` | Active source is empty; import a non-empty book first. |
| `error.upload.unsupported_format` | Supported source formats are TXT, Markdown, EPUB and confirmed single-book DOCX. |
| `error.upload.storage_busy` | Upload storage is busy; retry shortly. |
| `error.upload.storage_limit` | Source upload storage limit reached. |
| `error.upload.docx_text_too_large` | Extracted DOCX text exceeds source upload limits. |
| `error.upload.epub_empty` | No readable text content found in EPUB. |
| `error.upload.epub_text_too_large` | Extracted EPUB text exceeds source upload storage limits. |
| `error.generation.no_recovery` | No failed or incomplete three-pass generation is available to resume. |
| `error.generation.no_input` | No input file selected |
| `error.voice.version_applied` | Voice version is applied; select another base version or clear its timeline points first |
| `error.voice.config_busy` | Voice config is busy (locked by another operation); please try again. |
| `error.voice.busy` | Book or voices are busy; please try again. |
| `safety.book.switch_unconfirmed` | Could not complete or confirm {operation}; check the current state before retrying. |
| `safety.book.changed_while_preview` | Active book changed while reading the preview. |
| `safety.upload.docx_preview_changed` | DOCX preview changed; preview and accept this exact file again. |
| `safety.generation.source_changed_before_start` | Selected source changed before generation started. |
| `safety.generation.recovery_changed` | Recovery checkpoint changed before start. |
| `safety.voice.book_changed_before_save` | Active book changed; reload voices before saving |
| `safety.voice.book_changed` | Active book changed; reload voices |
| `safety.voice.adapter_identity_unverified` | Adapter identity could not be verified; restore the adapter manifest or reload voices before saving. |

## Doc label convention (from P9)

The merged docs pin one label per UI element. When a surface is translated, its
doc label becomes `translated (English)` — for example `**数据集构建器 (Dataset
Builder)**` — because maintainers, issues and `RECIPES.md` refer to tabs by their
English names and the English UI remains a supported locale. Until P9 the docs
keep the English tab names; the translation column below is the target the
catalogs use.

Translated doc sets: Chinese and Japanese have existed for a while; **Spanish
docs now exist too** (`README_ES.md`, `docs/guides/*_ES.md`,
`docs/wiki/*-ES.md`, merged in #1886). The design document's older "ES 0 files"
figure is stale. The Spanish renderings below follow the merged docs.

## Terms

| English | zh-CN | ja | es-419 | Notes |
| --- | --- | --- | --- | --- |
| Setup | 设置 | セットアップ | Configuración | Numbered tab 1. Docs keep the English tab name until P9. |
| Script | 脚本 | 台本 | Guion | Numbered tab 2. |
| Voices | 声音 | 声 | Voces | Numbered tab 3. Japanese uses 声, never ボイス for a voice. |
| Editor | 编辑器 | エディター | Editor | Numbered tab 4. Spanish docs keep `Editor`. |
| Result | 结果 | 結果 | Resultado | Numbered tab 5. |
| Designer | 设计器 | デザイナー | Diseñador | Docs currently keep `Designer`. |
| Preparer | 准备器 | プリペアラー | Preparador | Docs currently keep `Preparer`. |
| Dataset | 数据集 | データセット | Conjunto de datos | Docs often keep `Dataset`. |
| Dataset Builder | 数据集构建器 | データセットビルダー | Constructor de conjuntos de datos | |
| Training | 训练 | トレーニング | Entrenamiento | |
| Voice Lab | 语音实验室 | ボイスラボ | Laboratorio de voz | |
| Reports | 报告 | レポート | Informes | |
| Settings | 设置 | 設定 | Ajustes | The settings pane, not a tab label. |
| Configuration | 配置 | 設定 | Configuración | The saved configuration file. |
| Save | 保存 | 保存 | Guardar | |
| Save Configuration | 保存配置 | 設定を保存 | Guardar configuración | P1 messages name this button in English because the button itself is not keyed yet. |
| Cancel | 取消 | キャンセル | Cancelar | |
| Continue | 继续 | 続行 | Continuar | |
| Apply | 应用 | 適用 | Aplicar | |
| Export | 导出 | 書き出し | Exportar | Japanese docs use 書き出し, not エクスポート. |
| Take | 录音 | テイク | Toma | `TAKE_HISTORY` docs use テイク / toma. |
| Chunk | 分块 | チャンク | Fragmento | Spanish docs use `fragmento`. |
| Speaker | 说话人 | 話者 | Hablante | A person in the book. |
| Character | 角色 | 登場人物 | Personaje | |
| Character roster | 角色表 | 登場人物一覧 | Reparto de personajes | |
| Cast | 声音阵容 | キャスト | Reparto | A reusable character→voice mapping. |
| Voice | 声音 | 声 | Voz | A synthesized timbre. |
| Narrator | 旁白 | ナレーション | Narrador | |
| Review | 审阅 | レビュー | Revisión | |
| Generate | 生成 | 生成 | Generar | |
| Render | 渲染 | レンダリング | Renderizar | Audio rendering. |
| Preview | 预览 | プレビュー | Vista previa | |
| Book | 书 | 本 | Libro | |
| Upload | 上传 | アップロード | Subir | |
| Library | 库 | ライブラリ | Biblioteca | Saved scripts, voices, casts. |
| Series | 系列 | シリーズ | Serie | |
| Chapter | 章 | 章 | Capítulo | |
| Segment (verb) | 切分 | 分割 | Segmentar | Pass 1 of script generation. |
| Attribution | 归属 | 話者判定 | Atribución | Pass 2: who speaks each line. |
| Performance instruction | 演绎指令 | 演技指示 | Indicación de actuación | Pass 3: how the line is read. |
| Prompt | 提示词 | プロンプト | Prompt | Spanish docs keep `prompt`. |
| Model | 模型 | モデル | Modelo | |
| Provider | 提供方 | プロバイダー | Proveedor | An OpenAI-compatible endpoint. |
| Retry | 重试 | 再試行 | Reintento | |
| Timeout | 超时 | タイムアウト | Tiempo de espera | |
| Log | 日志 | ログ | Registro | The transcript stays English; the heading is translated. |
| Language | 语言 | 言語 | Idioma | |
| Clone | 克隆 | クローン | Clonación | |
| Casting | 试音 | キャスティング | Selección de voces | The Casting tab: designing and choosing voices, not a saved cast (`Reparto`). |
| Workspace | 工作区 | ワークスペース | Espacio de trabajo | The home/workflow landing tab. |
| Preset | 预设 | プリセット | Preajuste | Prompt/voice presets. |
| Auto-Configure | 自动配置 | 自動設定 | Configuración automática | The Setup header button. |
| Connection | 连接 | 接続 | Conexión | Setup category 1 and its heading. |
| Limits & retries | 限制与重试 | 制限と再試行 | Límites y reintentos | Setup category 2. |
| Speech engine | 语音引擎 | 音声エンジン | Motor de voz | Setup category 3. |
| Audio & timing | 音频与时间 | 音声とタイミング | Audio y tiempos | Setup category 4. |
| Generation | 生成 | 生成 | Generación | Setup category 5. |
| Script strategy | 脚本策略 | 台本戦略 | Estrategia de guion | Setup category 6. |
| Prompt studio | 提示词工作室 | プロンプトスタジオ | Estudio de prompts | Setup category 7. |
| Setup UI language | 设置界面语言 | セットアップ UI の言語 | Idioma de la interfaz de Configuración | The selector under the category list. |
| Voice Design | 声音设计 | ボイスデザイン | Diseño de voz | Product name; the Japanese stays ボイスデザイン. |
| Draft | 草稿 | 下書き | Borrador | Review status, not a UI label. |
| Profile | 配置档案 | プロファイル | Perfil | An LLM connection profile, not `Configuration`. |
| Seed | 随机种子 | シード | Semilla | |
| Adapter | 适配器 | アダプター | Adaptador | LoRA / built-in / trained. |
| Checkpoint | 检查点 | チェックポイント | Punto de control | Attribution and training recovery points. |
| Batch | 批处理 | バッチ | Lote | Plural `.one`/`.many` keys are unaffected. |
| Candidate | 候选 | 候補 | Candidato | Voice or checkpoint candidate. |
| Silence | 静音 | 無音 | Silencio | Inserted pause or detected quiet audio. |
| Sentence | 句子 | 文 | Oración | |
| Clause | 从句 | 節 | Cláusula | |
| Paragraph | 段落 | 段落 | Párrafo | |
| Fade | 淡入淡出 | フェード | Fundido | |
| Crossfade | 交叉淡化 | クロスフェード | Fundido cruzado | |
| Loudness | 响度 | ラウドネス | Sonoridad | Mastered integrated loudness. |
| True peak | 真峰值 | トゥルーピーク | Pico real | |
| Mastering | 母带处理 | マスタリング | Masterización | |
| Sidecar | sidecar 文件 | サイドカー | Archivo sidecar | The read-along time file. `sidecar` stays when naming the format. |
| Reference | 参考 | リファレンス | Referencia | Cloning reference, reference voice, reference take. |
| Alignment | 对齐 | アラインメント | Alineación | ASR word/source alignment. |
| Diarization | 说话人分离 | ダイアライゼーション | Diarización | |
| Segmentation | 切分 | セグメンテーション | Segmentación | Pass 1 output. |
| Timeline | 时间线 | タイムライン | Línea de tiempo | Voice-change and state timeline. |
| State (character) | 状态 | 状態 | Estado | Per-state voices. |
| Version | 版本 | バージョン | Versión | Saved voice/script version. |

## Voice and address rules

These four rules caused drift in earlier phases and are pinned here so a later
catalog edit can be checked against them:

- **Japanese "voice" is 声**, never ボイス. The product names ボイスデザイン
  (Voice Design) and ボイスラボ (Voice Lab) keep ボイス.
- **Chinese "voice" is 声音**, never 配音. A saved cast is 声音阵容 (or
  配音阵容 when the sentence needs the casting sense); neither word is used for
  an individual synthesized voice.
- **Spanish addresses the reader with tú everywhere**, including enclitics:
  Déjalo, Actívalo, not Déjelo/Actívelo. "Source text" is **texto fuente**.
- **Stock/preset voices** are 预设声音 (zh), プリセットの声 (ja) and
  **voz predefinida** (es).

The Spanish Editor tab is **Editor de líneas** in the UI: the docs keep the
English name `Editor`, and the longer UI label keeps it searchable while
differing from the English string the parity test would otherwise flag as an
untranslated copy.

## P4 safety keys

| Key | English source |
| --- | --- |
| `safety.shell.new_build_available` | A newer version of Alexandria is available. Save your work before reloading; reloading this page may discard unsaved changes. |
| `safety.voice.clone_unmeasured` | ASR, speaker identity and naturalness are UNMEASURED. Accepting creates a new voice; your current selection does not change until you choose it. |
| `safety.voice.clone_discard` | Discard preview |
| `safety.setup.unmanaged_fields` | {count} additional saved fields are outside the supported form and would not be retained by the existing Save. Review the saved file before saving. |

All four render through `tSafety`/`renderUiSafety`, so the translated line shows
with the authored English beneath it. The markup path reaches the same helper
because `applyUiCatalog` sends every `safety.*` key to `renderUiSafety`.

## Do not translate

These are identifiers. A translation would break a request, a lookup or a
reproduction record, and the parity test pins every one of them where it appears
in a catalog string:

- API paths (`/api/...`), HTTP field names, config keys (`llm_mode`,
  `parallel_workers`), storage keys (`alex-ui-locale`).
- `env:NAME` environment references.
- Model, quant and engine names: `Qwen3-TTS`, `Qwen3-14B`, `Whisper`, `LoRA`,
  `M4B`, `MP3`, `WAV`, `GPU`, `API`, `CUDA`, `ROCm`, `Alexandria`.
- File and directory names (`annotated_script.json`, `voicelines/`,
  `lora_models/`).
- `RECIPES.md`, `GOALS.md`, `RESULTS_INDEX.md` and their section names.
- Anything sent to a model as a prompt. Translating it would change model
  behaviour and invalidate the measurements in `RECIPES.md`.

## P1 catalog keys

The first real translation, small enough to review line by line. All 16 keys
carry `review_status.all = machine-draft` and `review_status.safety =
not-reviewed` in each locale.

| Key | English source |
| --- | --- |
| `setup.finder.label` | Find a setting |
| `setup.finder.placeholder` | Try context, memory, language or retries |
| `setup.finder.clear` | Clear |
| `setup.finder.hint` | Search labels and help for your current services. |
| `setup.finder.none` | No matching settings for your current services. Try another label or description. |
| `setup.finder.one` / `.many` | {count} matching setting. / {count} matching settings. |
| `setup.finder.limited` | Showing the first {limit}; refine your search for more specific results. |
| `setup.finder.opened` | Showing the original setting. Save Configuration to apply any edits. |
| `setup.preview.saved` | Saved |
| `setup.preview.would_save` | Would save |
| `setup.preview.action` | Preview changes |
| `setup.preview.title` | Settings change preview |
| `setup.preview.close` | Close preview |
| `setup.preview.defaults` | {count} missing settings filled with defaults |
| `setup.locale.storage_notice` | Browser storage is unavailable; this UI preference applies only to this tab until reload. |

The design's "14 #1732 messages" counts the plural pair as one message: there are
15 entries in the old `setupUiEnglish` table, plus the storage notice that was a
constant, so P1 ships 16 keys.
