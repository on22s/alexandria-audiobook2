[English](PREPARER_GUIDE.md) | [中文](PREPARER_GUIDE_CN.md) | [日本語](PREPARER_GUIDE_JA.md) | [Español](PREPARER_GUIDE_ES.md)

# Guía del Preparer de Alexandria

Convierte un solo audiolibro en un conjunto de datos de entrenamiento TTS anotado con un solo comando.

## Resumen

El preparer toma un archivo de audio (audiolibro) y produce un archivo ZIP que contiene:
- Segmentos de audio alineados por palabra (`sample_NNNN.wav` a 24 kHz)
- `metadata.jsonl` con anotaciones de texto, duraciones y rangos de tiempo
- Listo para usarse como conjunto de datos de entrenamiento TTS

# Guía del Preparer de Alexandria

## Qué hace esta herramienta (versión muy sencilla)

El Preparer convierte un audiolibro (archivo de audio) en un conjunto de datos de entrenamiento para el motor TTS de Alexandria. Hace tres cosas:

1. **Escucha el audio** y escribe cada palabra con marcas de tiempo exactas (lo que se llama "transcripción" o "ASR")
2. **Opcionalmente compara las palabras con el libro original** para que el texto use la grafía del libro (no lo que la IA oyó mal)
3. **Corta el audio en fragmentos pequeños** (de unos 10 segundos cada uno) y los guarda como archivos WAV con un archivo de metadatos

La salida es un archivo `.zip` que puedes alimentar a la pestaña LoRA Training de Alexandria para enseñar al motor TTS una voz nueva.

### ¿Para quién es esto?

- **Tienes un audiolibro** (archivo WAV) y quieres clonar una voz
- **Quieres entrenar una voz personalizada** con la pestaña Training
- **No necesitas esto** si solo usas Alexandria para generar audiolibros a partir de libros: la interfaz web lo hace todo automáticamente

---

## Inicio rápido para quienes no programan

### Requisitos previos

Antes de ejecutar el Preparer, asegúrate de que:

1. **Tienes Pinokio instalado** y Alexandria está instalado en Pinokio
2. **Tienes un archivo WAV de audiolibro**: si tienes un MP3, conviértelo primero a WAV (puedes usar un conversor en línea o Audacity)
3. **Tienes el archivo del libro original** (.epub o .txt): es opcional pero muy recomendable para obtener mejores resultados

### Paso 1: abre una terminal

- **Windows:** pulsa `Win + R`, escribe `cmd` y pulsa Intro
- **Mac:** abre Spotlight (Cmd + Space), escribe `terminal` y pulsa Intro
- **Linux:** abre el menú de aplicaciones y busca "Terminal"

### Paso 2: ve a la carpeta de Alexandria

Escribe este comando y pulsa Intro:

```bash
cd ~/.pinokio/api/alexandria-audiobook.git
```

Si instalaste Alexandria en otra ubicación, usa esa ruta en su lugar.

### Paso 3: ejecuta el Preparer

Copia y pega este comando (sustituye las rutas por las de tus archivos):

```bash
./app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "path/to/your/audiobook.wav" \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --source "path/to/your/book.epub"
```

**Qué significa cada parte:**
- `./app/env/bin/python`: es el Python que viene con Alexandria (no uses el Python de tu sistema)
- `--audio`: la ruta a tu archivo WAV de audiolibro
- `--model`: el modelo de IA que etiqueta cada fragmento con hablante e indicaciones de voz
- `--source`: el archivo del libro original (opcional, pero mejora la precisión)

### Paso 4: espera

El preparer hará lo siguiente:
1. Transcribir el audio (tarda ~1 hora por cada 5 horas de audio)
2. Comparar las palabras con el libro (si indicaste `--source`)
3. Cortar en fragmentos y guardar el ZIP

Cuando termine, verás:
```
✓ Dataset written to: alexandria_dataset.zip
```

### Paso 5: usa la salida

1. Descomprime `alexandria_dataset.zip`: contiene archivos WAV y un archivo `metadata.jsonl`
2. En la interfaz web de Alexandria, ve a la pestaña **Dataset**
3. Sube el archivo `metadata.jsonl` y los archivos WAV
4. Úsalo para entrenar una voz personalizada en la pestaña **Training**

---

