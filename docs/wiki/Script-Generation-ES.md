# Generación de guion

[English](Script-Generation.md) · [日本語](Script-Generation-JA.md) · [中文](Script-Generation-CN.md) · Español

Un modelo de lenguaje convierte el texto del libro en un guion: una lista de líneas, cada una con un hablante y
una instrucción de interpretación para la voz.

## El pipeline

1. **Sube** un libro (`.txt`, `.md` o `.epub`) en la pestaña Script. Los archivos EPUB se convierten
   a texto plano al subirlos.
2. **Generate Annotated Script** ejecuta tres pasadas sobre el texto:
   1. **Split** — divide el texto en narración y líneas habladas. Setup → **Step 1:
      dialogue detection** elige cómo:
      - **Auto** (recomendado): usa las comillas cuando dividen claramente un fragmento y
        pasan una comprobación; si no, pregunta al modelo. Todos los resultados publicados usaron esto.
      - **Quote marks only**: nunca pregunta al modelo.
      - **Quote-aware**: como las comillas, pero los términos entrecomillados dentro de la narración se quedan
        como narración.
      - **Model only**: pregunta siempre al modelo.
   2. **Attribute** — da a cada línea hablada un hablante de la lista de reparto del libro, usando el
      texto circundante.
   3. **Instruct** — escribe una instrucción de interpretación para cada línea.
3. **Review** (opcional): una segunda pasada del modelo que corrige errores habituales (ver abajo).
4. **Voices** — cada hablante del guion recibe una tarjeta de voz en la pestaña Voices.

Cada pasada tiene puntos de control. Una ejecución se puede pausar, reanudar tras un fallo o un corte de luz, guardar como
instantánea de la parte terminada, o empezar de nuevo sin perder tus ajustes. La línea de actividad
bajo el botón Generate muestra el paso actual, la unidad, el intento y el tiempo restante.

Antes de la generación, **Review character identities and source quotes** te deja
aprobar identidades de personaje estables, alias y evidencia de origen para el reparto de este libro.
Admite reconciliación explícita de importaciones, decisiones de combinar/dividir y deshacer
exacto. Consulta la [guía del reparto de personajes](../character-roster.md) para el flujo de trabajo,
las coordenadas de origen y el comportamiento de recuperación.

## Finalización y recuperación de subidas

Una subida solo es reutilizable después de que su texto esté completamente escrito. Las importaciones de EPUB
también terminan su compañero de procedencia antes de poner el texto a disposición. Los archivos
pendientes siguen siendo privados y cuentan contra el límite de almacenamiento de subidas. Las subidas existentes
se reutilizan por contenido o se conservan con sus nombres originales cuando los nombres chocan.

Una vez que una fuente completada es visible, cancelar su solicitud de subida o no conseguir
seleccionarla como libro activo deja esa fuente disponible. Puede que otra solicitud
ya la haya seleccionado. Una actualización de procedencia completada de una fuente reutilizada también
se conserva; un fallo antes de escribirla conserva la procedencia anterior.
La respuesta de error no significa que la fuente se haya borrado. Comprueba las subidas reutilizables
antes de volver a subir. Los fallos de validación de la selección dejan el libro anterior sin cambios.
Un error de disco tardío o una cancelación pueden llegar después de que se haya guardado una selección; refresca
y comprueba el libro actual antes de reintentar.

Un fallo del proceso antes de la publicación puede dejar un archivo privado `.source-upload-*.part`
y un compañero `*.publication.json` sin emparejar. Siguen contando para la cuota,
no se ofrecen como fuentes reutilizables y no se borran automáticamente. Un reintento
omite un compañero huérfano en lugar de adjuntarlo a texto no relacionado. Para recuperarte, detén AA2 y
conserva copias de estos archivos para inspección, y luego vuelve a subir la
fuente original. No renombres un stage privado a la biblioteca reutilizable: puede
contener solo parte de la subida. Quita los archivos conservados solo después de confirmar que ningún
trabajo activo o guardado depende de ellos y de conservar lo necesario para la recuperación.

La publicación requiere un movimiento atómico que rechace un destino existente. La
implementación usa el renombrado de Windows, `renameat2(RENAME_NOREPLACE)` de Linux, o
`renamex_np(RENAME_EXCL)` de macOS. Una operación no disponible o un sistema de archivos no compatible
falla sin recurrir a una sobrescritura ni a una copia parcialmente visible. La CI nativa
ejercita la publicación completa, las colisiones y los límites de muerte del proceso en
Linux, Windows y macOS; esto no es una garantía contra cortes de luz ni para todos los sistemas de archivos.

## Formato del guion

El guion es un array JSON:

```json
[
  {"speaker": "NARRATOR", "text": "The door creaked open slowly.", "instruct": "Calm, even narration."},
  {"speaker": "ELENA", "text": "Ah! Who's there?", "instruct": "Startled and fearful, sharp whispered question, voice cracking with panic."},
  {"speaker": "MARCUS", "text": "Haha... did you miss me?", "instruct": "Menacing confidence, low smug drawl with a dark chuckle, savoring the moment."}
]
```

- **`speaker`** — `NARRATOR`, o el nombre de un personaje en mayúsculas de la lista de reparto.
- **`text`** — exactamente lo que se dice. Las etiquetas de atribución ("she said") pertenecen a la narración,
  y las comillas se eliminan: el campo speaker registra que una línea es habla.
- **`instruct`** — una dirección de voz breve de una o dos frases (unas 8-15 palabras)
  que describe emoción, interpretación y calidad vocal.

## Escribir buenas instrucciones

El campo `instruct` se envía al motor de voz como dirección vocal. Combina hasta tres cosas:

1. **Tono emocional** — lo que siente el personaje: furioso, temeroso, triunfante, vacío,
   desesperado, divertido.
2. **Interpretación** — cómo lo dice: susurrado, arrastrado y grave, entrecortado y seco, mesurado, con intensidad
   creciente.
3. **Calidad vocal** — lo que hace la voz: quebrándose, áspera, tensa, cruda, con aire,
   forzada.

No todas las líneas necesitan las tres. Una línea de poca importancia puede ser simple ("Casual, offhand remark.");
reserva las instrucciones ricas y por capas para los picos emocionales.

**Buenos ejemplos**

- `"Cold fury, barely contained, voice tight."`
- `"Bright eager excitement, words tumbling out."`
- `"Menacing confidence, low smug drawl with a dark chuckle."`
- `"Devastated, voice cracking, struggling to hold composure."`
- `"Sharp whispered warning, urgent and hushed."`

**El narrador** usa `"Neutral, even narration."` por defecto. En un cambio de ánimo de la escena, se permite
un modificador: `"Tense, clipped narration."`, `"Quiet, somber narration."`, `"Wry,
light narration."`, y el mismo tono se mantiene en las líneas del narrador dentro de esa escena.

**Evita**

- Acciones físicas ("trembling", "leaning forward"): describe la voz, no el cuerpo.
- Dos palabras que significan lo mismo ("sneering contempt, dripping with disdain").
- Calificadores débiles ("slightly", "a bit"): diluyen la dirección.
- Palabras de velocidad a secas ("fast", "slow"): muestra el ritmo mediante la energía ("urgent intensity",
  "heavy, weighted delivery").
- Palabras que describen la voz en sí ("deep", "raspy"). Luchan contra la voz elegida; el
  ancla de identidad de la voz las elimina.

**Descriptores de voz que funcionan:** voice cracking, hollow, seething, flat, low, cold, numb,
drained, gravelly, tight, raw, breathy, hushed, booming, clipped, strained. La
[Referencia de voces](Voice-Reference-ES.md) tiene un vocabulario mucho más largo.

### Sonidos no verbales

Los sonidos se escriben como texto real y pronunciable, nunca como etiquetas entre corchetes ni tokens especiales:

- jadeos: "Ah!", "Oh!" con una instrucción como "Fearful, sharp gasp.";
- suspiros: "Haah...", "Hff...";
- risas: "Haha!", "Ahaha...";
- llanto: "Hic... sniff...";
- exclamaciones: "Mmm...", "Hmm...", "Ugh...".

Nada impronunciable (etiquetas desnudas, kana pictográfico) llega al motor de voz.

## Ajustes del modelo de lenguaje

Configúralos en la pestaña Setup:

- **Base URL**, **API Key** (`local` para un servidor local, o `env:NAME` para leer una variable
  de entorno) y **Model Name**.
- **Reasoning effort**: usa *low* para modelos de razonamiento. No prohíbas `<think>`: el razonamiento bajo
  midió mejor que el razonamiento desactivado en todos los modelos que razonan.

### Valores de muestreo por defecto

| ajuste | por defecto | notas |
|---|---|---|
| Temperature | 0.6 | ajuste general |
| Step 1 / 2 / 3 temperatures | 0.1 cada uno | las tres pasadas usan sus propias temperaturas |
| Top P | 0.8 | |
| Top K | 0 (desactivado) | |
| Min P | 0 | |
| Presence penalty | 0 | |
| Banned tokens | vacío | separados por comas; deja `<think>` fuera |

Los números de precisión publicados se midieron a temperatura 0, donde una ejecución repetida da la
misma respuesta en cada línea.

### Cómo se divide el texto

- **Step 1** envía unos 3,000 caracteres por solicitud (Setup → **Step 1: text per request**).
- **Step 2** envía 25 líneas por solicitud, más unos 2,000 caracteres de texto circundante
  alrededor de ellas. Ese bloque circundante es lo que añade el prompt `michel2_full`, y es
  la mayor mejora única medida. Ponerlo a 0 reproduce las puntuaciones anteriores al
  2026-09-19.
