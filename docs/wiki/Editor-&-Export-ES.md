# Editor y exportación

[English](Editor-&-Export.md) · [日本語](Editor-&-Export-JA.md) · [中文](Editor-&-Export-CN.md) · Español

La pestaña Editor muestra tu audiolibro línea a línea ("fragmentos"), con herramientas para editar, volver a renderizar,
escuchar y comprobar cada uno. La pestaña Result exporta el libro terminado.

## El editor de fragmentos

Cada fragmento muestra:

- **Status**: Pending (aún sin audio), Done o Error;
- **Speaker**: editable;
- **Text**: la línea hablada o la narración, editable;
- **Instruct**: la instrucción de interpretación, editable.

Cada fragmento también se puede reproducir, volver a renderizar por separado, e insertar o borrar.

### Editar una línea

1. Haz clic en un campo y edítalo; los cambios se guardan automáticamente.
2. Haz clic en el botón **generate** del fragmento para volver a renderizar solo esa línea; útil para arreglar una línea
   tras una edición, probar otra instrucción o comparar voces.
3. O haz clic en **Render Pending** para renderizar todos los fragmentos que aún necesitan audio.

## Pronunciación

### Corregir una pronunciación

Abre **Pronunciation**, carga las reglas compartidas y **Preview counts and line voices**.
Elige una línea y **Load line rules**. Introduce la grafía original y el reemplazo hablado,
luego **Find original spelling** y añade la coincidencia deseada como borrador. Las coincidencias se refieren al
texto preparado para el habla, después de la limpieza de indicaciones y símbolos. El guion en sí no cambia.

Para un escaneo acotado de una lista de palabras en inglés, elige **Discover English homographs** después de cargar
las reglas de línea. Encuentra lead/read/wind usando los límites de palabra existentes de coincidencia exacta,
no análisis lingüístico exhaustivo, inferencia de significado ni detección del idioma de la prosa.
El idioma TTS resuelto debe ser inglés.
Introduce un reemplazo hablado junto a una aparición y elige un significado/ejemplo para crear
un borrador no aprobado con una nota de significado elegida por el usuario. Los clics repetidos no añaden otro
borrador a un tramo ya incluido en borrador o aprobado. Revisa y aprueba explícitamente; el descubrimiento
no genera audio, no guarda reglas ni cambia texto/tomas. Los resultados truncados omiten las coincidencias posteriores.

Revisa el borrador, elige **Approved** y **Preview line** antes de **Save line rules**.
Las reglas en borrador y las rechazadas no afectan al habla. Las reglas aprobadas de aparición tienen prioridad sobre las
reglas compartidas para esa aparición; los reemplazos insertados nunca se sustituyen una segunda vez.
Las restricciones opcionales seleccionan el hablante declarado, la voz o ensemble asignado exacto,
el idioma o el modo de motor. Una pista fonética necesita un respaldo hablado explícito para motores de
texto plano; si no, la vista previa informa de que se omitió.

Guardar las reglas de línea espera a que el trabajo activo termine o se cancele. Solo un cambio efectivo
en el habla hace que una línea quede pendiente, y su archivo de audio anterior se conserva. Las notas y los borradores inactivos
mantienen el audio existente. Cambiar el idioma o el modo de motor también deja pendientes las líneas condicionales afectadas.
La vista previa no carga un modelo TTS; las audiciones anteriores generan audio desechable usando
la voz de línea elegida. Escucha una audición y la línea renderizada para juzgar la lectura.

Las reglas pertenecen a las identidades de línea actuales del Editor. Reordenar una línea conserva sus reglas;
editar su texto requiere revisar las nuevas coincidencias. Cargar o reconstruir un guion guardado crea
líneas nuevas y no transfiere las reglas de aparición. Las apariciones aprobadas solapadas y las coincidencias
obsoletas se rechazan. Una toma generada registra las decisiones aplicadas y omitidas con su
revisión de audio, así que notas de revisión posteriores no pueden reetiquetar una interpretación anterior.

## Renderizar audio

- **Render Pending** renderiza todos los fragmentos que aún no tienen audio.
- **Regenerate All** vuelve a renderizar todos los fragmentos.
- Con el TTS integrado (modo `local`), ambos usan renderizado por lotes, unas 3-6× el tiempo real. Con
  un servidor TTS externo envían una solicitud por línea en paralelo. Ver
  [Generación por lotes](Batch-Generation-ES.md).
- Ambos respetan el bloqueo de GPU: no empiezan mientras la generación de guion está usando la tarjeta,
  salvo que el modelo de lenguaje esté marcado como no presente en esta GPU.