## Qué hace esta herramienta (versión muy sencilla)

Imagina que tienes un audiolibro largo: alguien leyendo una novela entera en voz alta,
quizá 60 minutos o 6 horas. Para enseñar a un ordenador a hablar como ese
narrador, necesitas muchísimos **clips cortos** de su voz,
cada uno etiquetado con **las palabras exactas que dijo**. Esta herramienta crea esos
clips por ti, automáticamente.

Piénsalo así:

1. **Le das el audiolibro** (un archivo de audio grande).
2. **La herramienta escucha** y averigua dónde empieza y acaba cada palabra.
3. **Corta el audiolibro en clips del tamaño de un aperitivo**, de unos 10 segundos cada uno,
   intentando cortar en pausas naturales (como al final de una frase) para que cada
   clip no acabe a mitad de palabra.
4. **Para cada clip, escribe las palabras** que se dijeron en él.
5. **Un pequeño asistente de IA añade pistas** a las palabras, como marcar las
   *importantes*, o dónde hizo el narrador una pausa larga `...`
6. **Recibes un archivo zip** lleno de clips + una lista de lo que dice cada clip.

Ese zip es lo que alimentas después a un programa de entrenamiento TTS, para que el ordenador
aprenda a copiar la voz de ese narrador.

### El gran truco nuevo: usar el libro

También puedes darle a la herramienta **el libro real** (el archivo EPUB o de texto).

¿Por qué? Porque la IA que escucha (la parte que convierte audio en palabras) comete
errores, sobre todo con nombres de personajes. Puede escribir "**Yudy**" cuando el
narrador dijo "**Yurie**", o "**Kudou**" puede salir como "**Coodo**".

Cuando le das el libro, la herramienta puede **buscar la grafía correcta** de
cada clip. Como tener la hoja de respuestas junto a los deberes: la IA que escucha
oye el audio, luego la herramienta comprueba el libro y dice "ah, ese trozo dice
'Miyo Saimori', voy a usar esa grafía en lugar de lo que había adivinado".

También puede **descartar los clips que no están en el libro**, como cuando el
narrador dice "Audible presents…" o lee el anuncio del capítulo en voz alta,
o cualquier otro trozo que no coincida con la historia real.

## El ejemplo más sencillo posible (copiar y pegar)

### Solo convertir un audiolibro en clips (sin archivo de libro)

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "/home/fakemitch/Desktop/audiobook.wav" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf
```

Qué hace esto, en palabras sencillas:
- Lee el audiolibro en `/home/fakemitch/Desktop/audiobook.wav`.
- Usa el asistente de IA de `models/Qwen2.5-14B-Instruct-Q6_K.gguf` para etiquetar los clips.
- Guarda el resultado como `alexandria_dataset.zip` en la carpeta actual.

### Ahora haz lo mismo pero usando también el libro (recomendado)

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "/home/fakemitch/Desktop/audiobook.wav" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf \
  --source "/home/fakemitch/Desktop/books/My Happy Marriage - Volume 01.epub"
```

La única diferencia: esa última línea le dice a la herramienta dónde está el libro. Los nombres
saldrán bien escritos, y lo que solo existe en el audio (créditos, introducciones) se
descartará.

### "Quiero darte el libro pero, por favor, no descartes nada"

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "/home/fakemitch/Desktop/audiobook.wav" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf \
  --source "/home/fakemitch/Desktop/books/book.epub" \
  --keep-unaligned
```

El flag `--keep-unaligned` significa "si un clip no coincide con el libro, no pasa
nada: usa la mejor conjetura de la IA que escucha y conserva el clip de todos modos".

### "He perdido la cuenta de por dónde iba: empieza de nuevo"

Simplemente no uses `--resume`. Cada ejecución empieza de cero por defecto. Si el intento
anterior dejó basura, se limpia automáticamente.

### "Falló a las tres horas: continúa donde lo dejé"

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio "/home/fakemitch/Desktop/audiobook.wav" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf \
  --resume
```

El mismo comando, solo añade `--resume` al final. Se saltará los clips que ya
creó y continuará donde lo dejó. (Si usaste `--source` originalmente,
añádelo aquí también: debe coincidir con la ejecución original.)

## ¿Cuánto tarda?

Regla general aproximada en una AMD Radeon RX 9070 XT:

- **Escuchar el audio** (convertir sonidos en palabras): ~1 minuto por hora
  de audiolibro.
- **Etiquetar los clips** (la parte lenta): unas 10 horas por 1 hora de
  audiolibro. Así que un audiolibro típico de 6 horas tarda alrededor de 60 horas de
  tiempo de ordenador. Puedes dejarlo funcionando toda la noche; si necesitas la GPU
  para otra cosa, pulsa **Ctrl+C** para detenerlo y usa `--resume` más tarde.



**Características principales:**
- **ASR con Wav2Vec2 (principal)** con marcas de tiempo de palabra alineadas por CTC de verdad
- Alternativas whisper.cpp Small.en / WhisperX-CPU
- **Preprocesamiento con LLM (Enrichment)**: fase opcional que añade atribución de hablante, estilo de narración y tono emocional a los fragmentos de transcripción usando un LLM GGUF local (por ejemplo, Gemma-4-E2B)
- Anotaciones de Gemma 4 / Qwen con LLM en GPU
- **Fragmentación guiada por la fuente** (`--source`): alinea el ASR contra un EPUB/TXT, usa la grafía de la fuente para el texto del fragmento y descarta el material que solo existe en el audio
- **Límites de fragmento que respetan las pausas**: los fragmentos acaban en puntuación de frase o en pausas naturales del narrador dentro del último 30% de cada fragmento, no donde caiga el umbral de duración
- **Pipeline de 3 fases** (ASR → Enrich → Annotate) con aislamiento por subproceso para seguridad de ROCm
- Capacidad de reanudación: nunca pierdas trabajo por un fallo
- Puntos de control periódicos cada 50 segmentos
- ETA dinámica basada en el rendimiento medido real
- Carga de audio en una sola pasada (remuestreo en memoria)
- Audio de 24 kHz volcado a disco durante la anotación (~1.8 GB de RAM ahorrados en audiolibros largos)
- Registro de memoria y uso de GPU mediante `rocm-smi`

## Modelos recomendados

El preparer usa un LLM para anotar los fragmentos de texto para TTS. Recomendado para GPU AMD de 16 GB o más:

| Rol | Modelo | VRAM (Q6_K) | Por qué |
|---|---|---|---|
| **Principal** | `Qwen2.5-14B-Instruct-Q6_K.gguf` | ~12 GB | Fuerte seguimiento de instrucciones, salida estructurada concisa |
| **Alternativa** | `Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf` | ~5 GB | Respaldo si Qwen no carga |

## Uso

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```

### Con enriquecimiento por LLM (recomendado para libros con muchos personajes)

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --enrich-with-llm \
  --llm-model-path /path/to/gemma-4-E2B-it-Uncensored-MAX.BF16.gguf \
  --enrich-speaker-attribution --enrich-narration-style --enrich-emotional-tone
```

Esto ejecuta el **pipeline completo de 3 fases** (ASR → Enrich → Annotate). La fase de enriquecimiento usa un pequeño LLM local para añadir metadatos `speaker_attribution`, `narration_style` y `emotional_tone` a cada fragmento de transcripción antes de la anotación.

## Opciones de línea de comandos

### Obligatorias
- `--audio PATH`: archivo de audio de entrada (.wav, .mp3, .m4a, .flac, .ogg)
- `--model PATH`: ruta al modelo GGUF principal (obligatorio)

### Opcionales
- `--fallback-model PATH`: modelo GGUF de respaldo usado si `--model` no carga
- `--chunk-size SECONDS`: duración objetivo por fragmento en segundos (por defecto: `10.0`)
- `--lang CODE`: código de idioma para la transcripción (por defecto: `en`)
- `--output PATH`: ruta del ZIP de salida (por defecto: `alexandria_dataset.zip`)
- `--resume`: reanuda desde el `dataset_temp/` existente en lugar de empezar de cero
- `--skip-annotation`: reservado; actualmente se rechaza porque la exportación solo de transcripción no está implementada

