[English](COMPARE_GUIDE.md) | [中文](COMPARE_GUIDE_CN.md) | [日本語](COMPARE_GUIDE_JA.md) | [Español](COMPARE_GUIDE_ES.md)

# Guía de Compare de Alexandria

Compara tus transcripciones de `metadata.jsonl` con el EPUB o archivo de texto original.
Cada cambio requiere tu aprobación: no se escribe nada automáticamente.

## Qué hace esta herramienta (en palabras sencillas)

Después de que el Preparer escucha tu audiolibro y escribe lo que oye, a veces se equivoca con palabras,
sobre todo nombres, palabras poco habituales o palabras que suenan parecido. La herramienta Compare te permite
**comprobar lo que oyó la IA frente a lo que dice realmente el libro**, y corregir los errores uno a uno.

### ¿Para quién es esto?

- **Ejecutaste el Preparer** y obtuviste un archivo `metadata.jsonl`
- **Tienes el libro original** (.epub o .txt) del que se leyó el audiolibro
- **Quieres corregir errores de transcripción** antes de usar los datos para entrenar

### ¿Para qué molestarse?

Si la IA oyó "Sherlock" como "sure lock", el entrenamiento del TTS aprenderá la palabra equivocada. Corregir estos errores hace que tu voz entrenada sea más precisa.

---

## Inicio rápido para quienes no programan

### Paso 1: extrae tu conjunto de datos

Si la salida del preparer es un archivo `.zip`, descomprímelo primero:
```bash
unzip alexandria_dataset.zip -d my_dataset/
```

Ahora deberías tener una carpeta con `metadata.jsonl` y muchos archivos `sample_NNNN.wav`.

### Paso 2: abre una terminal

- **Windows:** pulsa `Win + R`, escribe `cmd` y pulsa Intro
- **Mac:** abre Spotlight (Cmd + Space), escribe `terminal` y pulsa Intro
- **Linux:** abre el menú de aplicaciones y busca "Terminal"

### Paso 3: ejecuta la herramienta Compare

Copia y pega este comando (sustituye las rutas por las tuyas):

```bash
cd ~/.pinokio/api/alexandria-audiobook.git
./app/env/bin/python alexandria_compare.py \
  --jsonl /path/to/my_dataset/metadata.jsonl \
  --source /path/to/original/book.epub
```

### Paso 4: revisa y corrige los errores

La herramienta te mostrará cada fragmento donde la transcripción de la IA difiere del libro. Verás algo como:

```
[ANNOTATED] sure lock looked at the clock
[ORIGINAL]  Sherlock looked at the clock
[a]ccept [m]erge [k]eep [e]dit [s]kip [q]uit
```

**Qué hace cada tecla:**
- **`a` (accept):** la IA tenía razón; conserva la versión de la IA
- **`m` (merge):** combina ambas versiones; suele ser la mejor opción porque mantiene la ortografía correcta del libro Y las marcas de dirección vocal que añadió la IA
- **`k` (keep):** conserva la versión de la IA sin cambios
- **`e` (edit):** escribe una corrección propia
- **`s` (skip):** salta esta por ahora (puedes volver luego)
- **`q` (quit):** guarda tu progreso y sale

**Consejo:** pulsa **`m` (merge)** la mayoría de las veces. Mantiene las palabras correctas del libro y conserva las indicaciones de voz (como "*whispered*" o "*excitedly*") que añadió la IA.

### Paso 5: usa los datos corregidos

Cuando termina la herramienta compare, guarda un `metadata.jsonl` corregido. Usa este archivo corregido en lugar del original al entrenar tu voz.

---

## Resumen

La herramienta compare:
1. Carga tu JSONL y el texto fuente original
2. Alinea de forma difusa cada fragmento de ~10 s contra el pasaje correspondiente de la fuente
3. **Aprueba automáticamente** las entradas que ya coinciden bien (umbral configurable)
4. **Te muestra** las entradas que difieren y espera tu decisión
5. Escribe un JSONL corregido solo con los cambios que aprobaste

## Dependencias

Las fuentes de texto plano no necesitan paquetes extra. La compatibilidad con EPUB requiere:
```bash
uv pip install ebooklib beautifulsoup4
# or
pip install ebooklib beautifulsoup4
```

El script compare también importa `alexandria_alignment.py` (un módulo hermano
en el mismo directorio) para la carga de fuentes, la alineación difusa y el
léxico de nombres propios. Ambos archivos deben quedarse uno al lado del otro.

## ¿Usaste `--source` en el preparer?

