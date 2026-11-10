# API 参考

**声音保存迁移：**此前未绑定校验的 `/api/save_voice_config` 客户端必须迁移。编辑前先获取 `GET /api/voice_config/snapshot`，然后像当前 UI 一样，把 `{"revision": snapshot.revision, "book_token": snapshot.book_token, "voices": changes}` 发送到 `POST /api/voice_config/save`。下面基于原始映射的旧式示例，要求把两个来源快照值作为显式的 `revision` 和 `book_token` 查询参数。缺少校验值或身份过期会返回 409；重新加载并检查当前书后再重试。绝不要仅仅为了提交一个为更早的书创建的负载而去获取新的校验值。

[中文主页](Home-CN.md) · [English](API-Reference.md)

Web 界面做的每一件事都是一次 HTTP 调用，所以应用可以被脚本驱动。本页用示例展示常见任务。**全部 181 条路由的完整列表**由代码生成，见 README 的 [API 参考](../../README.md#api-reference)；运行中的应用在 `/docs` 有交互式文档，`/openapi.json` 是 schema。

## 基础

- **地址：**`http://127.0.0.1:<port>`。Pinokio 选择端口；Docker 使用 4200。下面的示例使用 `http://127.0.0.1:4200`。
- **认证：**默认关闭。如果你设置了 `ALEXANDRIA_AUTH_PASSWORD`，每个请求都需要 HTTP Basic 凭据——见 README 的 [Authentication](../../README.md#authentication-optional) 一节。
- **后台任务：**长任务在后台启动。轮询 `GET /api/status/<task>` 直到 `running` 为 false，或用 `GET /api/status/eta` 查看正在运行的任务的进度和剩余时间。

可以轮询的任务名：

| task | 由什么启动 |
|---|---|
| `script` | 生成脚本 |
| `review` / `batch_review` | 审阅一个脚本 / 多个脚本 |
| `nicknames` | 查找昵称 |
| `persona` | 生成人设 |
| `voices` | 推荐 LoRA 声音 |
| `audio` | 渲染音频 |
| `audacity_export` / `chapter_export` | 导出 |
| `drift_check` | Check Voices |
| `lora_training` / `lora_test` | 训练 / 测试声音适配器 |
| `voice_design` | Voice Designer 预览 |
| `dataset_builder` | Dataset Builder 批量生成 |
| `preparer` / `batch_preparer` | Preparer |
| `voicelab` | Voice Lab 流水线 |
| `benchmark` | 基准测试框架 |

## 设置

```bash
# read the current settings (API keys are redacted)
curl http://127.0.0.1:4200/api/config

# the default prompt texts
curl http://127.0.0.1:4200/api/default_prompts

# save settings (send the full settings object you read above, with your changes)
curl -X POST http://127.0.0.1:4200/api/config \
  -H "Content-Type: application/json" \
  -d @config.json
```

## 脚本生成

```bash
# upload a book; it becomes the active book
curl -X POST http://127.0.0.1:4200/api/upload -F "file=@mybook.txt"

# generate a script from the active book (all fields optional)
curl -X POST http://127.0.0.1:4200/api/generate_script \
  -H "Content-Type: application/json" \
  -d '{"first_person_narrator": null, "strip_front_matter": true, "start_over": false}'

# follow its progress
curl http://127.0.0.1:4200/api/status/script
curl http://127.0.0.1:4200/api/status/eta

# review the script (a second model pass that fixes common mistakes)
curl -X POST http://127.0.0.1:4200/api/review_script
curl http://127.0.0.1:4200/api/status/review

# the current script
curl http://127.0.0.1:4200/api/annotated_script
```

`generate_script` 总是处理当前书——最后上传的那本，或用 `POST /api/uploads/select` 选中的那本。它不接受文件名。

## 声音

在每个独立的保存示例之前，先读取 `/api/voice_config/snapshot`，再准备改动。把 shell 变量 `REVISION` 和 `BOOK_TOKEN` 设为该响应里的 `revision` 和 `book_token`。一次成功的保存会改变 revision；准备下一次改动前先检查新的快照，绝不要只为重试过期的改动而去取新快照。

```bash
# the speakers and their current voice settings
curl http://127.0.0.1:4200/api/voices

# save voice settings
curl -X POST "http://127.0.0.1:4200/api/save_voice_config?revision=${REVISION:?Set originating revision}&book_token=${BOOK_TOKEN:?Set originating book token}" \
  -H "Content-Type: application/json" \
  -d '{
    "NARRATOR": {"type": "custom", "voice": "Ryan", "character_style": "calm, measured narration"},
    "ELENA":    {"type": "clone", "ref_audio": "designed_voices/previews/preview_123.wav", "ref_text": "Hello there."},
    "MARCUS":   {"type": "lora", "adapter_id": "dark_voice_123", "adapter_path": "lora_models/dark_voice_123", "character_style": "menacing undertone"},
    "SOLDIER":  {"type": "design", "description": "Young strong soldier"}
  }'
```

### 声音设置字段

| 字段 | 用于 | 含义 |
|---|---|---|
| `type` | 全部 | `custom`、`builtin_lora`、`clone`、`lora`、`design` 或 `ensemble` |
| `voice` | custom | 内置声音名称（Aiden、Dylan、Eric……） |
| `character_style` | custom、lora | 随每一行发送的身份锚点 |
| `style_timeline` | custom、lora | 变化点：`{"from_index": N, "character_style": "..."}` |
| `seed` | 全部 | 随机 seed；`"-1"` 表示随机 |
| `ref_audio`、`ref_text` | clone | 参考音频及其确切文字稿 |
| `adapter_id`、`adapter_path` | lora | 适配器 |
| `description` | design | 基础声音描述 |
| `members` | ensemble | 一起说话的说话人 |
| `alias_of` | 任意 | 使用另一个说话人的声音 |

一个别名示例：

```bash
curl -X POST "http://127.0.0.1:4200/api/save_voice_config?revision=${REVISION:?Set originating revision}&book_token=${BOOK_TOKEN:?Set originating book token}" \
  -H "Content-Type: application/json" \
  -d '{
    "ELENA":       {"type": "clone", "ref_audio": "designed_voices/previews/preview_123.wav", "ref_text": "Hello there."},
    "YOUNG ELENA": {"type": "clone", "alias_of": "ELENA"}
  }'
```

渲染音频时，"YOUNG ELENA" 使用 ELENA 的声音。

## 人设

```bash
# generate personas for characters that have no voice yet
curl -X POST http://127.0.0.1:4200/api/generate_personas \
  -H "Content-Type: application/json" \
  -d '{"new_only": true}'

# large casts: process speakers in batches
curl -X POST http://127.0.0.1:4200/api/generate_personas \
  -H "Content-Type: application/json" \
  -d '{"advanced": true, "batch_size": 40}'

curl http://127.0.0.1:4200/api/status/persona
curl -X POST http://127.0.0.1:4200/api/cancel_persona
```

高级人设生成包含已确定的角色状态。`GET /api/voice_config/snapshot` 返回 `voices[].persona_states`，每一项带有服务器生成的 `version_id`、原始来源范围、该状态自身片段的 `segment_sha256` 指纹，以及 `current`（保存的版本是否就是脚本当前所读状态的人设）。生成的状态人设存放在 `config[speaker].versions[version_id]`，并带有 `persona_state` 来源信息，只有生成和恢复会设置它：保存可以保留它，但绝不会引入或改变它；形如 `state_<24 hex>` 的 id 是保留的。`POST /api/voices/{speaker}/versions/{id}/select` 拒绝状态版本；请通过声音时间线应用它们。

要重新生成某一个状态，向 `/api/generate_personas` POST `speaker`、`state_version` 和当前快照的 `book_token`；高级处理会自动开启。状态请求不能与年龄 override 组合。`POST /api/persona/recover` 接受相同的状态选择器/token，用于保存已校验的人设 JSON，并可选继续其预览。

候选的创建/选择/收藏以及批准/审计端点接受可选的 `version_id` 和 `book_token` 字段，以作用于某个状态版本。候选删除以查询参数接受它们。状态操作需要当前 token 和已校验的来源身份；省略版本选择器则保留旧的角色级行为。`POST /api/suggest_voices` 接受 `state_version`、`book_token`，以及 `characters` 中恰好一个角色，只用该状态的证据/人设来匹配。通过按版本限定的候选端点保存/选择这些建议。

状态卡表单的编辑使用现有的 revision 保护 `/api/voice_config/save`，保留父条目及其同级版本。`/api/voices/{speaker}/version_timeline` 仍是显式的应用步骤；生成的状态版本必须匹配其当前脚本和确切的分块边界。过期、缺失、歧义或已合并的边界会返回冲突，而不是猜测位置。

两个整配置保存端点也会拒绝无效的生成状态时间线边界，以及删除已应用的版本。保留已有过期时间线的普通编辑仍然支持。通用版本保存在覆盖某个状态 ID 时必须保留当前的生成来源信息；保存基础的新的手动副本不会复制生成状态的来源信息。


## 分块与渲染

```bash
# all chunks
curl http://127.0.0.1:4200/api/chunks

# edit a chunk (any of text, instruct, speaker, pause_after)
curl -X POST http://127.0.0.1:4200/api/chunks/5 \
  -H "Content-Type: application/json" \
  -d '{"text": "Updated dialogue", "instruct": "Excited, bright energy."}'

# render one chunk
curl -X POST http://127.0.0.1:4200/api/chunks/5/generate

# render several: batched (the fast path used with local TTS)
curl -X POST http://127.0.0.1:4200/api/generate_batch_fast \
  -H "Content-Type: application/json" \
  -d '{"indices": [0, 1, 2, 3, 4]}'

# render several: one request per line, in parallel
curl -X POST http://127.0.0.1:4200/api/generate_batch \
  -H "Content-Type: application/json" \
  -d '{"indices": [0, 1, 2, 3, 4]}'

curl http://127.0.0.1:4200/api/status/audio

# merge into the finished audiobook
curl -X POST http://127.0.0.1:4200/api/merge
```

## 已保存脚本

```bash
curl http://127.0.0.1:4200/api/scripts

curl -X POST http://127.0.0.1:4200/api/scripts/save \
  -H "Content-Type: application/json" -d '{"name": "my-novel"}'

curl -X POST http://127.0.0.1:4200/api/scripts/load \
  -H "Content-Type: application/json" -d '{"name": "my-novel"}'
```

## Voice Designer

```bash
# preview a voice from a description
curl -X POST http://127.0.0.1:4200/api/voice_design/preview \
  -H "Content-Type: application/json" \
  -d '{"description": "A warm, deep male voice with a calm and steady tone", "sample_text": "Hello, how are you?"}'

# save a preview as a designed voice
curl -X POST http://127.0.0.1:4200/api/voice_design/save \
  -H "Content-Type: application/json" \
  -d '{"name": "warm_narrator", "description": "A warm, deep male voice", "sample_text": "Hello.", "preview_file": "designed_voices/previews/preview_123.wav"}'

curl http://127.0.0.1:4200/api/voice_design/list
curl -X DELETE http://127.0.0.1:4200/api/voice_design/<voice_id>
```

## 声音（LoRA）训练

```bash
# upload a dataset: a ZIP of WAV files plus metadata.jsonl
curl -X POST http://127.0.0.1:4200/api/lora/upload_dataset -F "file=@dataset.zip"

curl http://127.0.0.1:4200/api/lora/datasets
curl -X DELETE http://127.0.0.1:4200/api/lora/datasets/<dataset_id>

# train (the values shown are the defaults; see the Training guide before changing lr)
curl -X POST http://127.0.0.1:4200/api/lora/train \
  -H "Content-Type: application/json" \
  -d '{
    "name": "soldier_voice",
    "dataset_id": "gruff_soldier",
    "epochs": 5,
    "lr": 1e-6,
    "lora_r": 32,
    "lora_alpha": 128,
    "batch_size": 1,
    "gradient_accumulation_steps": 8,
    "language": "english"
  }'

curl http://127.0.0.1:4200/api/status/lora_training
curl http://127.0.0.1:4200/api/lora/models

# test a trained adapter
curl -X POST http://127.0.0.1:4200/api/lora/test \
  -H "Content-Type: application/json" \
  -d '{"adapter_id": "soldier_voice_1234567890", "text": "Moving to position.", "instruct": "Tense, whispering."}'

curl -X DELETE http://127.0.0.1:4200/api/lora/models/<adapter_id>
```

不要把 `lr` 提高到 5e-6：以该学习率训练出的声音永远不会停止说话。见[训练指南](Training-Guide-CN.md)。

## Dataset Builder

```bash
curl http://127.0.0.1:4200/api/dataset_builder/list

curl -X POST http://127.0.0.1:4200/api/dataset_builder/create \
  -H "Content-Type: application/json" -d '{"name": "my_voice_dataset"}'

# the description and global seed
curl -X POST http://127.0.0.1:4200/api/dataset_builder/update_meta \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "description": "A warm male narrator", "global_seed": "42"}'

# the sample rows
curl -X POST http://127.0.0.1:4200/api/dataset_builder/update_rows \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "rows": [{"text": "Hello world.", "emotion": "cheerful"}]}'

# render one sample
curl -X POST http://127.0.0.1:4200/api/dataset_builder/generate_sample \
  -H "Content-Type: application/json" \
  -d '{"dataset_name": "my_voice_dataset", "sample_index": 0, "description": "A warm male voice, cheerful", "text": "Hello world.", "seed": -1}'

# render several samples in the background
curl -X POST http://127.0.0.1:4200/api/dataset_builder/generate_batch \
  -H "Content-Type: application/json" \
  -d '{"name": "my_voice_dataset", "description": "A warm male voice", "samples": [{"text": "Hello.", "emotion": "cheerful"}]}'

curl http://127.0.0.1:4200/api/dataset_builder/status/my_voice_dataset
curl -X POST http://127.0.0.1:4200/api/dataset_builder/cancel

# save as a training dataset; ref_index picks the reference sample
curl -X POST http://127.0.0.1:4200/api/dataset_builder/save \
  -H "Content-Type: application/json" -d '{"name": "my_voice_dataset", "ref_index": 0}'

curl -X DELETE http://127.0.0.1:4200/api/dataset_builder/my_voice_dataset
```

## 下载与导出

```bash
# the merged audiobook
curl http://127.0.0.1:4200/api/audiobook --output audiobook.mp3

# Audacity: start the export, wait, then download the zip
curl -X POST http://127.0.0.1:4200/api/export_audacity
curl http://127.0.0.1:4200/api/status/audacity_export
curl http://127.0.0.1:4200/api/export_audacity --output audacity_export.zip
```

## 用 Python 跑整本书

```python
import requests, time

BASE = "http://127.0.0.1:4200"

def wait_for(task):
    while requests.get(f"{BASE}/api/status/{task}").json().get("running"):
        time.sleep(5)

with open("mybook.txt", "rb") as f:
    requests.post(f"{BASE}/api/upload", files={"file": f})
requests.post(f"{BASE}/api/generate_script", json={})
wait_for("script")

snapshot_response = requests.get(f"{BASE}/api/voice_config/snapshot")
snapshot_response.raise_for_status()
snapshot = snapshot_response.json()
save_response = requests.post(f"{BASE}/api/save_voice_config", params={
    "revision": snapshot["revision"], "book_token": snapshot["book_token"],
}, json={
    "NARRATOR": {"type": "custom", "voice": "Ryan", "character_style": "calm narrator"},
    "HERO": {"type": "lora", "adapter_id": "hero_voice_123", "adapter_path": "lora_models/hero_voice_123"},
})
save_response.raise_for_status()

chunks = requests.get(f"{BASE}/api/chunks").json()
requests.post(f"{BASE}/api/generate_batch_fast", json={"indices": [c["id"] for c in chunks]})
wait_for("audio")

integrity = requests.get(f"{BASE}/api/editor/integrity")
integrity.raise_for_status()
integrity = integrity.json()
if integrity['status'] != 'verified':
    print(integrity)  # inspect differences or the unavailable-source reason
    if input('Continue without a verified source match? [yes/no] ') != 'yes':
        raise SystemExit('Merge declined')
response = requests.post(f"{BASE}/api/merge", json={
    'integrity_confirmation': integrity['snapshot']
})
response.raise_for_status()  # changed source/book/chunks require a fresh check
wait_for("audio")
with open("audiobook.mp3", "wb") as f:
    f.write(requests.get(f"{BASE}/api/audiobook").content)
```

## 用 JavaScript 做同样的事

```javascript
const BASE = "http://127.0.0.1:4200";

async function waitFor(task) {
  while ((await (await fetch(`${BASE}/api/status/${task}`)).json()).running) {
    await new Promise(r => setTimeout(r, 5000));
  }
}

const form = new FormData();
form.append("file", fileInput.files[0]);
await fetch(`${BASE}/api/upload`, { method: "POST", body: form });
await fetch(`${BASE}/api/generate_script`, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: "{}"
});
await waitFor("script");

const snapshotResponse = await fetch(`${BASE}/api/voice_config/snapshot`);
if (!snapshotResponse.ok) { throw new Error('Voice snapshot failed'); }
const snapshot = await snapshotResponse.json();
const guards = new URLSearchParams({ revision: snapshot.revision, book_token: snapshot.book_token });
const saveResponse = await fetch(`${BASE}/api/save_voice_config?${guards}`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ NARRATOR: { type: "custom", voice: "Ryan", character_style: "calm" } })
});
if (!saveResponse.ok) { throw new Error('Voice save refused; review the current book before retrying'); }

const chunks = await (await fetch(`${BASE}/api/chunks`)).json();
await fetch(`${BASE}/api/generate_batch_fast`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ indices: chunks.map(c => c.id) })
});
await waitFor("audio");

const integrityResponse = await fetch(`${BASE}/api/editor/integrity`);
if (!integrityResponse.ok) { throw new Error('Source check failed'); }
const integrity = await integrityResponse.json();
if (integrity.status !== 'verified') {
  console.log(integrity); // inspect differences or the unavailable-source reason
  if (!window.confirm('Continue without a verified source match?')) {
    throw new Error('Merge declined');
  }
}
const mergeResponse = await fetch(`${BASE}/api/merge`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ integrity_confirmation: integrity.snapshot })
});
if (!mergeResponse.ok) { throw new Error('Merge refused; check the current snapshot again'); }
```

`POST /api/merge` 只有在当前编辑器文本与选中的原始来源匹配时才接受无请求体的请求。存在差异或来源不可用会返回 HTTP 409。检查 `GET /api/editor/integrity` 并显式确认它当前的 `snapshot` 才能继续；服务器在导出前会重新检查。没有原始来源的已保存脚本 JSON 书籍仍为不可用状态，每次合并都需要确认。这项检查比较的是文本，不是 WAV 里的词。


### 可选的生成语音校验

通过配置 API 保存 `tts.speech_verification: {"enabled": true}`，以检查之后的生成录音。它默认关闭。初始覆盖范围是英语单声道音频，使用缓存的 Linux CPU whisper.cpp Small.en 运行时，以及一个独立的 Large-v3 检查点做更强的确认。不会自动下载任何东西。缺少资源、不支持的输入和有限的证据上限会产生明确的不可用结果。检查会增加 CPU 时间和内存；ASR 和短行的声学证据仍然需要试听审阅。

该检查把完成编码的录音与合成 API 观察到确切文本比对，包括发音替换。确认存在实质性的缺失或多余段落时，会拒绝这次新尝试，同时保留之前选中的录音。其他差异保留为审阅证据。脚本里有意重复的内容以及有歧义的数值/发音区域会保留。

只有当输入、UID、书和选中的录音仍然匹配时，`GET /api/chunks` 和 `/api/chunks/status` 才会暴露有界的 `speech_verification` 元数据。一次快速的文件版本检查会隐藏被替换或缺失的音频，而无需对整本书做哈希/解码。`speech_verification_failure` 把被拒绝的尝试与选中的录音分开描述。`/api/chunks/quality` 暴露语音审阅类别。不完整或不可用的复查会记录在 `speech_verification_recheck` 中，不会抹掉未变的选中录音已完成的证据。这些读取不会启动检查。Editor 检查器显示确切的输入、计数、诊断摘录、开销、校验器身份以及任何缩写。

要复查某一次已保存的录音，用当前分块快照里的 `expected_book_token`、`expected_uid` 和 `expected_audio_revision` 向 `POST /api/chunks/{index}/speech_verification` 发送请求。该录音必须有当前记录的合成输入和完成音频哈希；更旧或未经证明的录音需要在开启检查的情况下重新生成。CPU 任务会对选中的音频做哈希，运行一次新的有界校验，并且只在书、行、输入、录音和字节都未改变时条件性地保存结果。它返回一个精简的 `receipt` 和一份有界的新 `report`。即使发现内容缺陷，复查也会保留选中的音频。不会创建单独的历史报告缓存。

用 `POST /api/speech_verification/cancel` 请求取消，用 `GET /api/status/speech_verification` 查看任务状态。在提供方的子进程和私有临时文件清理完毕之前，占用权会一直保留，包括发起请求的 HTTP 连接断开之后。CPU ASR 与词级时间共享一个由 close 持有的私有租约，所以提供方繁忙时会报告不可用。

Editor 的撤销使用 `DELETE /api/chunks/{index}` 返回的 `undo_token`。在 60 秒内把该 token、返回的 `deleted` 对象作为 `chunk`，以及原始的 `at_index` 发送到 `POST /api/chunks/restore`。一个回执只能在同一活动书籍版本中恢复它原本的那次删除，且只能一次；过期、被修改或重复的请求返回 409。

批量上传的调用方可以用 `POST /api/upload?select_active=false` 保存来源而不改变当前书。普通上传默认会选中它。

`DELETE /api/voices/{speaker}/versions/{version_id}?book_token=...` 只删除已保存的版本。它要求当前的书/来源身份，并拒绝活动的版本或被已应用时间线引用的版本。音频文件、基础设置和同级版本都会保留。
