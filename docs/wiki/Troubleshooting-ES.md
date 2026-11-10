# Solución de problemas

[English](Troubleshooting.md) · [日本語](Troubleshooting-JA.md) · [中文](Troubleshooting-CN.md) · Español

Empieza por los logs. Casi siempre nombran el problema:

- la **línea de actividad** bajo el botón Generate de la pestaña Script: qué espera una ejecución, el
  intento y el tiempo restante;
- `logs/api/<task>-latest.log`: el log completo de cada tarea en segundo plano (por ejemplo
  `logs/api/audio-latest.log`);
- `logs/llm_responses.log` y `logs/review_responses.log`: cada respuesta del modelo de lenguaje durante
  la generación y revisión de guiones, con el motivo de finalización, los recuentos de tokens y el tiempo empleado. El
  archivo `*.requests.log` junto a cada uno guarda la solicitud exacta detrás de cada respuesta, emparejada por su
  id `request=`, así que una respuesta mala se puede reproducir;
- `logs/api/start.js/latest`: el log del lanzador; `logs/sessions/` agrupa las ejecuciones relacionadas de
  instalación, arranque y ayudantes.

La sección [Solución de problemas](../../README_ES.md#solución-de-problemas) del README cubre el lanzador (Pinokio
que no llega a "Open Web UI"). Esta página cubre el resto.

## Libros y guardados

### Un libro guardado no se abre después de un fallo

Cargar o guardar un libro reemplaza varios archivos a la vez. La app mantiene un diario de cada
cambio, así que si uno se interrumpe (un fallo, un corte de luz, un disco lleno), la app lo termina o
lo deshace la próxima vez que el libro se abre. Normalmente nunca lo notas.

Si los archivos del libro ya no coinciden con el diario, por ejemplo porque otro programa editó
uno a mitad del cambio, la app no adivinará qué versión es la correcta. Se niega a abrir el libro,
y la página Home muestra qué comandos probar. Ejecútalos desde la carpeta de Alexandria con el
Python de la app:

```bash
python app/book_state_recovery.py inspect --hash
python app/book_state_recovery.py finish
python app/book_state_recovery.py quarantine --accept-current-files
```

- `inspect` solo mira. Muestra qué estaba haciendo el cambio, qué archivos difieren de lo que
  esperaba, cualquier carpeta apartada y cualquier resto que dejó un fallo.
- `finish` ejecuta ahora mismo la misma recuperación que haría la app.
- `quarantine --accept-current-files` es el último recurso. Conserva los archivos del libro exactamente como
  están ahora y aparta el diario y la carpeta de trabajo a una
  carpeta `.book-switch-quarantine-*`. No se borra nada.
- Si la app dice que el diario es de una versión anterior, usa
  `migrate-legacy --trust-journal-hashes`.

Deja cualquier carpeta `.book-switch-*` en paz hasta que estés seguro de que no hay ningún cambio en marcha. Una carpeta dejada
por un fallo antes de que se escribiera su diario nunca se limpia automáticamente, así que una vez que el libro
se abre con normalidad puedes borrarla tú. [Guardados y cambios de libro
interrumpidos](../../docs/guides/BOOK_SWITCH_RECOVERY_ES.md) cubre todos los estados de fallo, el resumen
de la página Home y los códigos de salida.

## Generación de guion

### La generación falla o produce basura

- Comprueba que el servidor del modelo de lenguaje está en ejecución y accesible en la **Base URL**; haz clic en
  **Test Connection** en Setup.
- Comprueba que **Model Name** coincide con el modelo que el servidor tiene cargado (el botón de refrescar
  los lista).
- Lee la respuesta real del modelo en `logs/llm_responses.log` (generación) o
  `logs/review_responses.log` (revisión), y la solicitud detrás en el
  `.requests.log` correspondiente.
- Usa un modelo de [Qué modelo para tu tarjeta](Which-Model-For-Your-Card-ES.md), mantén activo el esquema
  JSON y pon el razonamiento en *low*.

### Modelos de razonamiento ("thinking")

Los modelos de razonamiento funcionan bien; de hecho, mejor. Pon **Reasoning effort** en *low* y deja que el
servidor limite el presupuesto. **No** añadas `<think>` a los tokens prohibidos: el razonamiento bajo midió
mejor que el razonamiento desactivado en todos los modelos que razonan.

### Un modelo cargado con muy poco contexto

Una solicitud puede exceder cualquier tamaño de contexto cargado; 8k no está intrínsecamente roto y 32k no es
siempre seguro para tu VRAM. Cuenta el prompt, la respuesta y el presupuesto de razonamiento juntos.

- Lee el error del servidor y los recuentos de tokens antes de cambiar ajustes.
- Reduce el texto, el número de líneas o la ventana de revisión de la solicitud afectada, o aumenta el contexto si
  el modelo y la memoria disponible lo admiten.