- La lista de reparto se lleva de solicitud en solicitud para que los nombres sigan siendo consistentes.
- Si una solicitud falla, el **rescate de contexto** la reintenta con ventanas más amplias (2,000, 4,000 y 6,000
  caracteres, dos reintentos cada una).

## Revisar un guion

Después de la generación, **Review Script** ejecuta una segunda pasada del modelo que corrige:

1. **Etiquetas de atribución dentro del diálogo**: quita "said he", "she replied" y similares.
2. **Narración asignada a un personaje**: la separa como NARRATOR.
3. **Diálogo oculto en la narración**: lo extrae como línea del personaje.
4. **Narración dividida demasiado finamente**: combina líneas cortas consecutivas del narrador de la misma
   escena.
5. **Instrucciones inválidas**: convierte acciones físicas en equivalentes vocales y mantiene las instrucciones
   ricas válidas.

**Contextual Review** procesa el guion en ventanas solapadas (4 líneas a cada lado por
defecto), así que el modelo ve las líneas circundantes. Es más preciso para la consistencia de hablante y tono.

**Labels-only review** (opcional: `prompts.review_labels_only: true`, o
`review_script.py --labels-only`) pide solo el hablante y la nota de interpretación de cada línea y
nunca reescribe el texto, así que una coma cambiada o una etiqueta de habla movida ya no pueden costarle a un lote
sus correcciones de hablante. No puede dividir ni combinar líneas; la revisión normal sí
([detalles](../features/labels-only-review.md)).

**Batch review** puede revisar toda una serie de guiones guardados a la vez, opcionalmente encontrando
apodos primero y ejecutando dos pasadas (hacia delante y luego hacia atrás).

Herramientas relacionadas: **Find Nicknames** descubre nombres alternativos para los personajes, y **Edit
aliases** te deja corregirlos, para que "Betty" y "BEATRICE" compartan una sola voz.

## Prompts

Cada pasada lee su propio archivo de prompt en `app/`:

- `default_prompts_segment.txt` (paso 1), `default_prompts_attribute.txt` (paso 2),
  `default_prompts_instruct.txt` (paso 3);
- `review_prompts.txt` para la pasada de revisión.

Cada archivo contiene un mensaje de sistema, una línea `---SEPARATOR---`, y luego un mensaje de usuario con
los marcadores `{roster}` y `{batch}`.

Para cambiarlos:

- **En Setup**: elige la variante del prompt de atribución, edita el texto y **Save as preset**.
  Los presets sobreviven a los reinicios. `michel2_full` es el valor por defecto. `michel2_spoken`, que pregunta solo
  por las líneas habladas, puntuó 70.1% frente a 63.6% en Qwen3-14B sin adaptador (un conjunto
  local de referencia de tres libros) con unas 3× menos tiempo en el paso 2 ([detalles](../features/spoken-only-attribution.md)).
  Otros modelos aún no se han medido; mantén el prompt con el que se entrenó un adaptador de atribución.
  Pasa el cursor o enfoca un ajuste o su etiqueta durante medio segundo para ver ayuda y, cuando se ha medido, una recomendación.
- **What the model will see** muestra los mensajes exactos que enviaría el paso 2.
- **Reset to Defaults** recarga los archivos sin reiniciar.

Cambiar el prompt cambia la medición: una puntuación publicada pertenece a su variante de prompt.
Ver [Prompts y adaptadores](Prompts-and-Adapters-ES.md).

### Libros en otros idiomas

Los prompts por defecto están escritos para inglés. Para otros idiomas, adáptalos a las
convenciones de ese idioma (comillas francesas « »), corchetes japoneses (「」), las formas habituales de
atribuir el habla, y ajusta el **Language** del TTS para que coincida.

## Qué modelo usar

Las recomendaciones medidas, por memoria de GPU y con puntuaciones, están en
[Qué modelo para tu tarjeta](Which-Model-For-Your-Card-ES.md) y en los
[Modelos de LLM recomendados](../../README_ES.md#modelos-de-llm-recomendados) del README. En resumen:

- **Qwen3.8-27B** en UD-Q3_K_XL es el modelo local más preciso que cabe en una tarjeta de 16 GB.
- **Qwen3.6-35B-A3B** es la opción rápida: aproximadamente tan rápido como un modelo 8B y unos 11 puntos más
  preciso.
- **DeepSeek v4-pro** (alojado) es el modelo más preciso medido.

## Guiones guardados

Los guiones se guardan y cargan desde la sección **Saved Scripts** de la pestaña Script:

- **Save** guarda el guion y sus ajustes de voz en `scripts/`.
- **Load** los restaura, para que puedas seguir editando o regenerar líneas concretas.
- **Repair** previsualiza correcciones de hablantes o contenido en un guion guardado, y conserva una copia de seguridad del
  original.
