[English](BATCH_PROCESSOR_GUIDE.md) | [中文](BATCH_PROCESSOR_GUIDE_CN.md) | [日本語](BATCH_PROCESSOR_GUIDE_JA.md) | [Español](BATCH_PROCESSOR_GUIDE_ES.md)

# Guía del procesador por lotes de Alexandria

Procesa varios archivos de audiolibro en secuencia con un solo comando.

## Qué hace esta herramienta (en palabras sencillas)

El procesador por lotes ejecuta el Preparer (ver `PREPARER_GUIDE.md`) sobre **varios archivos de audiolibro a la vez**. En lugar de ejecutar el preparer manualmente un archivo cada vez, le das una lista de archivos (o una carpeta llena de archivos) y los procesa todos en orden, uno tras otro.

### ¿Para quién es esto?

- **Tienes varios audiolibros** y quieres crear conjuntos de datos de entrenamiento para todos
- **No necesitas esto** si solo tienes un audiolibro: usa el Preparer

### Cómo se relaciona con el Preparer

El procesador por lotes es solo un envoltorio alrededor del Preparer. Hace lo siguiente:
1. Toma una lista de archivos de audio
2. Ejecuta el Preparer en cada uno
3. Guarda cada salida con un nombre único para que no se sobrescriban entre sí
4. Te muestra un resumen cuando terminan todos los archivos

---

## Inicio rápido para quienes no programan

### Paso 1: reúne tus archivos

Pon todos los archivos WAV de audiolibro que quieres procesar en una carpeta. Por ejemplo:
```
/home/fakemitch/Desktop/my_audiobooks/
  ├── book1.wav
  ├── book2.wav
  └── book3.wav
```

### Paso 2: abre una terminal

- **Windows:** pulsa `Win + R`, escribe `cmd` y pulsa Intro
- **Mac:** abre Spotlight (Cmd + Space), escribe `terminal` y pulsa Intro
- **Linux:** abre el menú de aplicaciones y busca "Terminal"

### Paso 3: ejecuta el procesador por lotes

Copia y pega este comando (sustituye la ruta de la carpeta por la tuya):

```bash
cd ~/.pinokio/api/alexandria-audiobook.git
./app/env/bin/python alexandria_batch_processor.py \
  --folder "/path/to/your/audiobooks" \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf
```

### Paso 4: espera

Cada archivo tarda aproximadamente 1 hora por cada 5 horas de audio. Si tienes 3 audiolibros de 5 horas cada uno, calcula unas 3 horas en total.

### Paso 5: comprueba los resultados

Al terminar, cada audiolibro tendrá su propia carpeta de salida con un archivo de conjunto de datos `.zip`.

---

## Resumen

El procesador por lotes permite poner en cola varios archivos de audio para procesarlos. Cada archivo se procesa uno tras otro (en secuencia), creando un conjunto de datos anotado completo para cada uno.

**Características principales:**
- Procesa varios archivos con un solo comando
- Procesamiento secuencial (uno a la vez, sin sobrecarga de paralelismo)
- Nombres de archivo de salida automáticos (incluyen el nombre del archivo original)
- Registro completo y seguimiento del progreso
- Resumen de resultados en JSON con estadísticas
- Aceleración por GPU para todos los archivos
- Estimaciones de tiempo para cada archivo

## Inicio rápido

Elige uno de los tres modos de entrada:

**A. Lista los archivos individualmente** (posicionales, separados por espacios)
```bash
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model Qwen2.5-14B-Instruct-Q6_K.gguf
```

**B. Escanea una carpeta entera** (usa el flag `--folder`; el espacio importa)
```bash
python alexandria_batch_processor.py --folder /path/to/audiobooks --model Qwen2.5-14B-Instruct-Q6_K.gguf
```

**C. Mezcla ambos** (carpeta + archivos extra)
```bash
python alexandria_batch_processor.py extra.wav --folder /path/to/audiobooks --model Qwen2.5-14B-Instruct-Q6_K.gguf
```

> **Las rutas con espacios deben ir entre comillas.** Si tu carpeta es `/home/you/Desktop/New folder`, el shell la dividirá en dos argumentos a menos que la pongas entre comillas:
> ```bash
> # WRONG — shell splits at the space
> python alexandria_batch_processor.py --folder /home/you/Desktop/New folder --model model.gguf
>
> # RIGHT — quotes keep the path as one argument
> python alexandria_batch_processor.py --folder "/home/you/Desktop/New folder" --model model.gguf
> ```