### Preprocesamiento con LLM (Enrichment)
Añade metadatos a los fragmentos de transcripción antes de la anotación usando un LLM GGUF local.
- `--enrich-with-llm`: activa la fase de enriquecimiento (se ejecuta entre el ASR y la anotación). Se requiere al menos uno de los tres flags de categoría de abajo; sin ninguno seleccionado, el comando sale con un error.
- `--llm-model-path PATH`: ruta al archivo de modelo GGUF para el enriquecimiento (obligatorio si se define `--enrich-with-llm`; recomendado: Gemma-4-E2B o un modelo pequeño similar)
- `--enrich-speaker-attribution`: indica al LLM que extraiga la atribución de hablante (por ejemplo, "main character", "narrator", "secondary character")
- `--enrich-narration-style`: indica al LLM que extraiga el estilo de narración (por ejemplo, "calm", "energetic", "sad", "questioning")
- `--enrich-emotional-tone`: indica al LLM que extraiga el tono emocional (por ejemplo, "happy", "anxious", "neutral", "excited")

### Diarización de hablantes (opcional)
Atribuye las palabras transcritas a hablantes diarizados mediante pyannote.audio. Requiere
el `preparer_env` aislado de AMD Linux/ROCm creado por Install (incluido
`requirements-diarization.txt`) y un token de Hugging Face que haya aceptado
los términos de
`pyannote/speaker-diarization-community-1`.
- `--diarize`: ejecuta siempre la diarización completa de hablantes sobre todo el audio
- `--auto-detect-speakers`: primero hace una precomprobación muestreada barata (tres ventanas
  de 120 s, ~2-3 min) y solo ejecuta la pasada completa y cara de diarización cuando una
  ventana muestra un segundo hablante con ≥10% del tiempo de habla de esa ventana.
  Se omite (se recurre a la diarización completa) para audio de menos de 15 minutos o cuando
  la precomprobación no puede ejecutarse. Un `--diarize` explícito gana a este flag.
- `--hf-token TOKEN`: token de Hugging Face (por defecto: variable de entorno `HF_TOKEN`);
  requerido por ambos flags anteriores
- Ejecuta `python3 env_doctor.py` (raíz del repo) para comprobar si `pyannote.audio` está
  instalado en el intérprete con el que vas a ejecutar el preparer.

### Fragmentación guiada por la fuente (opcional)
Pasa un EPUB o archivo de texto que coincida con el audiolibro para corregir las transcripciones
erróneas del ASR en la fuente y descartar el material que solo existe en el audio antes del paso del LLM.
- `--source PATH`: EPUB o TXT del mismo libro que narra el audio
- `--source-threshold N`: ratio mínimo de alineación para conservar un fragmento (por defecto: `0.65`)
- `--keep-unaligned`: cuando un fragmento cae por debajo de `--source-threshold`, escribe el
  texto del ASR en lugar de descartarlo (por defecto: descartar)
- `--source-start N`: empieza la alineación de la fuente en la palabra N (omite el anclaje automático)
- `--source-start-text TEXT`: busca de forma difusa TEXT en la fuente y empieza ahí
- `--no-auto-anchor`: desactiva la detección automática del anclaje de fuente (empieza en la palabra 0)

## Ejemplos

### Ejecución estándar (Qwen principal, Gemma de respaldo)
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf
```

### Ruta de salida personalizada
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio book1.wav \
  --model model.gguf \
  --output datasets/book1.zip
```

### Pausar y reanudar (por ejemplo, para jugar o liberar la GPU)

Pulsa **Ctrl+C** en cualquier momento para detener el script. Tu progreso se guarda automáticamente:

```
⚠ Process interrupted by user
Partial results preserved in dataset_temp/ - rerun with --resume to continue
```

Cuando quieras continuar, vuelve a ejecutar con los mismos argumentos más `--resume`:

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model model.gguf \
  --resume
```

El preparer retoma exactamente donde lo dejó, sin repetir trabajo.

### Reanudar tras un fallo
Si el script falla a las 50 horas de una anotación de 60 horas, la recuperación es idéntica: vuelve a ejecutar con `--resume`:

```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model model.gguf \
  --resume
```

El preparer hará lo siguiente:
1. Leer el `dataset_temp/metadata.jsonl` existente
2. Encontrar el último segmento completado y su tiempo de fin
3. Saltar el audio ya procesado
4. Continuar con segmentos nuevos desde ese punto

### Otro idioma
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio spanish_book.wav \
  --model model.gguf \
  --lang es
```