- **Cancel** detiene un renderizado; los fragmentos terminados se conservan.

## Comprobar el audio

- **Check Voices** compara cada fragmento renderizado con la voz de referencia de su hablante (similitud
  ECAPA) y marca los fragmentos que se desviaron. Marca **flagged only** para ver solo esos.
- **Text integrity** muestra, palabra por palabra, dónde difiere el guion del texto de origen.
- Cada archivo generado se valida como audio real; un archivo roto se marca en su fragmento.

## Escuchar

- **Play** un solo fragmento con su botón de reproducción.
- **Play Sequence** reproduce los fragmentos en orden, con pausas naturales:
  - entre hablantes distintos: 500 ms por defecto (**Speaker Change Pause**);
  - entre líneas del mismo hablante: 250 ms por defecto (**Same Speaker Pause**).

  Ambos se ajustan en la pestaña Setup.

## Instantáneas de tomas guardadas

Inspecciona una línea renderizada y abre **Saved take snapshots**. Elige **Save this take**
antes de cambiar sus palabras, voz o semilla, o de regenerarla. Esto guarda una copia inmutable
del audio seleccionado más su texto registrado, hablante, interpretación, contador de semilla y evidencia
disponible. Usa **This line's snapshots** para escuchar copias, o **All project snapshots**
para encontrar copias de líneas y generaciones de libro anteriores. La reproducción verifica los bytes
guardados y deja intacto el audio de la línea seleccionada.

**Protect** impide el borrado. **Delete with undo** oculta una instantánea de la reproducción pero
conserva sus bytes; **Undo deletion** la restaura. **Remove permanently…** es una acción separada y
confirmada que libera almacenamiento y no se puede deshacer. Los archivos de instantánea actuales, referenciados y
protegidos no se pueden quitar. No se ejecuta ninguna poda automática.

El archivo del proyecto permite 16 registros por identidad de libro/línea, 4,096 registros y 1 GiB
de carga en total. Cada copia está limitada a 64 MiB, mono/estéreo y diez minutos.
Los metadatos están limitados a 128 KiB por registro y 16 MiB por manifiesto, más una escritura
pendiente de metadatos acotada a 16 MiB. Las copias eliminadas e interrumpidas cuentan hasta que se quitan
explícitamente. Una copia fallida o interrumpida nunca reemplaza el audio seleccionado; su registro
incompleto cargado sigue visible para su limpieza. Los metadatos de archivo desconocidos o dañados
se conservan para inspección en lugar de repararse o podarse en silencio.

**Automatic take history** está desactivado por defecto. En Editor, abre esa sección, elige
**Refresh setting** y luego **Turn on** para conservar las interpretaciones seleccionadas anteriores antes de
la regeneración. Los límites del archivo siguen aplicándose: si la toma anterior no se puede conservar de forma segura,
la regeneración se rechaza antes de reemplazarla. **Turn off** detiene las nuevas instantáneas automáticas;
las instantáneas existentes se quedan, y una toma anterior pendiente sigue protegida hasta que su reemplazo
tenga éxito.

En un host compatible, las instantáneas listas con evidencia de restauración para el libro y la línea
actuales muestran **Check & restore**. El botón inicia una comprobación de elegibilidad; no
garantiza que la instantánea se pueda restaurar. La restauración verifica el audio guardado y
sus dependencias de generación originales contra la línea actual, conserva la toma
desplazada y selecciona la interpretación guardada sin generar habla nueva. Si la toma desplazada
no se puede conservar de forma segura, la restauración se rechaza. No
cambia el guion declarado para encajar con una instantánea incompatible. La restauración verificada está
disponible para CustomVoice, VoiceClone y LoRA locales integrados en hosts Linux compatibles.
La interpretación guardada necesita evidencia de publicación nativa de un renderizado con ventanas de síntesis
y recuperación activadas. Los bytes actuales de la referencia de clonación y la generación del adaptador LoRA
deben coincidir con las dependencias de ejecución registradas. Las instantáneas antiguas sin esa evidencia
siguen disponibles para escuchar.

Las grabaciones heredadas listas y no eliminadas, o las instantáneas sin evidencia de restauración
coincidente, siguen disponibles para escuchar. Los cambios en el texto, la voz, los ajustes de modelo u otras dependencias registradas pueden
hacer que una instantánea no sea elegible para restauración. Cargar un guion guardado crea nuevas identidades
de línea; las instantáneas antiguas se quedan en **All project snapshots** y nunca se asignan a
líneas nuevas por posición o texto.

## Combinar