Si produjiste este JSONL con `alexandria_preparer_rocm_compatible.py
--source ...`, las transcripciones erróneas de nombres de personajes en el ASR y las grafías dialectales
ya se corrigieron en el momento de la preparación: el texto del fragmento usa la grafía de la
fuente, no lo que transcribió el ASR. En ese caso, el paso de revisión con compare es sobre todo
una comprobación rápida de la prosodia: revisa por encima las marcas de `*emphasis*` y
de pausa `...` que parezcan raras, corrige unos pocos casos con `[e]dit` y listo.

Si produjiste el JSONL sin `--source`, el texto de cada fragmento es la
salida cruda del ASR y usarás la revisión con compare para corregir nombres, dialecto
y cualquier material que solo existe en el audio y que el preparer no sabía que debía descartar. Ese es el
flujo de trabajo clásico para el que se diseñó originalmente este script.

## Importante: ejecútalo en una terminal real

Este script es **interactivo**: espera la entrada del teclado en cada entrada.
Debes ejecutarlo en una sesión de terminal real, no mediante una herramienta de programación con IA ni ningún
otro entorno que no admita stdin.

```bash
cd /path/to/alexandria-audiobook2
app/env/bin/python alexandria_compare.py \
  --jsonl /path/to/metadata.jsonl \
  --source /path/to/original.txt
```

Luego escribe tu elección (`a`, `k`, `e`, `s` o `q`) directamente en el prompt `>`.

## Uso

```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source original_book.epub
```

O con un archivo de texto plano:
```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source original_book.txt
```

Si tu conjunto de datos ya está comprimido, extrae primero `metadata.jsonl`:
```bash
unzip alexandria_dataset.zip dataset_temp/metadata.jsonl
```

## Opciones

| Flag | Valor por defecto | Descripción |
|---|---|---|
| `--jsonl PATH` | obligatorio | Ruta a `metadata.jsonl` |
| `--source PATH` | obligatorio | `.epub` o `.txt` original |
| `--output PATH` | `<jsonl>_corrected.jsonl` | Dónde escribir el JSONL corregido |
| `--threshold N` | `0.90` | Ratio de similitud por encima del cual las entradas se aprueban automáticamente |
| `--review-all` | desactivado | Muestra todas las entradas, sea cual sea su similitud |
| `--reset` | desactivado | Descarta el progreso guardado y empieza desde el principio |
| `--source-start N` | desactivado | Empieza manualmente en la palabra N de la fuente (omite el anclaje automático) |
| `--source-start-text "..."` | desactivado | Busca de forma difusa una frase en la fuente y empieza ahí |
| `--no-auto-anchor` | desactivado | Desactiva el anclaje automático; empieza en la palabra 0 de la fuente |
| `--review-preanchor` | desactivado | Revisa individualmente las entradas anteriores al anclaje (por defecto: se conservan automáticamente) |

## Carga de la fuente

Al arrancar verás una o dos líneas de diagnóstico como:

```
Loading source  : /path/to/book.epub
  284,125 characters
  29 recurring proper nouns (gifted, godou, grotesqueries, hana, kanoko, kaya, keiko, kiyoka +21 more)
```

La línea de nombres propios es una comprobación rápida de que el script identificó los
nombres de personaje correctos en la fuente. Escanea palabras con mayúscula inicial que aparecen
varias veces en medio de una frase: nombres de personajes, topónimos, términos propios de la historia
como `Grotesqueries` o `Spirit Sight`. La alineación los usa para
relajar el listón de aceptación de límites cuando una palabra del fragmento destrozada por el ASR (`coodo`,
`youth`, `caia`) queda junto a uno de esos nombres en la fuente. Si la muestra
parece incorrecta (faltan personajes principales, está llena de palabras comunes), la alineación
seguirá funcionando pero puede necesitar más `[e]dit` manuales en los límites de nombres propios.

El script también limpia automáticamente dos artefactos recurrentes de la extracción de EPUB durante
la carga de la fuente:
- **Fallos letra-dígito-letra** (`thos1e` → `those`, `Kars1a` → `Karsa`)
  causados por el OCR al insertar dígitos sueltos entre letras.
- **Diacríticos partidos** (`fianc é` → `fiancé`, `fiancé e` → `fiancée`) causados
  por la extracción de EPUB al separar los diacríticos latinos precompuestos de sus raíces.

No tienes que hacer nada para esto: ocurre automáticamente.

## Alineación inicial (introducción del audio frente a los preliminares del texto)

Los audiolibros y los textos fuente rara vez empiezan en el mismo punto:
- El audio puede abrir con **créditos** ("Audible presents…"), una **introducción del narrador**
  o un **anuncio de capítulo** que no está en el texto.
- El texto puede abrir con **páginas de copyright**, un **índice** o una
  **dedicatoria** que el narrador no lee.

En una ejecución nueva, la herramienta compare hace una **búsqueda de anclaje automático**: escanea
toda la fuente para encontrar la posición que mejor coincide con la primera entrada sólida del JSONL,
y empieza la alineación ahí.