> **No olvides el espacio después de `--folder`.** Escribir `--/home/...` (sin espacio) hace que argparse trate toda la ruta como el nombre de un flag desconocido.

## Uso

### Un solo archivo (script original)
```bash
python alexandria_preparer_rocm_compatible.py \
  --audio audiobook.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```

### Varios archivos (procesador por lotes)
```bash
python alexandria_batch_processor.py \
  audiobook1.wav audiobook2.wav audiobook3.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```

### Escaneo de carpeta (procesador por lotes)
```bash
python alexandria_batch_processor.py \
  --folder "/path/to/audiobooks" \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf \
  --chunk-size 10.0 \
  --lang en
```
Pon la ruta de la carpeta entre comillas si contiene espacios.

## Opciones de línea de comandos

### Argumentos posicionales
- `audio_files` - Uno o más archivos de audio a procesar (obligatorio, separados por espacios)

### Argumentos opcionales
- `--folder DIR` - Escanea una carpeta en busca de todos los archivos de audio compatibles (`.wav .flac .ogg`), ordenados alfabéticamente; se puede combinar con archivos listados individualmente
- `--model PATH` - Ruta al archivo de modelo GGUF principal (obligatorio, recomendado: `Qwen2.5-14B-Instruct-Q6_K.gguf`)
- `--fallback-model PATH` - Modelo GGUF de respaldo opcional si `--model` no carga (por ejemplo, `Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf`)
- `--chunk-size SIZE` - Duración objetivo por fragmento en segundos (por defecto: 10.0)
- `--lang CODE` - Código de idioma para la transcripción (por defecto: en)
- `--force` - Reprocesa los archivos aunque ya exista `alexandria_dataset_<name>.zip` (por defecto: saltar)

### Modo guiado por la fuente (opcional)

Cuando está activado, el procesador por lotes reenvía los flags de fragmentación guiada por la fuente
a cada subproceso del preparer. Consulta `PREPARER_GUIDE.md` para saber qué hace el modo
en detalle; el resumen a nivel de lote es: los nombres de los personajes salen
correctamente escritos (del EPUB), y los pasajes que solo existen en el audio (créditos,
introducciones de capítulo) se descartan automáticamente.

- `--source-folder DIR` - Carpeta de archivos fuente `.epub` o `.txt`. Cada
  archivo de audio se empareja con una fuente por su nombre base: `audio/Book1.wav` busca
  `<source-folder>/Book1.epub` (preferido) o `<source-folder>/Book1.txt`.
  Los archivos de audio sin fuente correspondiente se ejecutan en el modo heredado solo-ASR con un
  aviso. Mutuamente excluyente con `--source`.
- `--source PATH` - Un único archivo fuente aplicado a **todos** los archivos de audio
  del lote. Útil cuando procesas varias grabaciones del
  mismo libro (por ejemplo, distintos narradores). Mutuamente excluyente con
  `--source-folder`.
- `--source-threshold N` - Ratio mínimo de alineación para conservar un fragmento
  (por defecto: 0.65). Se reenvía al `--source-threshold` del preparer.
- `--keep-unaligned` - No descartes los fragmentos de baja confianza; usa el texto
  del ASR en su lugar. Se reenvía al `--keep-unaligned` del preparer.

Al inicio del lote, el procesador imprime qué archivos coincidieron y
cuántos no, para que puedas detectar problemas de nombres antes de que la ejecución avance
por 60 horas de audio:

```
├─ Source-guided: matching from /path/to/books/
│   3 matched, 1 no match (legacy ASR-only for those)
│     no match: 'Audiobook4'
├─ Source threshold: 0.65 (strict-drop)
```

## Pausar y reanudar

### Pausar a mitad de ejecución (por ejemplo, para jugar o liberar la GPU)

Pulsa **Ctrl+C** en cualquier momento para detener el procesador por lotes. El punto de control del archivo actual en `dataset_temp/` se conserva automáticamente. Cuando quieras continuar, vuelve a ejecutar exactamente el mismo comando; no hacen falta flags adicionales:

```bash
# Stopped mid-run with Ctrl+C — just rerun the same command to resume
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf
```

El procesador por lotes hará lo siguiente:
- **Saltar** los archivos cuyo zip de salida ya existe (archivos completados)
- **Reanudar** el archivo interrumpido desde su último punto de control (sin repetir trabajo)
- **Procesar desde cero** los archivos que aún no se han empezado

