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
- P5 adds 377 keys: 193 for the `script` pane (text nodes,
  `placeholder`/`title`/`aria-label` attributes, and the four safety refusals),
  78 for the Dataset Builder surface in `app-workbench.js` (the P5 "Workbench"
  surface; four of these are `safety.*` destructive confirms) and 106 for the
  task guides in `app-guides.js`. The guides are built by `buildTaskGuides()` on
  every render, so their English stays a literal argument of `t()` while a
  locale switch re-paints them. Destructive JS confirms keep the authored
  English on the line beneath the translation through
  `formatUiSafetyText()`, the string form of `tSafety()` (the confirm body's
  existing `white-space: pre-line`). All three locales remain machine drafts.
- P6 adds 891 keys: 78 for the `voices` pane and 90 for the `casting` pane
  (text nodes and `title`/`placeholder`/`aria-label` attributes), 118 for
  `app-pronunciation.js`, 175 for `app-casting-research.js`, 153 for
  `app-casting.js`, 88 for `app-character-roster.js` and 189 for the
  Voices/casting-only strings in `app-core.js` (Series Cast, voice suggestions,
  voice draft/save messages). The Casting search placeholder
  (`gender:female tone:calm -status:reject`) is `data-i18n-skip`: its field
  names are the query syntax and stay English in every locale. Studio activity
  rows keep the English `action` identifiers the server stores — the same
  transcript boundary as the task log — while the row's shape and labels are
  translated. All three locales remain machine drafts.
- P7 adds 1,200 keys: 340 for the `editor` pane, 144 for `audio`, 74 for
  `reports` and 84 for the home workspace (text nodes and
  `title`/`placeholder`/`aria-label`/`alt` attributes), plus the user-facing
  text of `app-reports.js` (74), `app-workspace.js` (84), `app-take-history.js`
  (74), `app-editor-review.js` (98 plus label keys), `app-readalong.js`,
  `app-audiobookshelf.js`, the five sound modules and the Editor/Audio strings
  in `app-core.js` (chunk rows, batch render, merge and export status). The
  Audiobookshelf API-key field keeps its technical placeholder under
  `data-i18n-skip` only where it is a URL. `app-calibre.js` is deliberately
  left for a later pass: its UNKNOWN-outcome state machine is not part of
  these panes. All three locales remain machine drafts.