### Fragmentos más grandes
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model model.gguf \
  --chunk-size 15.0
```

## Fragmentación guiada por la fuente

Cuando pasas `--source <epub-or-txt>`, el preparer alinea de forma difusa cada fragmento
del ASR contra el texto fuente y hace una de estas dos cosas:

- **Reemplaza** el texto del ASR del fragmento por la grafía de la fuente cuando el
  ratio de alineación alcanza `--source-threshold` (0.65 por defecto). El LLM entonces
  anota un texto ya correcto. Los nombres de personajes, las grafías dialectales y
  la puntuación salen exactamente como los tiene el EPUB.
- **Descarta** el fragmento por completo cuando la alineación está por debajo del umbral. Sin
  archivo WAV, sin entrada JSONL. Esto es lo que elimina el material que solo existe en el audio:
  introducciones del narrador, créditos de "this audiobook is a work of fiction", anuncios de
  capítulo que no están en el texto, etc.

Si prefieres conservar los fragmentos de baja confianza (usando su texto de ASR), pasa
`--keep-unaligned`. El cursor no avanza en esos, así que el siguiente fragmento
sigue intentando alinearse desde donde se sabía por última vez que coincidía la prosa.

### Ejemplo

```bash
app/env/bin/python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --source "/home/fakemitch/Desktop/books/My Happy Marriage - Volume 01.epub"
```

### Qué verás en los logs

Al arrancar, el preparer informa de la carga de la fuente y la construcción del léxico:
```
▶ Loading source for guided chunking: /path/to/book.epub
  ├─ Source: 284,125 characters
  ├─ 29 recurring proper nouns (gifted, godou, grotesqueries, hana, kanoko, kaya +23 more)
  ├─ 49,686 source words
  └─ Auto-anchor: entry 3 → source word 287 (62.1% match)
```

Por cada fragmento descartado durante la anotación:
```
↪ DROPPED chunk at 421.36s (source ratio 0.42 < 0.65)
```

Por cada fragmento conservado de baja confianza (solo con `--keep-unaligned`):
```
↪ chunk 42 kept (ASR text); source ratio 0.51 < 0.65
```

### Detección de archivo fuente equivocado

Antes de que empiece la anotación del LLM, el preparer preescanea los primeros ~30 fragmentos
del ASR contra la fuente. Si la alineación media está por debajo del 50% o más
del 40% de los fragmentos caen por debajo del 60%, el preparer **aborta con un error**
en lugar de desperdiciar horas de tiempo del LLM en una fuente que no coincide:

```
⚠ Source/audio divergence too high to proceed:
  Sampled 30 chunks — avg alignment 38%, 21 (70%) below 60%.
  Usually means a wrong edition or different translation.
  Re-run without --source, or pass --keep-unaligned to accept the ASR
  text for low-confidence chunks.