- El optimizador de LM Studio usa perfiles protegidos específicos de cada modelo con un respaldo local
  de 8,192 tokens; no siempre selecciona 32k.
- Sigue [Elegir un tamaño de contexto](Setup-and-Serving-ES.md#elegir-un-tamaño-de-contexto) y luego ejecuta
  **Test this book with the LLM**. Una muestra que pasa no prueba que todos los fragmentos posteriores quepan.

### "Idle" durante mucho tiempo

Lee la línea de actividad: nombra el paso, la unidad, el intento y qué está esperando (el
modelo, un backoff por límite de tasa, un reintento).

- Si no dice nada desde hace minutos, el servidor ha dejado de responder: pruébalo desde Setup.
- Una ejecución pausada por **When retries run out → pause** espera a que hagas clic en Resume.

### Pause está en gris

En Windows la app no puede suspender el proceso worker, así que Pause está desactivado ahí. Cancel y
**Resume failed run** siguen funcionando.

### Los nombres de personajes son inconsistentes

- La lista de reparto se lleva de solicitud en solicitud para que los nombres sean consistentes.
- Usa **Find Nicknames** y **Edit aliases** para que distintos nombres de una misma persona compartan una voz.
- Puedes corregir los nombres a mano en el Editor, o con la reparación de hablantes de Saved Scripts.

### Ninguna API

Pon **How requests are sent** en `manual`. La app escribe cada solicitud en
`manual_llm/pending.json`, y la pestaña Script muestra un panel donde copias el prompt y pegas
la respuesta.

## Generación de voz

### La descarga del modelo falla o es muy lenta

- Los modelos TTS (unos 3.5 GB cada uno) se descargan de Hugging Face la primera vez que renderizas.
  Una descarga atascada en 0% es un problema de red, no una app congelada.
- **China o redes restringidas:** apunta las descargas a un espejo antes de lanzar:

  ```
  HF_ENDPOINT=https://hf-mirror.com
  ```

  o añádelo a `start.js` en los parámetros de `shell.run`:
  `env: { HF_ENDPOINT: "https://hf-mirror.com" }`.
- **Límites de tasa (HTTP 429):** crea una [cuenta gratuíta de Hugging Face](https://huggingface.co/join)
  y pon `HF_TOKEN` con tu token de acceso.
- **Una descarga a medias:** borra el archivo parcial de la caché de Hugging Face y prueba
  de nuevo. `download_model.py` obtiene los pesos fuera de la app.

### El modelo no carga

- Lee el primer traceback en `logs/api/audio-latest.log`.
- Asegúrate de que hay suficiente memoria de GPU (ver [Compatibilidad de GPU](../../README_ES.md#compatibilidad-de-gpu) del README).
- Como último recurso, pon **Device** en `cpu`, mucho más lento.
- En una APU AMD la app usa fp32 automáticamente; si forzaste `bf16`, quítalo.

### Una voz de clonación no funciona

- Comprueba que el audio de referencia existe en la ruta configurada.
- El texto de referencia debe ser una transcripción exacta del audio.
- Usa audio limpio de 5-15 segundos; las referencias de menos de unos 7 segundos dan una voz inestable.
- Asegúrate de que la voz diseñada se **guardó**, no solo se previsualizó.

### No se encuentra un adaptador LoRA

- Comprueba que el entrenamiento terminó (pestaña Training).
- Las rutas de adaptador son relativas a la carpeta del proyecto: comprueba que existe
  `lora_models/<adapter_id>`.
- Refresca la lista de adaptadores en la pestaña Voices.

### Una voz entrenada nunca deja de hablar

Es un defecto de la voz entrenada (goal 2.3), normalmente por una tasa de aprendizaje demasiado alta o
sobreajuste. La comprobación de parada lo detecta antes de la promoción. Usa mientras tanto una voz publicada para ese personaje,
y reentrena con una tasa de aprendizaje de 1e-6 (ver la [Guía de entrenamiento](Training-Guide-ES.md)).

### Glitches de Voice Design

- Evita contradicciones entre la descripción y la instrucción ("calm gentle voice" con
  "screaming furiously").
- Mantén la descripción sobre la voz en sí (edad, género, calidad), y deja la emoción y
  la interpretación a la instrucción.

### Servidor TTS externo

- En modo `external`, asegúrate de que el servidor Gradio de TTS está en ejecución en la URL configurada.
- El modo externo renderiza una línea por solicitud; usa el modo `local` para el renderizado por lotes.

## Rendimiento

### El renderizado es lento

1. Haz clic en **Auto-Configure** en Setup.
2. Activa **Compile Codec**: 3-4× más rápido tras un calentamiento único de 30-60 s.
3. Activa **Sub-batching**: menos tiempo de GPU desperdiciado en relleno.
4. Sube **Parallel Workers** si la memoria lo permite.
5. Un modelo de lenguaje en la misma tarjeta reduce a la mitad lo que el TTS puede usar. Si el tuyo está alojado
   en otro sitio, abre **Provider request options** y pon **Runs on this machine's GPU?** en **No**.

Ver [Generación por lotes](Batch-Generation-ES.md).

### Sin memoria

- Baja **Parallel Workers** y **Max Items/Batch**.
- Cierra otros programas que usen la GPU.
- La comprobación de memoria rechaza un lote que no cabe, así que quedarse sin memoria a mitad de una
  ejecución normalmente significa que otro proceso tomó la tarjeta después de la comprobación.
- El entrenamiento LoRA necesita la GPU para sí; la app descarga sus modelos automáticamente.
- Cambiar entre tipos de voz puede necesitar brevemente memoria extra mientras carga un modelo.

### El segundo lote se cuelga o falla

Es fragmentación de la memoria de GPU. La app limpia la memoria entre sublotes para evitarlo. Si
aun así ocurre, reduce el tamaño de lote o reinicia la app entre ejecuciones grandes.

### Avisos de MIOpen (AMD)

Los avisos de espacio de trabajo de MIOpen en GPU AMD se manejan automáticamente y no afectan a la calidad ni a la
velocidad.

## Salida de audio

### Archivos MP3 rotos o diminutos (428 bytes)

Falta ffmpeg o no puede codificar MP3. El instalador compila ffmpeg en el entorno de la app,
así que normalmente significa que la app se está ejecutando fuera de `app/env`: iníciala desde Pinokio, o activa
`app/env`. Cada archivo generado se comprueba, y el Editor marca un fragmento roto.

Para comprobar un ffmpeg a mano:

```bash
ffmpeg -encoders 2>/dev/null | grep mp3
```

### Calidad del audio

- Usa referencias limpias de 5-15 segundos para clonar.
- Prueba otra semilla para las voces Custom (o déjala vacía para aleatoria).
- Revisa las instrucciones: las contradictorias, o las que describen el cuerpo en lugar de la
  voz, bajan la calidad. Las palabras que describen la voz en sí ("deep", "raspy") luchan contra la voz
  elegida; el ancla de identidad las elimina.
- Para voces LoRA, asegúrate de que los datos de entrenamiento tenían variedad emocional.

### Caracteres corruptos en el texto

- La reparación de subidas corrige problemas de codificación habituales.
- Si un libro sigue mostrando `â€™`, ejecuta la vista previa de reparación desde Saved Scripts.
- Asegúrate de que los archivos de entrada son UTF-8.
- El kana pictográfico y otros caracteres impronunciables se eliminan antes del TTS a propósito.

## Entrenamiento

### La loss se queda alta (por encima de 10)

- Comprueba que los archivos WAV son válidos, no están corruptos.
- Comprueba que `metadata.jsonl` tiene la transcripción correcta para cada clip.
- Comprueba que el audio de referencia existe y está limpio.
- **No subas la tasa de aprendizaje** para forzar la loss a bajar: a 5e-6 las voces nunca dejan de hablar.

### La voz suena plana tras el entrenamiento

- A los datos de entrenamiento les falta variedad emocional: la causa más común.
- Reconstruye el conjunto de datos con pares de emoción y texto variados (alegre, enfadado, triste, susurrado,
  gritado).
- Prueba un rank LoRA más bajo (8-16) para conservar más expresividad del modelo base.

### El entrenamiento falla o se cuelga

- El entrenamiento necesita la GPU para sí; la app debería descargar sus modelos primero.
- Lee el primer error de CUDA o ROCm en el log.
- Prueba un tamaño de lote más pequeño o menos pasos de acumulación de gradiente.

## Windows

### Errores de bloqueo de archivos

Windows puede bloquear archivos JSON durante lecturas y escrituras en paralelo. La app escribe los archivos de forma atómica
y reintenta con backoff. Si los errores continúan:

- baja **Parallel Workers**;
- cierra editores o exploradores de archivos que tengan abiertos archivos del proyecto.

## Comprobar tu instalación

```bash
cd app
python run_isolated_api_tests.py          # quick — model-free API checks in a throwaway copy
python run_isolated_api_tests.py --full   # also generation — needs a GPU and an LLM
```

Arranca su propia copia de la app con una carpeta de datos vacía, así que tus libros y voces no se
tocan. Pasar el modo rápido verifica solo las rutas sin modelo que ejercita; la generación
puede seguir fallando por el código de la app, la carga de modelos, los recursos de GPU o la integración con proveedores.
Inspecciona los logs de la tarea que falla. Ver [Home](Home-ES.md#comprobar-una-instalación) para probar una
app que ya está en ejecución.