- P8 adds 492 keys: 203 under `training.*`, 114 under `voicelab.*`, 69 under
  `preparer.*`, 28 under `dataset.*` (the dataset-builder pane), 19 under
  `designer.*`, 10 shared `language.*` names, 42 `core.*` labels (system
  status/ETA strip, LM Studio badge, duration units, task-status vocabulary)
  and 7 `safety.training.*` confirms. Pane text: text nodes and
  `title`/`placeholder`/`aria-label` attributes; module text: the user-facing
  strings of `app-training.js` (adapters, dataset upload, comparison, blind
  review, history), `app-voicelab.js` (config readiness, inspect badges,
  preflight, health) and the non-Dataset-Builder sections of `app-workbench.js`
  (Preparer outputs, the system-status/ETA strip, the LM Studio toggle,
  task-control restoration), plus the one shared task-status label map. With P8
  the whole of `index.html` is keyed except two live JS status lines. The two
  filesystem-path placeholders (`/path/to/…`) are `data-i18n-skip`: a path
  example is a technical value, not copy. `app-scripts.js` stays English: the
  Designer's runtime list and status messages are outside this phase's file
  scope. All three locales remain machine drafts. The P3 help entries that
  name the Voice Lab pipeline or the Designer now use the on-screen labels
  (`语音实验室 (Voice Lab)` / `ボイスラボ (Voice Lab)` / `Laboratorio de voz`,
  `设计器 (Designer)` / `デザイナー (Designer)` / `Diseñador`), and the Chinese
  `流程` entries that meant "pipeline" are now `流水线`.


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
| Pipeline | 流水线 | パイプライン | pipeline | The Spanish docs use the English loanword `pipeline`, so the catalog does too. |
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
| Workbench | 工作台 | ワークベンチ | Banco de trabajo | The P5 collective name for the Dataset Builder surface; the tab and screen are labelled **Dataset** (`数据集` / `データセット` / **Conjunto de datos**). |
| Dataset project | 数据集项目 | データセットプロジェクト | Proyecto de conjunto de datos | A saved Dataset Builder project. |
| Sample | 样本 | サンプル | Muestra | One Dataset Builder row; a synthetic snippet. |
| Status | 状态 | ステータス | Estado | Task or row state. Character **State** uses 状态 / 状態 for a per-state voice. |
| Generation log | 生成日志 | 生成ログ | Registro de generación | The Script pane's log download links. The transcript itself stays English. |
| Source scope | 来源范围 | ソース範囲 | Alcance del texto fuente | The "keep a later part of this book" panel. |
| Annotated script | 标注脚本 | 注釈付き台本 | Guion anotado | Pass output with speaker labels and directions. |
| First-person narrator | 第一人称旁白 | 一人称のナレーター | Narrador en primera persona | The optional narrator name. |
| Context window | 上下文窗口 | コンテキストウィンドウ | Ventana de contexto | Neighbouring entries used by contextual review. |
| Nickname | 昵称 | ニックネーム | Apodo | Alias detection. |
| Alias | 别名 | 別名 | Alias | A second name for a character. |
| Speaker label | 说话人标签 | 話者ラベル | Etiqueta de hablante | The label before a line of dialogue. |
| Provisional reference | 临时参考 | 暫定リファレンス | Referencia provisional | The saved script compared against for speaker-label suggestions. |
| Capture | 捕获 | 取得 | Captura | OPDS: explicitly downloading one source before import. |
| Catalog (OPDS) | 目录 | カタログ | Catálogo | The OPDS catalog, distinct from the Scripts library (`库`). |
| Translation proposal | 翻译提案 | 翻訳提案 | Propuesta de traducción | The translated-edition preview; never an approved translation. |
| Guide | 指南 | ガイド | Guía | A Home task guide. |
| Step | 步骤 | 手順 | Paso | One guide step. |
| Checklist | 检查清单 | チェックリスト | Lista de comprobación | A guide's status: steps are a checklist, not progress. |
| Retire (a character) | 停用 | 無効にする | Retirar | Removes a character from the active roster; never 引退. |
| Persona | 人物设定 | ペルソナ | Persona | The character description, separate from its voice. |
| Review state | 审阅状态 | レビュー状態 | Estado de revisión | Draft / approved / rejected. |
| Unreviewed | 未审阅 | 未レビュー | Sin revisar | Casting status before any review. |
| Shortlist | 入围 | 候補 | Preseleccionar | A casting review status. |
| Callback | 复试 | コールバック | Segunda prueba | A casting review status; not a phone call. |
| Reject | 拒绝 | 不採用 | Rechazar | A casting review status. |
| Trash | 回收站 | ゴミ箱 | Papelera | Where removed catalogue voices wait. |
| Research collection | 研究收藏 | リサーチコレクション | Colección de investigación | Organizes catalogue voices; it never changes assignments. |
| Tone | 语调 | トーン | Tono | A voice keyword. |
| Texture | 音质 | 質感 | Textura | A voice keyword. |
| Pronunciation | 发音 | 発音 | Pronunciación | The shared lexicon and per-line rules. |
| Line rule | 行规则 | 行ルール | Regla de línea | One occurrence rule for one line. |
| Unseeded | 未设置种子 | シード未設定 | sin semilla | A voice setting with no saved seed. |
| Audio (stage) | 音频 | 音声 | Audio | The stage label in Home. `Audio` is the Spanish word, so it is allowed to equal the English (KEEP_ENGLISH). |
| Take snapshot | 录音快照 | テイクのスナップショット | Instantánea de toma | A saved immutable copy of one take. |
| Purge | 永久删除 | 完全に削除 | Eliminar permanentemente | Removes an archive copy; never 删除 only. |
| Restore (a take) | 恢复 | 復元 | Restaurar | Explicit, dependency-checked selection of saved bytes. |
| Audition | 试听 | 試聴 | Escuchar | Listening without changing the selection. |
| Speech evidence | 语音证据 | 音声証拠 | Evidencia de voz | ASR/speech checks recorded for a take. |
| Drift | 漂移 | ドリフト | Deriva | Voice identity drift. |
| Delivery | 投递 | 配信 | Entrega | Sending an export to Audiobookshelf. |
| Upload | 上传 | アップロード | Carga | The HTTP upload step; the button stays 上传/アップロード/Enviar. |
| Verify (delivery) | 验证 | 検証 | Verificar | Byte-level comparison of the remote file. |
| Benchmark | 基准 | ベンチマーク | Comparativa de rendimiento | The Reports benchmark panel. |
| Export integrity | 导出完整性 | 書き出しの整合性 | Integridad de la exportación | The audio checks before publication. |
| Chapter-by-chapter export | 逐章导出 | 章ごとの書き出し | Exportación capítulo por capítulo | |
| Master | 母带 | マスター | Máster | A recorded master audio file. |
| Stem | 分轨 | ステム | Pista | A per-part recorded stem. |
| Rendition | 版本 | レンディション | Versión | One recorded master/stem provenance record. |
| Cue | 提示 | キュー | Señal | A sound placed at a dialogue anchor. |
| Mix | 混音 | ミックス | Mezcla | A full mixed-audio candidate. |
| Approval | 批准 | 承認 | Aprobación | Full-mix review approval, separate from delivery. |
| Epoch | 轮次 | エポック | Época | One training pass. Not a book chapter (`章`). |
| Loss | 损失 | 損失 | Pérdida | Training loss. |
| Learning rate | 学习率 | 学習率 | Tasa de aprendizaje | |
| Batch size | 批大小 | バッチサイズ | Tamaño de lote | |
| Gradient accumulation | 梯度累积 | 勾配累積 | Acumulación de gradiente | Grad accum steps. |
| LoRA rank | LoRA 秩 | LoRA ランク | rango de LoRA | `rank`, the matrix rank of the adapter. |
| LoRA alpha | LoRA Alpha 值 | LoRA Alpha 値 | Alpha de LoRA | Keeps the parameter name `Alpha`. |
| Promote (a candidate) | 提升 | 昇格 | Promover | Makes a trained candidate the production checkpoint. |
| Rollback (a promotion) | 回滚 | ロールバック | Revertir | Restores the pre-promotion checkpoint. |
| Blind review | 盲测审阅 | ブラインドレビュー | Revisión a ciegas | A/B listening with the identities hidden until submission. |
| Quality (stage) | 质检 | 品質チェック | Calidad | Voice Lab stage 1: warning-only clip audit. |
| Dedup | 去重 | 重複除去 | Deduplicación | Voice Lab stage 2. |
| Train (stage) | 训练 | トレーニング | Entrenamiento | Voice Lab stage 3; same word as the Training tab. |
| Evaluate (stage) | 评估 | 評価 | Evaluación | Voice Lab stage 4; also the Evaluation column. |
| Profile (stage) | 画像 | プロファイル | Perfil | Voice Lab stage 5 (acoustic + LLM description). An LLM connection **Profile** keeps 配置档案. |
| Name (stage) | 命名 | 命名 | Nombre | Voice Lab stage 6, renaming adapters. |

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

