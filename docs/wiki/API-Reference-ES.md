# Referencia de la API

[English](API-Reference.md) · [日本語](API-Reference-JA.md) · [中文](API-Reference-CN.md) · Español

**Migración del guardado de voces:** los clientes de `/api/save_voice_config` que antes no iban vinculados deben migrar.
Antes de editar, captura `GET /api/voice_config/snapshot` y luego envía
`{"revision": snapshot.revision, "book_token": snapshot.book_token, "voices": changes}`
a `POST /api/voice_config/save`, como hace la interfaz actual. Los ejemplos heredados de mapa
en bruto de abajo requieren ambos valores de la instantánea de origen como parámetros de consulta explícitos
`revision` y `book_token`. La falta de guardas o una identidad obsoleta devuelven 409;
recarga y revisa el libro actual antes de reintentar. Nunca obtengas guardas nuevas
solo para enviar una carga creada para un libro anterior.

Todo lo que hace la interfaz web es una llamada HTTP, así que la app se puede programar. Esta página muestra
las tareas habituales con ejemplos. La **lista completa de las 181 rutas**, generada a partir del
código, está en la [referencia de la API](../../README_ES.md#referencia-de-la-api) del README; la documentación interactiva está
en `/docs` y el esquema OpenAPI en `/openapi.json` en una app en ejecución.

## Fundamentos

- **Dirección:** `http://127.0.0.1:<port>`. Pinokio elige el puerto; Docker usa 4200. Los
  ejemplos de abajo usan `http://127.0.0.1:4200`.
- **Autenticación:** desactivada por defecto. Si defines `ALEXANDRIA_AUTH_PASSWORD`, cada solicitud
  necesita credenciales HTTP Basic; ver la sección
  [Autenticación](../../README_ES.md#autenticación-opcional) del README.
- **Tareas en segundo plano:** los trabajos largos empiezan en segundo plano. Sondea
  `GET /api/status/<task>` hasta que `running` sea false, o `GET /api/status/eta` para el progreso y el
  tiempo restante de lo que esté en ejecución.

Nombres de tarea que puedes sondear:

| tarea | la inicia |
|---|---|
| `script` | generar un guion |
| `review` / `batch_review` | revisar un guion / varios |
| `nicknames` | encontrar apodos |
| `persona` | generar personas |
| `voices` | sugerir voces LoRA |
| `audio` | renderizar audio |
| `audacity_export` / `chapter_export` | exportaciones |
| `drift_check` | Check Voices |
| `lora_training` / `lora_test` | entrenar / probar un adaptador de voz |
| `voice_design` | vistas previas de Voice Designer |
| `dataset_builder` | generación por lotes del Dataset Builder |
| `preparer` / `batch_preparer` | el Preparer |
| `voicelab` | el pipeline de Voice Lab |
| `benchmark` | el banco de pruebas de benchmarks |

## Ajustes

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

## Generación de guion

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

`generate_script` trabaja siempre sobre el libro activo: el último subido, o uno elegido con
`POST /api/uploads/select`. No toma un nombre de archivo.

## Voces

Antes de cada ejemplo de guardado independiente, lee `/api/voice_config/snapshot` antes
de preparar las ediciones. Pon las variables de shell `REVISION` y `BOOK_TOKEN` con el
`revision` y el `book_token` de esa respuesta. Un guardado correcto cambia la revisión;
revisa una instantánea nueva antes de preparar otra edición, nunca solo para reintentar ediciones obsoletas.

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

### Campos de los ajustes de voz

| campo | lo usa | significado |
|---|---|---|
| `type` | todos | `custom`, `builtin_lora`, `clone`, `lora`, `design` o `ensemble` |
| `voice` | custom | el nombre de la voz integrada (Aiden, Dylan, Eric, …) |
| `character_style` | custom, lora | el ancla de identidad que se envía con cada línea |
| `style_timeline` | custom, lora | puntos de cambio: `{"from_index": N, "character_style": "..."}` |
| `seed` | todos | semilla aleatoria; `"-1"` para aleatoria |
| `ref_audio`, `ref_text` | clone | el audio de referencia y su transcripción exacta |
| `adapter_id`, `adapter_path` | lora | el adaptador |
| `description` | design | la descripción de la voz base |
| `members` | ensemble | los hablantes que hablan juntos |
| `alias_of` | cualquiera | usar la voz de otro hablante |

Un alias:

```bash
curl -X POST "http://127.0.0.1:4200/api/save_voice_config?revision=${REVISION:?Set originating revision}&book_token=${BOOK_TOKEN:?Set originating book token}" \
  -H "Content-Type: application/json" \
  -d '{
    "ELENA":       {"type": "clone", "ref_audio": "designed_voices/previews/preview_123.wav", "ref_text": "Hello there."},
    "YOUNG ELENA": {"type": "clone", "alias_of": "ELENA"}
  }'
```

Cuando se renderiza audio, "YOUNG ELENA" usa la voz de ELENA.

## Personas

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

La generación avanzada de personas incluye los estados de personaje consolidados. `GET /api/voice_config/snapshot` devuelve `voices[].persona_states`, cada uno con un `version_id` generado por el servidor, el rango de origen original, una huella `segment_sha256` del segmento propio de ese estado, y `current` (si la versión guardada es la persona del estado tal como se lee ahora en el guion). Las personas de estado generadas viven en `config[speaker].versions[version_id]` y llevan procedencia `persona_state`, que solo fijan la generación y la recuperación: un guardado puede conservarla pero nunca introducirla ni cambiarla, y los ids de la forma `state_<24 hex>` están reservados. `POST /api/voices/{speaker}/versions/{id}/select` rechaza las versiones de estado; aplícalas en la línea de tiempo de voz.

Para regenerar un estado, haz POST a `/api/generate_personas` con `speaker`, `state_version` y el `book_token` de la instantánea actual; el procesamiento avanzado se activa automáticamente. Una solicitud de estado no puede combinar una anulación de edad. `POST /api/persona/recover` acepta el mismo selector de estado/token para guardar un JSON de persona validado y, opcionalmente, reanudar su vista previa.

Los endpoints de creación/selección/favorito de candidato y de aprobación/auditoría aceptan campos opcionales `version_id` y `book_token` para actuar sobre una versión de estado. El borrado de candidatos los acepta como parámetros de consulta. Las acciones de estado requieren el token actual y una identidad de origen validada; los selectores de versión omitidos conservan el comportamiento heredado a nivel de personaje. `POST /api/suggest_voices` acepta `state_version`, `book_token` y exactamente un personaje en `characters` para emparejar usando solo la evidencia/persona de ese estado. Guarda/selecciona esas sugerencias mediante los endpoints de candidato con ámbito de versión.

Las ediciones del formulario de una tarjeta de estado usan el `/api/voice_config/save` existente protegido por revisión, conservando la entrada padre y sus versiones hermanas. `/api/voices/{speaker}/version_timeline` sigue siendo un paso de aplicación explícito; las versiones de estado generadas deben coincidir con su guion actual y con un límite de fragmento exacto. Los límites obsoletos, ausentes, ambiguos o combinados devuelven un conflicto en lugar de adivinar una ubicación.

Ambos endpoints de guardado de configuración completa también rechazan límites de línea de tiempo de estado generado inválidos y la eliminación de una versión aplicada. Las ediciones ordinarias que conservan una línea de tiempo obsoleta existente siguen admitiéndose. Los guardados de versión genéricos deben conservar la procedencia generada actual al sobrescribir un ID de estado; guardar una nueva copia manual de la base no copia la procedencia de estado generado.


## Fragmentos y renderizado

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

## Guiones guardados

```bash
curl http://127.0.0.1:4200/api/scripts

curl -X POST http://127.0.0.1:4200/api/scripts/save \
  -H "Content-Type: application/json" -d '{"name": "my-novel"}'

curl -X POST http://127.0.0.1:4200/api/scripts/load \
  -H "Content-Type: application/json" -d '{"name": "my-novel"}'

# read-only: an interrupted save or switch, set-aside evidence, and the
# recovery commands (the app shows the same summary on its Home page)
curl http://127.0.0.1:4200/api/book/recovery
```

`GET /api/book/recovery` no cambia nada. Informa de `status` (`clean`, `pending`,
`migration_required` o `unreadable`), la fase del diario, si otra operación tiene
el bloqueo del libro, las carpetas separadas y el estado de su recibo, los restos de fallo como recuentos y
bytes, y los comandos a ejecutar en orden. Ver [Guardados y cambios de libro
interrumpidos](../../docs/guides/BOOK_SWITCH_RECOVERY_ES.md).

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

## Entrenamiento de voz (LoRA)

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

No subas `lr` a 5e-6: las voces entrenadas con esa tasa nunca dejan de hablar. Ver la
[Guía de entrenamiento](Training-Guide-ES.md).

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

## Descargas y exportación

```bash
# the merged audiobook
curl http://127.0.0.1:4200/api/audiobook --output audiobook.mp3

# Audacity: start the export, wait, then download the zip
curl -X POST http://127.0.0.1:4200/api/export_audacity
curl http://127.0.0.1:4200/api/status/audacity_export
curl http://127.0.0.1:4200/api/export_audacity --output audacity_export.zip
```

## Un libro entero, desde Python

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

## Lo mismo, desde JavaScript

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

`POST /api/merge` acepta una solicitud sin cuerpo solo cuando el texto actual del editor
coincide con la fuente original seleccionada. Las diferencias o una procedencia no disponible
devuelven HTTP 409. Inspecciona `GET /api/editor/integrity` y confirma explícitamente su
`snapshot` actual para continuar; el servidor lo vuelve a comprobar antes de exportar. Un libro
guardado como script-JSON sin su fuente original sigue no disponible y requiere
confirmación en cada combinación. Esta comprobación compara texto, no las palabras de los WAV.


### Verificación opcional del habla generada

Guarda `tts.speech_verification: {"enabled": true}` mediante la API de configuración
para comprobar las tomas generadas posteriores. Está desactivado por defecto. La cobertura inicial es
audio mono en inglés usando un runtime cacheado de whisper.cpp Small.en en CPU Linux y
un checkpoint Large-v3 distinto para una confirmación más fuerte. Nada se descarga
automáticamente. Los recursos ausentes, las entradas no admitidas y los límites finitos de evidencia
producen resultados no disponibles explícitos. Las comprobaciones añaden tiempo de CPU y RAM; el ASR y
la evidencia acústica de líneas cortas siguen necesitando revisión por escucha.

La comprobación compara la toma codificada terminada con el texto exacto observado en
la API de síntesis, incluidas las sustituciones de pronunciación. Un pasaje material confirmado que falta o sobra
rechaza el nuevo intento y conserva la toma seleccionada anterior. Otras discrepancias quedan como evidencia para revisión.
Las repeticiones declaradas y las regiones numéricas/de pronunciación ambiguas se conservan.

`GET /api/chunks` y `/api/chunks/status` exponen metadatos `speech_verification` acotados
solo cuando la entrada, el UID, el libro y la toma seleccionada siguen coincidiendo. Una comprobación rápida
de versión de archivo oculta el audio reemplazado o ausente sin calcular el hash ni decodificar un
libro entero. `speech_verification_failure` describe un intento rechazado por separado
de la toma seleccionada. `/api/chunks/quality` expone categorías de revisión del habla.
Una nueva comprobación incompleta o no disponible se registra en `speech_verification_recheck`
sin borrar la evidencia completada de la toma seleccionada sin cambios.
Estas lecturas no inician una comprobación. El inspector del Editor muestra la entrada exacta,
los recuentos, extractos de diagnóstico, costes, identidades del verificador y cualquier abreviatura.

Para volver a comprobar una toma guardada, envía `POST /api/chunks/{index}/speech_verification`
con `expected_book_token`, `expected_uid` y `expected_audio_revision` de
la instantánea actual del fragmento. La toma debe tener una entrada de síntesis registrada actual
y un hash del audio terminado; las tomas anteriores o sin prueba requieren regeneración con
las comprobaciones activadas. La tarea de CPU calcula el hash del audio seleccionado, ejecuta una verificación acotada
nueva y guarda el resultado de forma condicional solo si el libro, la fila, las entradas,
la toma y los bytes siguen sin cambios. Devuelve un `receipt` compacto y un `report` nuevo
acotado. Volver a comprobar conserva el audio seleccionado incluso cuando encuentra un
defecto de contenido. No se crea una caché de informes históricos separada.

Usa `POST /api/speech_verification/cancel` para solicitar la cancelación y
`GET /api/status/speech_verification` para inspeccionar el estado de la tarea. La reserva (claim) sigue siendo de su dueño hasta que los descendientes del proveedor y el scratch privado se hayan limpiado,
incluso después de que la solicitud HTTP iniciadora se desconecte. El ASR de CPU comparte un
arrendamiento privado de cierre propio con el tiempo de palabra, así que un proveedor ocupado informa no disponible.

El Deshacer del Editor usa el `undo_token` devuelto por `DELETE /api/chunks/{index}`.
Envía ese token, el objeto `deleted` devuelto como `chunk` y el `at_index` original
a `POST /api/chunks/restore` en 60 segundos. Un recibo puede restaurar
solo su borrado original en la misma generación de libro activa, una vez; las solicitudes obsoletas,
modificadas o repetidas devuelven 409.

Los clientes de subida por lotes pueden usar `POST /api/upload?select_active=false` para guardar una
fuente sin cambiar el libro activo. Las subidas ordinarias lo seleccionan por defecto.

DELETE `/api/voices/{speaker}/versions/{version_id}?book_token=...` quita solo la versión guardada. Requiere la identidad actual de libro/fuente y se niega con una versión activa o una referenciada por la línea de tiempo aplicada. Los archivos de audio, los ajustes base y las versiones hermanas se conservan.