```

Normalmente esto significa que has proporcionado el EPUB equivocado (otra
traducción, una edición abreviada o un libro completamente distinto). Vuelve a anclar
manualmente con `--source-start N` / `--source-start-text "..."`, cambia a
la fuente correcta, o ejecuta sin `--source` para recurrir al flujo heredado
solo-ASR.

### Cuándo usarlo y cuándo omitirlo

**Usa `--source` cuando:** tengas el EPUB/TXT correspondiente al
audiolibro Y quieras nombres de personajes limpios + grafías dialectales en el
conjunto de datos de entrenamiento Y no te importe descartar fragmentos que no aparecen en
la fuente (introducciones de capítulo, notas del narrador, paratexto).

**Omite `--source` cuando:** no tengas el texto fuente, o estés
procesando audio que se sabe que está basado libremente en la fuente
(adaptaciones, reescrituras, versiones abreviadas) donde un descarte estricto perdería
contenido que quieres conservar.

### Efecto posterior en la revisión con compare

El modo guiado por la fuente reduce drásticamente lo que verás al revisar
el JSONL resultante con `alexandria_compare.py`. La mayoría de las ediciones que los usuarios
suelen hacer (correcciones de nombres de personajes del ASR, correcciones de grafías dialectales)
ya se han aplicado en el momento de la preparación. La revisión con compare pasa a ser una
comprobación rápida de prosodia: normalmente unos minutos por libro en lugar de una
hora.

## Límites de fragmento que respetan las pausas

El fragmentador emite un fragmento cuando la duración acumulada alcanza
`--chunk-size`, pero no corta exactamente en esa palabra. Mira hacia atrás
en el último 30% de la duración del fragmento y prefiere cortar en:

1. Una palabra que termina en puntuación de frase (`.`, `!`, `?`): lo mejor para la coherencia
   del TTS; elige la ÚLTIMA de la ventana.
2. La pausa natural más larga (hueco ≥ 0.25s entre el fin de una palabra del ASR y el inicio
   de la siguiente) en la misma ventana.
3. La palabra final (la palabra actual en el umbral): solo cuando ninguna
   de las anteriores está disponible.

Las palabras recortadas por la mirada atrás se convierten en la semilla del siguiente fragmento;
no se pierde audio. Efecto neto: los fragmentos acaban en respiraciones, cláusulas o finales de frase
en lugar de a mitad de frase. Combinado con el modo `--source`, los
WAV resultantes son utilizables para el entrenamiento TTS con una edición manual mínima.

## Flujo de procesamiento

1. **Valida las entradas** — Comprueba que existen el archivo de audio y el modelo
2. **Carga el audio** — Una sola lectura a la frecuencia nativa, remuestreo a 16 kHz (para ASR) y 24 kHz (para salida)
3. **Vuelca 24 kHz a disco** — Escribe `.alexandria_audio_24k.wav` y libera RAM
4. **Transcribe el audio** (Wav2Vec2 → IFW → WhisperX-CPU):
   - Wav2Vec2 procesa el audio en fragmentos de 30 s con 3 s de solape
   - Cada fragmento se decodifica con `output_word_offsets=True` para una alineación real de tramas CTC
   - Las palabras de la región de solape se deduplican por propiedad del centro de palabra
5. **Anota los fragmentos**:
   - Carga el modelo Gemma 4 completo en la GPU (`n_gpu_layers=-1`)
   - Recorre los segmentos de palabras, acumulando en fragmentos de ~10 s
   - Genera la anotación TTS por fragmento con una ventana de contexto de 3 segmentos
   - Escribe cada `sample_NNNN.wav` y añade a `metadata.jsonl` inmediatamente (a prueba de fallos)
   - Registra la ETA dinámica cada 10 segmentos
6. **Crea el conjunto de datos de salida** — Comprime el contenido de `dataset_temp/` en la ruta de salida
7. **Limpieza** — Elimina el audio temporal + el directorio temporal (solo si tiene éxito)

## Capacidad de reanudación

El preparer escribe los metadatos de cada segmento en `dataset_temp/metadata.jsonl` **inmediatamente** después de procesarlo (no los acumula hasta el final). Cada entrada incluye:

```json
{
  "audio_filepath": "sample_0042.wav",
  "text": "annotated text...",
  "duration": 10.04,
  "start": 421.36,
  "end": 431.40
}
```

Con `--resume`:
- Se cargan las entradas existentes
- El tiempo `end` más alto se convierte en el punto de reanudación
- Se saltan las palabras con `start < resume_time`
- Se añaden las entradas nuevas (las existentes no se tocan)
- El texto de las últimas 5 entradas se convierte en el contexto del LLM

**En una pausa intencionada (Ctrl+C) o un fallo**: `dataset_temp/` se conserva (no se borra). Vuelve a ejecutar con `--resume` y los mismos argumentos `--audio` y `--model` para continuar donde lo dejaste.

**Si tiene éxito**: `dataset_temp/` se borra después de crear el zip.

### Comprobación de seguridad del archivo fuente

Se escribe un marcador `.source` en `dataset_temp/` que contiene la ruta absoluta del archivo de audio que se está procesando. Con `--resume`:
- Si el marcador coincide con el `--audio` actual → reanudar con seguridad.
- Si el marcador falta o apunta a otro archivo → **borrar `dataset_temp/` y empezar de cero** (registrado como advertencia).

Esto evita corrupciones accidentales al reutilizar el directorio de trabajo para audiolibros distintos. El procesador por lotes depende de esta garantía para pasar `--resume` con seguridad en cada reintento.

## Monitorización de la GPU

El preparer registra estadísticas de la GPU en puntos clave:
- Después de cargar el modelo Wav2Vec2
- Antes y durante el procesamiento de fragmentos (cada 50 fragmentos)
- Después de cargar el modelo Gemma
- Durante la anotación (cada 10 segmentos)

Cada entrada del registro muestra:
```
GPU Usage (chunk 50/725):
  ├─ Memory: 4.52 GB / 17.10 GB (26.4%)
  └─ Utilization: 87.5%