## P5 safety keys

| Key | English source |
| --- | --- |
| `safety.script.scope.discard` | Discard undo; keep current scope |
| `safety.script.roster.discard-undo` | Discard saved undo |
| `safety.script.roster.restore-draft` | Restore previous unsaved draft |
| `safety.script.roster.retire` | Retire selected |
| `safety.script.run.start-over` | Start over |
| `safety.script.run.start-over.title` | Discard saved progress for this text and begin again at chunk 1. |
| `safety.script.run.resume-failed` | Resume failed run |
| `safety.script.opds.discard` | Discard captured source |
| `safety.script.batch.collision.replace` | Back up and replace it |
| `safety.workbench.project.delete` | Delete project "{project}" and all its samples? |
| `safety.workbench.sample.delete` | Delete sample {index}? This removes its text and generated audio link and cannot be undone. |
| `safety.workbench.batch.regenerate` | Regenerate all {count} samples? |
| `safety.workbench.import.replace` | Import {count} samples into "{project}"? This will replace the {rows} rows and their generated audio links in this project. Continue? |

Markup safety keys render through `renderUiSafety`; the four Dataset Builder
confirms are JS `showConfirm()` calls and render through
`formatUiSafetyText()`, which is the same `tSafety()` contract in one string
because the confirm body is already `white-space: pre-line`. Confirm **titles**
and **action labels** stay plain translated keys: they name the control the
reader is choosing, not the consequence, and a two-line button label would be
unreadable. Every caller keeps `danger: true`.