## Capacidad de reanudación

El procesador por lotes admite **dos capas de reanudación**:

**1. Omisión a nivel de archivo** — Los archivos cuyo zip de salida (`alexandria_dataset_<name>.zip`) ya existe se saltan en ejecuciones posteriores.

**2. Reanudación a mitad de archivo** — Si un archivo se interrumpió a mitad de anotación (por Ctrl+C o un fallo), el procesador por lotes pasa `--resume` al preparer automáticamente. El preparer lee su punto de control y continúa desde el último segmento completado.

El marcador `.source` del preparer en `dataset_temp/` asegura que nunca reanudemos por error el trabajo parcial de otro archivo: si el marcador no coincide con el archivo de audio actual, se borra `dataset_temp/` y el procesamiento empieza desde cero.

```bash
# First run - processes all 3 files
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf
# (book1 completes, book2 crashes 50h into annotation)

# Second run:
#  - book1.wav skipped (zip exists)
#  - book2.wav resumes from last checkpoint (continues from segment ~4000)
#  - book3.wav processed fresh
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf

# Force reprocess everything (no skip, no resume)
python alexandria_batch_processor.py book1.wav book2.wav book3.wav --model model.gguf --force
```

## Salida en tiempo real

El procesador por lotes transmite la salida del script principal **en tiempo real** para que puedas vigilar:
- El progreso de la transcripción (fragmentos de Wav2Vec2 en proceso)
- El porcentaje de uso de la GPU
- El progreso de la anotación con estimaciones de tiempo transcurrido/restante
- Cualquier advertencia o error en cuanto ocurre

¡Se acabó esperar horas preguntándote si el procesamiento está atascado!

## Ejemplos

### Procesar 3 audiolibros
```bash
python alexandria_batch_processor.py \
  book1.wav book2.wav book3.wav \
  --model Qwen2.5-14B-Instruct-Q6_K.gguf \
  --fallback-model Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf
```

### Procesar con un tamaño de fragmento personalizado
```bash
python alexandria_batch_processor.py \
  book1.wav book2.wav \
  --model model.gguf \
  --chunk-size 15.0
```

### Procesar en otro idioma
```bash
python alexandria_batch_processor.py \
  spanish_book.wav french_book.wav \
  --model model.gguf \
  --lang es
```

### Guiado por la fuente: lote con un EPUB por audiolibro

Cada archivo de audio se empareja con una fuente vecina por su nombre base. Así,
`audio/MyHappyMarriage.wav` se empareja con `books/MyHappyMarriage.epub`,
etc. Los archivos de audio que no tienen una fuente correspondiente también se procesan,
solo que en el modo heredado solo-ASR (con un aviso al arrancar).

```bash
app/env/bin/python alexandria_batch_processor.py \
  --folder /home/fakemitch/Desktop/audiobooks/ \
  --source-folder /home/fakemitch/Desktop/books/ \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf
```

### Guiado por la fuente: el mismo EPUB aplicado a todos los archivos del lote

Cuando tienes varias grabaciones del mismo libro (distintos narradores,
distintas versiones), usa `--source` en lugar de `--source-folder` para que se aplique
el mismo archivo fuente a todas ellas:

```bash
app/env/bin/python alexandria_batch_processor.py \
  narrator_a.wav narrator_b.wav narrator_c.wav \
  --source "/home/fakemitch/Desktop/books/My Happy Marriage - Volume 01.epub" \
  --model models/Qwen2.5-14B-Instruct-Q6_K.gguf
```

### Guiado por la fuente con un umbral más estricto

Si el umbral por defecto de 0.65 descarta pasajes que quieres conservar,
relájalo (o usa `--keep-unaligned` para no descartar nunca):

```bash
# Loosen threshold to 0.55 — keep more borderline chunks
app/env/bin/python alexandria_batch_processor.py \
  book.wav --model model.gguf \
  --source book.epub \
  --source-threshold 0.55

# Or: keep everything (use ASR text for low-confidence chunks)
app/env/bin/python alexandria_batch_processor.py \
  book.wav --model model.gguf \
  --source book.epub \
  --keep-unaligned
```

## Archivos de salida

Para cada archivo de entrada, el procesador por lotes crea:
- `alexandria_dataset_<filename>.zip` - Conjunto de datos anotado
- Renombrado automáticamente desde `alexandria_dataset.zip` para evitar conflictos