```

La memoria viene de `torch.cuda.memory_allocated()`. El uso viene de `rocm-smi --showuse --json` (GPU AMD). Nota: cuando Gemma es el único consumidor de la GPU, `torch.cuda.memory_allocated()` informa 0 porque llama-cpp-python gestiona la memoria de GPU fuera de PyTorch; consulta `rocm-smi` para ver la imagen real.

## Seguimiento del progreso

El pipeline se ejecuta en **tres fases**, cada una lanzada como un subproceso separado para el aislamiento de ROCm:

### Fase 1: Wav2Vec2 (ASR)
```
↳ Chunk 50/725 | avg 0.51s/chunk | ETA 5m 47s
```

### Fase 2: enriquecimiento por LLM (opcional, requiere `--enrich-with-llm`)
```
▶ Created 72 chunks for LLM enrichment
▶ Running LLM enrichment: python llm_enricher.py --model-path ... --input-file ... --output-file ...
Enriching chunk: 10.00s - 20.14s
...
✓ LLM Enrichment Phase completed successfully.
```

### Fase 3: anotación
```
↳ Progress: 50/4713 chunks | Avg: 46.2s/chunk | Elapsed: 38m 42s | ETA: 59h 31m
```

El formato de tiempo se adapta: `Nh Mm` si es > 1 hora, `Mm Ss` si es > 1 min, `Ns` en otro caso.

## Logs

Los logs detallados se escriben en `logs/alexandria_preparer_YYYYMMDD_HHMMSS.log`:
- Entradas completas de nivel DEBUG para cada paso
- Instantáneas de estadísticas de GPU
- Líneas de comando y salidas de los subprocesos
- Tracebacks de excepciones (si hay)

Sigue el log en otra terminal:
```bash
tail -f logs/alexandria_preparer_*.log
```

## Prioridad de métodos de ASR

| # | Método | Dispositivo | Contexto | Marcas de tiempo de palabra |
|---|---|---|---|---|
| 1 | **Wav2Vec2** (principal) | GPU | fragmentos de 30 s + 3 s de solape | Reales (alineación de tramas CTC) |
| 2 | whisper.cpp Small.en | GPU/CPU nativa | Continuo | Nativas, con palabras de duración cero fusionadas de forma conservadora |
| 3 | WhisperX-CPU | CPU | n/a | Alineadas mediante alineación forzada |

Wav2Vec2 se elige primero por su preservación del contexto con fragmentos solapados y sus marcas de tiempo por palabra alineadas por CTC. Si no está disponible o su salida falla la validación de marcas de tiempo, el preparer usa la instalación fijada de whisper.cpp Small.en producida por `install.js`; WhisperX sigue siendo el último recurso de CPU. Small.en es intencionadamente solo para inglés, así que las solicitudes en otros idiomas lo omiten en lugar de usar en silencio el modelo equivocado.

## Consejos de rendimiento

- **Memoria de GPU** — Gemma usa ~8 GB; Wav2Vec2 usa ~2 GB. Ambos caben cómodamente en una GPU de 16 GB o más.
- **RAM** — Volcar el audio de 24 kHz a disco ahorra ~1.8 GB durante la fase de anotación de varias horas.
- **Espacio en disco** — Deja ~5 - 10 GB libres por audiolibro (WAV temporal + WAV de segmentos + zip final).
- **Tiempo de ejecución** — En una AMD Radeon RX 9070 XT, calcula ~6 min de Wav2Vec2 + ~60 h de anotación con Gemma para un audiolibro de ~5.4 h con fragmentos de 10 s.

## Solución de problemas

### Archivo de audio no encontrado
```
Error: Audio file not found: audiobook.wav
```
- Comprueba que la ruta es correcta; el CWD del script es el lugar desde donde lo lanzaste.

### Archivo de modelo no encontrado
```
Error: Model file not found: ...
```
- Descarga el modelo GGUF de Gemma (por ejemplo, `download_model.py`) o indica `--model PATH`.

### `rocm-smi unavailable`
- Verifica que existe `/opt/rocm/bin/rocm-smi` y que es ejecutable.
- La memoria de GPU sigue funcionando sin él; solo falta el % de uso.

### `expandable_segments not supported`
- Aviso informativo de PyTorch: AMD ROCm no admite esa optimización de memoria. PyTorch recurre a la asignación estándar. No hace falta hacer nada.

### La reanudación eligió el archivo equivocado
- El preparer sigue la fuente mediante el marcador `dataset_temp/.source`. Los marcadores que no coinciden hacen que `dataset_temp/` se borre automáticamente (con una advertencia) antes de procesar de cero.
- Si quieres forzar un inicio limpio de todos modos, borra `dataset_temp/` a mano u omite `--resume`.

### Sin espacio en disco
- Comprueba con `du -sh dataset_temp/ .alexandria_audio_24k.wav`.
- Libera espacio y vuelve a ejecutar con `--resume`.

### WAV sobredimensionado (>4 GiB): la cabecera informa de una duración minúscula
Síntoma: un WAV de audiolibro de varios GB informa de una duración de unos minutos en lugar
de horas. Ejemplo de log de arranque:
```
[WARNING] ⚠ Oversized WAV detected: header says 135.3s (2.3 min) but file size
implies 24477.2s (6.80 hr). WAV data-chunk-size field is 32-bit and has
wrapped — ffmpeg `-ignore_length 1` will be used to read the full audio.
```

Por qué ocurre: el WAV estándar usa un campo sin signo de 32 bits para el tamaño del fragmento
`data`, que se desborda a los 4 GiB. A 44.1 kHz estéreo PCM_16 eso ocurre a
~6.8 horas, así que cualquier WAV de audiolibro de longitud completa producido por una herramienta que no
cambió a RF64/WAV64 tiene una cabecera desbordada. `soundfile` y `librosa.load`
respetan la cabecera falsa y truncan en silencio a `filesize mod 4 GiB` de
audio: cada ejecución "completa" sobre un archivo sobredimensionado es en realidad solo el
primer trozo (por ejemplo, 2 min de un archivo de 6.8 h, 22 min de un archivo de 27 h).

Qué hace el preparer al respecto: `validate_inputs` compara el tamaño del archivo
con los bytes que implica la cabecera, imprime el aviso de arriba cuando no
coinciden y encamina la carga por ffmpeg (`-ignore_length 1`). El
scratch de 24 kHz se transmite directamente a disco y el búfer de ASR de 16 kHz
se canaliza a un array numpy float32. No se materializa ningún intermedio a la frecuencia nativa,
así que la RAM máxima queda acotada sea cual sea la duración del audio.

Qué deberías hacer: nada, normalmente; la corrección es automática cuando
el preparer detecta el desbordamiento. Si prefieres recodificar el WAV una vez para
arreglarlo de forma permanente (y evitar la decodificación duplicada con ffmpeg), convierte a
FLAC o RF64-WAV, por ejemplo:
```
ffmpeg -ignore_length 1 -i input.wav -c:a flac output.flac
```

## API (invocación programática)

### Python (subprocess)
```python
import subprocess

result = subprocess.run([
    "python", "alexandria_preparer_rocm_compatible.py",
    "--audio", "audiobook.wav",
    "--model", "Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf",
    "--chunk-size", "10.0",
    "--lang", "en",
    "--output", "my_dataset.zip",
], check=True)
```

### JavaScript (Node.js)
```javascript
const { execFile } = require("child_process");

execFile("python", [
  "alexandria_preparer_rocm_compatible.py",
  "--audio", "audiobook.wav",
  "--model", "model.gguf",
  "--output", "my_dataset.zip",
], { maxBuffer: 1024 * 1024 * 100 }, (err, stdout, stderr) => {
  if (err) throw err;
  console.log(stdout);
});
```

### Curl (no aplica)
El preparer es una herramienta CLI, no un servicio HTTP. Envuélvelo en un servidor Flask/FastAPI si necesitas acceso por red.

## Ver también

- `BATCH_PROCESSOR_GUIDE.md` — Procesa varios audiolibros en secuencia con reanudación entre archivos
- `../../README.md` — Resumen del proyecto