## P6 safety keys

P6 carries the phase's destructive confirmations. All render through
`formatUiSafetyText()` (JS confirms) or `renderUiSafety()` (markup), so the
translation shows with the authored English beneath it.

| Key | English source |
| --- | --- |
| `safety.casting.replace_voices` | Replace voices for the selected characters in the active book? |
| `safety.casting.use_voice` | Use {voice} ({book}) for {speaker}? {speaker}'s current voice is kept as a saved candidate. |
| `safety.research.clear_marks.one` / `.many` | Clear {scope} on {count} selected voice(s)? Tags, status, priority, notes and research collections are kept. |
| `safety.research.delete_permanently.one` / `.many` | Permanently delete {count} selected voice(s) from the catalogue? This removes the catalogue entry only; shared audio, files and existing assigned books are preserved. Trashed voices are kept indefinitely until you delete them here. |
| `safety.roster.discard-edits` | Discard unsaved character-review edits? |
| `safety.roster.discard-undo` | Discard the saved exact character undo? Current characters and cast will stay. |
| `safety.roster.import-discard` | Discard this import and changes made after it? |
| `safety.pron.reload_discard` | Discard unsaved pronunciation edits and reload shared rules? |
| `safety.pron.save_shared` | Save these pronunciation rules for all books? Future renders will use them. Existing audio stays unchanged. |
| `safety.pron.line_discard` | Discard unsaved line rules and load the chosen line? |
| `safety.voice.cast_delete` | Delete cast "{cast}"? Cast-only members will be removed; shared characters will be kept. |
| `safety.voice.cast_member_remove` | Remove "{member}" from cast "{cast}"? |
| `safety.voice.delete_candidate` | Delete saved candidate {candidate} for {speaker}? This cannot be undone. |
| `safety.voice.clear_overrides` | {overrides}. Clear these so the suggested voice is used? Saved voice versions are kept. |
| `safety.voice.style_remove` | Remove the voice-style change for {name} from line {line}? Existing rendered audio is not changed. |
| `safety.voice.draft_recover_title` | Recover these voice edits?{content} |
| `safety.voice.draft_discard_title` | Discard this saved voice draft?{content} |
| `safety.voice.discard_all` | Discard all unsaved voice edits and reload the saved voices? |

The remaining P6 `safety.*` keys are warnings and partial-outcome messages
rather than confirmations: a changed book during a refresh (`book_changed_*`),
a superseded or unconfirmed save (`*_superseded`, `*_unconfirmed`), a partial
seed repair, a missing gender match, an unsupported homograph scan, and a
privacy/accuracy note in the Casting view. They also show the English beneath
the translation, because the risk they describe is a user who cannot read the
translation.

## P7 safety keys

P7 adds the Editor, Audio, Export, Publication, Reports and Home safety text.
Destructive confirms render through `formatUiSafetyText()` (JS confirms and the
native window confirm) or `renderUiSafety()` (markup and the home recovery
panel), so the authored English stays on screen beneath the translation.