Haz clic en **Merge All** para unir todos los fragmentos con audio válido en un audiolibro, en orden,
con las pausas anteriores. El silencio al principio y al final de cada línea (aire muerto; en voces LoRA una
mediana de 310-340 ms) se recorta cuando se unen los fragmentos, así que las pausas que configuras son las
pausas que oyes. El resultado aparece en la pestaña Result.

## Opciones de exportación

### El audiolibro combinado

- Un MP3 a 128 kbps con todas las voces y las pausas naturales.
- Descárgalo desde la pestaña **Result**; el archivo es `cloned_audiobook.mp3`.

### M4B con capítulos

**Export M4B** crea un archivo de audiolibro con marcadores de capítulo. Puedes definir **Title**,
**Author**, **Narrator**, **Year**, **Description** y **Cover Image**, y elegir
**Per-chunk chapters**.

### Comprobaciones de integridad de exportación

La pestaña Result comprueba el audio terminado de MP3, M4B y capítulos antes de reemplazar una entrega
anterior. Estas comprobaciones comparan el audio codificado con un plan de tramas de origen independiente,
lo decodifican para detectar muestras dañadas o no finitas, y comprueban los marcadores de capítulo del M4B y los metadatos
del libro declarados. Una exportación fallida o cancelada conserva la entrega anterior.

En **Export integrity checks**, define el umbral de silencio y el hueco inesperado máximo
para la próxima exportación. El silencio largo produce un aviso por defecto; activa **Reject exports
with unexpected long silence** para rechazarlo en su lugar. Las pausas entre líneas declaradas quedan
excluidas. Estas comprobaciones no certifican el contenido del habla ni la pronunciación.

**Show checks for this book** muestra las comprobaciones completadas por separado de los intentos rechazados,
incluidos los recuentos de tramas y los rangos de tiempo de silencio o de capítulo. Los resultados se conservan para esta
sesión de la app, hasta 32 entregas y 16 intentos rechazados por tipo de exportación. La vista omite los
recibos cuando cambian los metadatos del archivo; abrirla no recalcula hashes ni decodifica el audio.
Sus valores SHA-256 describen los bytes en el momento de la comprobación completada. La publicación de la exportación
en sí comprueba los bytes completos del archivo y la identidad del origen.

Audacity comprueba los miembros del ZIP, las cabeceras WAV, las tramas exactas de pista, el LOF/las etiquetas y los CRC del archivo.
Sus huecos de hablantes inactivos son intencionados y no se clasifican como narración faltante.

### Archivos de capítulo

**Export chapters** escribe cada capítulo como su propio archivo MP3 o WAV, con nombre según una plantilla como
`{chapter_number} - {chapter_name}`. Las opciones incluyen relleno de números, nombres de libro y serie,
volumen, una lista de capítulos, archivos por fragmento, solo capítulos cambiados y una vista previa de los nombres
de archivo. Los archivos y un zip de ellos están en `chapter_exports/`.

### Líneas individuales

Cada línea también se guarda como su propio archivo en `voicelines/`, numerado en orden de lectura y nombrado por
hablante, listo para importar en software de audio:

```
voicelines/
├── voiceline_0001_narrator.mp3
├── voiceline_0002_elena.mp3
├── voiceline_0003_marcus.mp3
└── ...
```

### Audacity

**Export to Audacity** (pestaña Result) crea una pista WAV por hablante para edición multipista:

```
audacity_export.zip
├── project.lof       # open in Audacity to import every track at once
├── labels.txt        # import via File > Import > Labels for line annotations
├── narrator.wav      # full-length track with only the NARRATOR's audio
├── elena.wav         # full-length track with only ELENA's audio
├── marcus.wav        # full-length track with only MARCUS's audio
└── ...
```

Para usarlo:

1. Descarga y descomprime `audacity_export.zip`.
2. Abre `project.lof` en Audacity; se importa la pista de cada hablante.
3. Importa `labels.txt` mediante **File > Import > Labels** para las anotaciones de línea.

Cada pista se rellena con silencio hasta la misma longitud, así que reproducirlas todas juntas suena
exactamente como el MP3 combinado. Esto es útil para el volumen por personaje, efectos en un hablante,
ajustar el tiempo entre líneas y postproducción profesional.

## Archivos MP3 rotos o diminutos

Un MP3 de 428 bytes significa que falta ffmpeg o que no puede codificar MP3. El instalador compila ffmpeg en
el entorno de la app, así que normalmente significa que la app se está ejecutando fuera de `app/env`. Iníciala
desde Pinokio (o activa `app/env`) y vuelve a renderizar. Ver
[Solución de problemas](Troubleshooting-ES.md).