Verás una salida como:
```
🔍 Searching for initial alignment anchor...
  ✓ JSONL entry 3 anchors at source word 287 (62.1% match)
  Source preview: "It was a warm Saturday morning when Sakuta..."

  ⚠ 3 JSONL entries before the anchor have no matching source text
     (likely audio intro/credits not present in the text)
     ✓ Auto-kept as-is (use --review-preanchor to review them individually)
```

### Cuando el anclaje automático se equivoca

Si el anclaje cae en el lugar equivocado (por ejemplo, una frase se repite en el libro y
encontró la instancia incorrecta), anúlalo manualmente:

```bash
# Skip the first 412 words of source (e.g., copyright + TOC + dedication)
app/env/bin/python alexandria_compare.py \
  --jsonl metadata.jsonl --source book.txt --source-start 412

# Or search for a distinctive opening phrase
app/env/bin/python alexandria_compare.py \
  --jsonl metadata.jsonl --source book.txt \
  --source-start-text "It was a warm Saturday morning"

# Or disable auto-anchor entirely and start at word 0
app/env/bin/python alexandria_compare.py \
  --jsonl metadata.jsonl --source book.txt --no-auto-anchor
```

### Entradas anteriores al anclaje

Las entradas del JSONL anteriores al punto de anclaje no tienen texto fuente
correspondiente. Por defecto se conservan tal cual (ya que suelen ser material de introducción
que solo existe en el audio y que no quieres sobrescribir). Usa `--review-preanchor` para
revisar cada una individualmente.

Nota: el anclaje automático solo se ejecuta en la **primera** ejecución. Al reanudar desde un
punto de control, se usa directamente la posición guardada del cursor: tus decisiones de alineación
anteriores se conservan.

## Realineación a mitad de flujo

Incluso con un buen anclaje inicial, la fuente y el audio pueden desviarse
a mitad del libro (por ejemplo, el audio lee un título de capítulo que no está en el texto, o
se salta un pasaje que sí está). El script lo detecta automáticamente:

- Cuando una entrada puntúa mal contra su tramo de fuente cercano al cursor, el
  script hace una **búsqueda más amplia por delante** (hasta 3.000 palabras).
- Si encuentra una coincidencia fiable, el cursor **salta hacia delante** hasta ella.
- Si no existe ninguna coincidencia fiable por delante, la entrada se marca como
  sin equivalente en la fuente y se muestra con:

  ```
  ORIGINAL  :  (no matching passage found in source within search range)
  ```

  Para estas entradas, la acción `[a]` de aceptar el original está desactivada; usa
  `[k]` para conservar, `[e]` para editar o `[s]` para saltar.

Esto detecta normalmente: anuncios de capítulo que solo se dicen en el audio, créditos
finales del audio y cualquier inserción del narrador que solo existe en el audio.

## Interfaz de revisión

Cada entrada que queda por debajo del umbral se muestra así:

```
────────────────────────────────────────────────────────────────────────
Entry 42/4713  sample_0042.wav  7m 1s → 7m 11s  Match: 78.3%
────────────────────────────────────────────────────────────────────────
ANNOTATED :  He *walked* ... slowly across the room .... pausing to look
ORIGINAL  :  He walked slowly across the room, pausing to look

  TRANS diff:  He walked [slowly] across the room [pausing] to look
  ORIG  diff:  He walked [slowly] across the room [,] [pausing] to look

  [a] accept original   [k] keep annotation   [e] edit manually   [s] skip for now   [q] quit & save
  >
```

**Colores del diff:**
- Palabras `RED` — están en la transcripción pero no en el original (sobran o están mal)
- Palabras `GREEN` — están en el original pero no en la transcripción (faltan)
- Normal — palabras que coinciden

**ANNOTATED** muestra el texto completo, incluidas las marcas TTS (`...`, `*word*`).
Las filas del diff comparan palabras limpiadas/normalizadas para que el ruido de puntuación no
enturbie la comparación.

## Tus elecciones

| Tecla | Qué ocurre |
|---|---|
| `a` | Reemplaza el texto del JSONL por el texto plano de la fuente. **Elimina todas las marcas de prosodia.** |
| `m` | **Combina**: usa las palabras de la fuente pero conserva los `*emphasis*` y las pausas `...` del LLM. Solo se muestra cuando la anotación tiene marcas que vale la pena conservar. |
| `k` | Conserva el texto anotado actual exactamente como está |
| `e` | Escribe un reemplazo propio (escribes exactamente lo que va al JSONL) |
| `s` | Salta esta entrada; queda sin cambios y no volverá a aparecer en esta sesión (se mostrará en la próxima ejecución) |
| `q` | Guarda el progreso y sale; vuelve a ejecutar el mismo comando para continuar |