Ejemplo:
```
Input:  audiobook1.wav
Output: alexandria_dataset_audiobook1.zip

Input:  audiobook2.wav  
Output: alexandria_dataset_audiobook2.zip
```

## Registro

El procesador por lotes crea registros detallados en el directorio `logs/`:
- `logs/alexandria_batch_YYYYMMDD_HHMMSS.log` - Registro completo del procesamiento

## Resumen de resultados

Cuando termina el procesamiento, se crea un resumen JSON:
- `batch_results_YYYYMMDD_HHMMSS.json` - Resultados del procesamiento

Contiene:
- Resultados por archivo (éxito/fallo)
- Rutas y tamaños de los archivos de salida
- Tiempos de procesamiento de cada archivo
- Tiempo total de procesamiento
- Marca de tiempo

Ejemplo:
```json
{
  "timestamp": "2026-05-16T16:30:00",
  "total_time_seconds": 14400,
  "results": {
    "succeeded": [
      {
        "file": "book1.wav",
        "output": "alexandria_dataset_book1.zip",
        "output_size_mb": 250.5,
        "time": "2h 15m",
        "time_seconds": 8100
      }
    ],
    "failed": [],
    "skipped": []
  }
}
```

## Flujo de procesamiento

1. **Validación** - Comprueba que todos los archivos existen y son formatos de audio válidos
2. **Comprobación del modelo** - Verifica que el modelo Gemma es accesible
3. **Monitorización de la GPU** - Registra la memoria y el uso de la GPU al inicio
4. **Procesamiento secuencial** - Procesa cada archivo uno a uno:
   - Carga el audio (16kHz + 24kHz)
   - **Transcribe con Wav2Vec2** (GPU principal, fragmentos solapados de 30s para preservar el contexto)
     - Recurre a whisper.cpp Small.en si es necesario
     - Recurre a WhisperX-CPU si es necesario
   - **Anota los fragmentos con Gemma** (aceleración por GPU)
     - Memoria y uso de la GPU monitorizados cada 10 segmentos
     - Tiempo transcurrido mostrado en formato horas/minutos
     - Tiempo restante estimado con actualizaciones en tiempo real
   - Crea segmentos de audio con anotaciones
   - Empaqueta como archivo ZIP
   - Renombra la salida para evitar conflictos
   - Estadísticas de GPU registradas entre archivos
5. **Resumen** - Genera un informe del procesamiento con estadísticas del lote

## Monitorización de la GPU y seguimiento del progreso

### Monitorización en tiempo real
- **Memoria de la GPU**: sigue la memoria asignada/total y el porcentaje durante:
  - Después de cargar el modelo Wav2Vec2
  - Antes y durante el procesamiento de fragmentos de Wav2Vec2 (cada 50 fragmentos)
  - Después de cargar el modelo Gemma
  - Durante la anotación (cada 10 segmentos)
  - Entre archivos durante el procesamiento por lotes

- **Uso de la GPU**: muestra el porcentaje real de carga de la GPU mediante rocm-smi
  - Ayuda a verificar que la GPU se está usando realmente
  - Detecta falsos positivos en los que la GPU dice estar en uso pero no lo está
  - Ejemplo: `Utilization: 45.3%`

### Seguimiento del progreso
- **Formato de tiempo**: todas las estimaciones de tiempo se muestran en formato horas/minutos
  - Total estimado: `~60h 13m` en lugar de solo segundos
  - Tiempo transcurrido: `Elapsed: 7m 40s`
  - Tiempo restante: `Remaining: ~60h 12m`
  - Actualizado cada 10 segmentos de anotación

### Ejemplo de salida de progreso
```
GPU Usage (chunk 50/725):
  ├─ Memory: 4.52GB / 16.00GB (28.3%)
  └─ Utilization: 87.5%

Progress: 10/4713 chunks | Elapsed: 7m 40s | Remaining: ~60h 12m
```

## Consejos de rendimiento

- **Aceleración por GPU** - Toda la transcripción y la anotación usan la GPU con monitorización en tiempo real
- **Procesamiento secuencial** - Sin sobrecarga de paralelismo, cada archivo tiene acceso completo a la GPU
- **Espacio en disco** - Asegúrate de tener ~5-10 GB libres por audiolibro
- **Vigila la memoria** - La caché de la GPU se limpia entre archivos, el uso de memoria se registra

## Estimaciones de tiempo