| Key | English source |
| --- | --- |
| `safety.take.purge` / `.title` / `.action` | Permanently remove this snapshot? This frees its archive storage and cannot be undone. The selected line audio is kept. |
| `safety.core.editor.regenerate-all` / `.title` | Regenerate all {count} non-empty chunks? This will replace existing audio. |
| `safety.core.editor.regenerate-selection` / `.title` | Regenerate these {count} selected lines? Existing audio for this selection will be replaced. |
| `safety.core.editor.apply-gap` / `.title` | Set pause after this line to {pause} ms? Uses NARRATOR as the narrator. Speech, delivery instructions and takes stay unchanged. |
| `safety.core.export.preset.replace` / `.title` | Replace chapter filename preset "{name}"? Its saved template, padding and chapter selection will be replaced. |
| `safety.core.export.preset.delete` / `.title` | Delete chapter filename preset "{name}"? This cannot be undone unless you exported your presets. |
| `safety.sound.mixes.discard` / `.discard-confirm` | Permanently discard this saved mix and its approval? Original dialogue takes and imported sounds are kept. |
| `safety.sound.mixes.delivery-unverified` | Delivery response could not be verified. Check the saved output before retrying. |
| `safety.sound.mixes.source-required` | Source review is required; no delivery was published. |
| `safety.sound.mixes.consent` / `.consent-unavailable` | I reviewed all {pages} pages of this source comparison and want to deliver this exact text. |
| `safety.abs.rights` | I confirm this export uses only content and voices I have rights to share with my Audiobookshelf server |
| `safety.abs.send-outcome.accepted` / `.sent` / `.failed` / `.unknown` | Upload accepted; verify delivery. … Local export unchanged ({bytes} bytes, SHA-256 {sha}). |
| `safety.abs.verify` | After sending, choose Verify delivery to compare the remote file with this export. Until its bytes match, delivery stays unconfirmed and duplicate sends stay blocked. Rights confirmation does not override that check. |
| `safety.readalong.book-changed` | The book changed during this build. Nothing was downloaded; build again for the current book. |
| `safety.readalong.start-unconfirmed` | The build was not confirmed as started. Check the Result status before starting again. |
| `safety.home.recovery.*` | The interrupted-save and unreadable-recovery leads, with and without the terminal command `{steps}`. |
| `safety.home.status.*`, `safety.home.library.unavailable`, `safety.home.task.*`, `safety.home.collection.*` | Stale or partial home-workspace reads: displayed facts are from an earlier reading, nothing was loaded or switched. |

The remaining P7 `safety.*` keys are warnings rather than confirms: a changed
book during a render, a previous book's render finishing, an unconfirmed
Audacity export or outlier scan, an incomplete cleanup selection, a stale cue
or source revision, and a mix/cue history invalidated by a book switch.

## P8 safety keys

P8 adds the Training and LoRA-adapter safety text. Every one is a destructive
confirm (`danger: true` or its non-modal form) and renders through
`formatUiSafetyText()`, so the authored English stays on screen beneath the
translation. The action labels and confirm titles are plain translated keys:
they name the control, not the consequence.

| Key | English source |
| --- | --- |
| `safety.training.dataset.delete` | Delete dataset "{name}"? The uploaded ZIP and its samples are removed. This cannot be undone. |
| `safety.training.delete-adapter` | Delete this trained adapter? This cannot be undone. |
| `safety.training.delete-backup` | Permanently delete this rollback backup? You will no longer be able to restore the pre-promotion checkpoint. |
| `safety.training.promote` | Promote {candidate}? The current production checkpoint will be preserved for rollback. |
| `safety.training.rollback` | Restore the production checkpoint saved before promotion? The promoted candidate stops being production. |
| `safety.training.recover` | Recover production from the checkpoint saved before the interrupted operation? The files written by that operation are discarded. |
| `safety.training.review.clear` | Delete all human review history for this adapter? The decisions and notes are removed and cannot be recovered. |

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