## Que la voz no se vuelva plana: prefiere `[m]erge`

El LLM del preparer añade marcas de prosodia al JSONL:
- `*word*` — **énfasis** en palabras acentuadas
- `...` — pausa natural, `....` — pausa más larga

Estas marcas son lo que hace expresiva a la voz TTS entrenada. Si usas `[a]ccept
original` en muchas entradas, reemplazas el texto anotado por las palabras planas de la
fuente, **eliminando todas las pistas de prosodia** y empujando la voz hacia una salida plana/monótona.

La opción `[m]erge` corrige las palabras y conserva las marcas:

```
ANNOTATED :  HE *WALKED* SLOWLY... ACROSS THE ROOM
ORIGINAL  :  He walked slowly across the room,
MERGED    :  He *walked* slowly ... across the room,
            (correct spelling + capitalisation + the LLM's emphasis and pause)
```

La combinación maneja varias formas complicadas automáticamente:
- **Énfasis de varias palabras** — `*Trull Sengar*` se expande a `*Trull* *Sengar*`
  antes de la alineación, así ambos nombres llevan la marca a la grafía de la fuente.
- **Cadenas unidas por puntos** — fragmentos como `YOU...*WALKED*...HOME` (donde el LLM
  usó `...` como separador de palabras en lugar de espacios) se dividen correctamente
  para que cada `*word*` sobreviva a la combinación.
- **Nombres destrozados por el ASR** — una sola palabra del fragmento como `*Tralesengar*` que cubre una
  frase de dos palabras de la fuente, `Trull Sengar`, difundirá su énfasis entre
  ambas palabras de la fuente mediante concat-fuzzy.

El énfasis aún puede perderse cuando la transcripción errónea del ASR es tan grave que
ni el ratio difuso por palabra ni el concatenado superan el umbral; esos casos
aparecen en la revisión y puedes corregirlos con `[e]dit`.
Las pausas entre palabras se conservan en cualquier caso.

Al final de la sesión, si aceptaste muchos originales sin combinar,
verás un aviso que sugiere usar `[m]erge` la próxima vez.

## Pausar y reanudar

Pulsa `q` en cualquier prompt de revisión para detenerte. Se guarda automáticamente un archivo de
punto de control junto a tu JSONL:

```
.metadata_compare_progress.json
```

Vuelve a ejecutar exactamente el mismo comando para retomar donde lo dejaste:

```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source original_book.epub
```

Para empezar completamente de cero (descartar todas las decisiones anteriores):
```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source original_book.epub \
  --reset
```

## Salida

El JSONL corregido se escribe por defecto en `metadata_corrected.jsonl` (junto al
JSONL de entrada). Las entradas en las que elegiste `[k]` conservar o que se aprobaron automáticamente quedan
sin cambios. Solo las entradas de `[a]` aceptar y `[e]` editar tienen un texto distinto.

Puedes indicar otra ruta de salida:
```bash
python alexandria_compare.py \
  --jsonl dataset_temp/metadata.jsonl \
  --source book.epub \
  --output my_reviewed_dataset.jsonl
```

## Ajuste del umbral

El umbral por defecto de `0.90` significa que las entradas en las que ≥ 90 % de las palabras coinciden con el
original se aprueban en silencio. Bájalo para ver más entradas:

```bash
# See everything with less than 95% match
python alexandria_compare.py --jsonl ... --source ... --threshold 0.95

# See everything, no auto-approve
python alexandria_compare.py --jsonl ... --source ... --review-all
```

## Cómo funciona la alineación

La herramienta mantiene un **cursor** en la lista de palabras de la fuente. Para cada entrada del JSONL:
1. Quita las marcas de anotación TTS (`...`, `*word*`) de la transcripción
2. Normaliza tanto la transcripción como las palabras de la fuente (minúsculas, sin puntuación)
3. Desliza una ventana de ±200 palabras alrededor del cursor, puntuando cada posición con
   coincidencia difusa de secuencias
4. Avanza el cursor hasta el final de la mejor coincidencia

Esto significa que **la precisión de la alineación depende de que la transcripción esté en orden**,
lo cual siempre ocurre porque se produjo secuencialmente a partir del audio. Si
fragmentos individuales tienen errores de ASR muy graves, la alineación puede desviarse un poco;
lo notarás como puntuaciones de coincidencia inesperadamente bajas en las entradas circundantes.

## Resumen al terminar

```
Complete!
  Auto-approved (≥90%) : 4512
  Kept annotation      : 83
  Accepted original    : 78
  Edited manually      : 12
  Skipped              : 28
  Re-run to review 28 skipped entries.
```