Basadas en ~5.5 horas por audiolibro de 19,500 segundos:
- Transcripción: ~15-20 minutos (acelerada por GPU)
- Anotación: ~4-5 horas (acelerada por GPU, ~46s por fragmento)

Total = ~4.5-5.5 horas por audiolibro

Para 3 audiolibros: ~13-16 horas en total

## Solución de problemas

### "unrecognized arguments" o "--folder path is not a directory"
```
error: unrecognized arguments: --/home/you/Desktop/New
Error: --folder path is not a directory: /home/you/Desktop/New
```
Casi siempre es un problema de comillas del shell, no un bug:
- **Falta un espacio:** `--/home/...` debería ser `--folder /home/...` (con un espacio entre el flag y la ruta)
- **Espacios sin comillas:** pon entre comillas dobles cualquier ruta que contenga espacios: `--folder "/home/you/Desktop/New folder"`
- **Barra invertida final + espacio:** en comandos de varias líneas, asegúrate de que `\` es el último carácter de la línea (sin espacios al final)

### Archivo no encontrado
```
Error: File not found: audiobook.wav
```
- Comprueba que la ruta del archivo es correcta
- Usa rutas absolutas si el archivo está en otro directorio
- Pon entre comillas cualquier ruta que contenga espacios: `"/path with spaces/book.wav"`

### Formato no compatible
```
Warning: Unsupported format: audiobook.aac
```
- Formatos admitidos: `.wav`, `.flac`, `.ogg` (de forma nativa mediante soundfile/libsndfile)
- `.mp3` y `.m4a` no son fiables: convierte primero a `.wav`: `ffmpeg -i input.mp3 output.wav`

### Modelo no encontrado
```
Error: Model file not found
```
- Asegúrate de que la ruta del modelo es correcta
- Descarga el modelo Gemma-4 si falta

### GPU no disponible
```
Warning: GPU not available - running on CPU (slower)
```
- Comprueba la instalación de CUDA/ROCm
- Verifica que los controladores de la GPU están instalados
- Un solo archivo tardará mucho más en CPU

### Sin espacio
```
Error: No space left on device
```
- Limpia conjuntos de datos antiguos
- Libera espacio en disco (hacen falta ~10GB por archivo)
- Comprueba con `du -sh alexandria_dataset*.zip` los archivos antiguos

### Los conjuntos de datos parecen completos pero solo cubren una fracción minúscula de cada audiolibro
Si tus archivos `.wav` son mayores de ~4 GiB cada uno y ves resúmenes del preparer
como "Total audio in dataset: 1.5 min" en un libro que debería durar
varias horas, has dado con el bug de desbordamiento de cabecera WAV de >4 GiB: el campo
data-chunk-size del WAV es de 32 bits y trunca en silencio todo lo que pasa del
desbordamiento. El preparer lo detecta ahora automáticamente y encamina la
carga por ffmpeg, pero deberías volver a ejecutar cualquier conjunto de datos construido
con el cargador antiguo. Consulta PREPARER_GUIDE.md → "Oversized WAV" para más detalles.

## Uso avanzado

### Procesar todos los archivos de una carpeta
```bash
python alexandria_batch_processor.py \
  --folder /path/to/audiobooks \
  --model model.gguf
```

Los archivos se toman por orden alfabético. Formatos admitidos: `.wav .flac .ogg`, que tienen soporte nativo en soundfile/libsndfile. Convierte primero los archivos `.mp3` o `.m4a` a `.wav` (por ejemplo, `ffmpeg -i input.mp3 output.wav`).

También puedes mezclar una carpeta con archivos listados individualmente:
```bash
python alexandria_batch_processor.py extra.wav \
  --folder /path/to/audiobooks \
  --model model.gguf
```

### Procesar con recuperación de errores
El procesador por lotes hace automáticamente lo siguiente:
- Valida todos los archivos antes de empezar
- Continúa si falla un archivo
- Informa qué archivos tuvieron éxito o fallaron
- Guarda los resultados en JSON

### Vigilar el progreso
```bash
# In another terminal
tail -f logs/alexandria_batch_*.log
```

## Notas de desarrollo

El procesador por lotes:
- Envuelve `alexandria_preparer_rocm_compatible.py`
- Gestiona conflictos de nombres de archivo
- Sigue tiempos y resultados
- Registra todas las operaciones
- Limpia los archivos temporales

Para modificar el comportamiento, edita `alexandria_batch_processor.py` directamente.
