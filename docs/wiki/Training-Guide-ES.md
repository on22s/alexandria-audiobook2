# Guía de entrenamiento

[English](Training-Guide.md) · [日本語](Training-Guide-JA.md) · [中文](Training-Guide-CN.md) · Español

Entrena un adaptador LoRA sobre el modelo Base de Qwen3-TTS para crear tu propia voz. Una voz entrenada mantiene
una identidad consistente y sigue las instrucciones de interpretación para la emoción.

> **Empieza por la receta medida de abajo, no por tablas antiguas.** Una tasa de aprendizaje de **5e-6
> hace que las voces se desboquen**: en 2 de 2 pruebas la voz nunca dejó de hablar, mientras que 3 de 3 con
> 1e-6 se detuvieron correctamente. La loss de entrenamiento no lo muestra: solo lo hace el audio
> generado. Evidencia: [RECIPES.md](../../RECIPES.md) §"Voice adapters".

## La receta que funciona

| uso | ajustes | evidencia |
|---|---|---|
| una voz de biblioteca (un narrador) | 6 epochs, tasa de aprendizaje **1e-6**, rank 64, alpha 128, 180-200 clips | 67 voces publicadas se entrenaron así y pasan la comprobación de parada |
| una voz de evaluación | 6 epochs, tasa de aprendizaje **1e-6**, rank 32, alpha 128, 200 clips, seed 1234 | los adaptadores detrás de los resultados publicados de voz en inglés |

Los valores por defecto de la pestaña Training están cerca de esto: 5 epochs, tasa de aprendizaje 1e-6, tamaño de lote 1,
rank 32, alpha 128, acumulación de gradiente 8.

## Resumen

La pestaña Training incluye voces LoRA integradas, así que puedes usar voces LoRA sin entrenar
nada. Para crear la tuya:

1. **Crea un conjunto de datos**: muestras de voz (WAV) con transcripciones.
2. **Configura el entrenamiento**: define los parámetros.
3. **Entrena**: se ejecuta como un proceso separado. Necesita la GPU para sí, así que la app descarga primero sus
   modelos TTS.
4. **Prueba**: escucha la voz entrenada.
5. **Asigna**: usa el adaptador en la pestaña Voices.

## Crear un conjunto de datos

Hay dos formas, más el Voice Lab para bibliotecas completas.

### Opción 1: Dataset Builder (voces sintéticas)

Genera las muestras a partir de una descripción de texto con el modelo VoiceDesign, previsualizando cada una.
Lo mejor para voces que no existen como grabaciones reales. Haz clic en **Build New Dataset** en la
pestaña Training, o abre la pestaña Dataset. Ver [Dataset Builder](Dataset-Builder-ES.md).

Un buen conjunto de datos sintético:

- tiene **6-25 muestras**;
- tiene **variedad emocional real**: los datos solo neutros producen voces planas que se resisten a las instrucciones
  de emoción;
- mantiene una **descripción raíz** para la identidad de la voz, y usa la emoción de cada fila para
  la interpretación (las dos se combinan en el prompt de VoiceDesign);
- incluye **expresiones cortas** ("Oh!", "Hmm.", "Right.") para que la voz aprenda a parar en líneas
  cortas;
- termina con un **pasaje largo, tranquilo y neutro**, que es una muestra de referencia ideal;
- usa una **semilla fija para la muestra de referencia**, para que el embedding de hablante se mantenga estable cuando
  regeneres.

Filas de ejemplo para la voz de un soldado:

| emoción | texto |
|---|---|
| *(vacío: neutro)* | The patrol route has been secured and all positions are accounted for. |
| Barking orders sharply | Move out! I want that perimeter locked down in sixty seconds! |
| Quiet, tense warning | Keep your voice down. There's movement in the treeline, fifty meters out. |
| Exhausted, catching breath | We've been marching for twelve hours. The men need rest or we'll lose them. |
| Bitter, restrained anger | They sent us in without support. Command knew exactly what they were doing. |
| Gentle, reassuring | Easy now, son. You did good out there. We're going to get you home. |

### Opción 2: subir audio real (ZIP)

Sube un ZIP como este:

```
dataset.zip
├── sample_001.wav
├── sample_002.wav
├── ...
├── ref.wav            # the reference audio for the speaker embedding
└── metadata.jsonl     # one JSON object per line
```

`metadata.jsonl`:

```json
{"audio_filepath": "sample_001.wav", "text": "The transcript of this sample.", "ref_audio": "ref.wav"}
{"audio_filepath": "sample_002.wav", "text": "Another transcript.", "ref_audio": "ref.wav"}
```

**Requisitos del audio**

- WAV, idealmente mono de 24 kHz (otras frecuencias de muestreo se remuestrean automáticamente).
- Audio limpio sin ruido de fondo.
- **Un hablante por conjunto de datos.**
- Los clips de más de 30 segundos se omiten.

**Audio de referencia**

- Usa el mismo `ref_audio` para cada muestra; la propia documentación de Qwen3-TTS lo recomienda
  encarecidamente.
- Aporta el embedding de hablante, así que una referencia consistente hace la voz más estable.
- Si una línea no tiene `ref_audio`, el entrenador usa `ref.wav` en la carpeta del conjunto de datos, y luego la
  primera muestra de entrenamiento.

**Sin campo emotion.** Las grabaciones reales ya llevan su emoción, así que `metadata.jsonl` no tiene
campo `emotion`: en el Dataset Builder la emoción es solo un prompt para *generar* audio.
Tus grabaciones siguen necesitando **variedad emocional**, porque la voz aprende su rango del
audio en sí (ver [Rango emocional](#rango-emocional)).

### Opción 3: Voice Lab (una biblioteca completa)

La pestaña Voice Lab convierte audiolibros en voces LoRA con nombre en bloque: el Preparer alinea un
audiolibro con su texto, y luego siguen la deduplicación, el entrenamiento por lotes, el perfilado y el nombrado. Ver
la sección [Interfaz web](../../README_ES.md#interfaz-web) del README.

### Cómo es un conjunto de datos en disco

```
lora_datasets/{name}/
├── metadata.jsonl      # {audio_filepath, text} per line
├── ref.wav             # reference audio for the speaker embedding
├── ref_text.txt        # the exact transcript of ref.wav
└── sample_000.wav ...  # the training audio
```

## Ajustes de entrenamiento

| ajuste | valor por defecto de la pestaña Training | efecto |
|---|---|---|
| **Epochs** | 5 | pasadas completas por el conjunto de datos; más fija la voz con más fuerza pero arriesga sobreajuste |
| **Learning rate** | 1e-6 | cuánto se mueven los pesos por paso; **no uses 5e-6** (ver el aviso de arriba) |
| **LoRA rank** | 32 | la capacidad del adaptador (ver abajo) |
| **LoRA alpha** | 128 | un factor de escala; la fuerza efectiva es alpha ÷ rank (128/32 = 4×, 128/64 = 2×) |
| **Batch size** | 1 | muestras por paso; mantenlo en 1 |
| **Gradient accumulation** | 8 | simula un lote mayor sin más memoria |
| **Language** | English | el token de idioma del códec: **debe coincidir con el idioma del conjunto de datos** |

### Elegir el rank

| rank | tamaño del adaptador | mejor para |
|---|---|---|
| **32** | unos 56 MB | hasta unas 60 muestras: aprende la voz sin memorizar ruido |
| **64** | unos 111 MB | 120+ muestras; las voces de biblioteca publicadas (180-200 clips) usan 64 |

Con unas 60 muestras, los mejores adaptadores de upstream usaban todos rank 32; el rank 64 con los mismos datos produjo
archivos más grandes que sonaban peor, porque la capacidad extra memorizaba artefactos del entrenamiento.
**Regla general:** rank 32 a menos que tengas 100+ muestras. Si la loss parece buena pero la voz
suena mal, baja el rank antes de cambiar cualquier otra cosa.

## Leer la loss de entrenamiento

Estas bandas de loss vienen de las pruebas de Alexandria upstream con conjuntos de datos pequeños. Úsalas solo como guía
aproximada: **una loss buena no demuestra una voz buena.** El desboque a 5e-6 es invisible en
la loss; solo escuchando, o con la comprobación de parada, se detecta.

| loss | audio | sigue instrucciones | veredicto |
|---|---|---|---|
| 4.4 y más | claro | débilmente | poco entrenada |
| 4.1-4.2 | claro, expresivo | bien | el punto óptimo |
| 3.9-4.1 | expresivo, puede distorsionar | fuertemente | al límite: las ejecuciones varían |
| 3.4-3.8 | distorsionado pero comprensible | fuertemente | sobreajuste |
| 3.0-3.3 | distorsionado, o nunca para | — | sobreajustada, inutilizable |

- **Más datos necesitan menos epochs**: cada epoch enseña más.
- **La exposición total** (muestras × epochs) de unas 250-400 funcionó en upstream; por encima de 600 arriesgaba
  sobreajuste.
- Ajustes idénticos pueden caer a un lado u otro de 4.1 de una ejecución a otra.

## Entrenar

1. Haz clic en **Start Training** en la pestaña Training.
2. La app descarga sus modelos TTS para liberar la GPU.
3. El entrenamiento se ejecuta como un proceso separado con un log en vivo: epoch, loss, tasa de aprendizaje y tiempo
   restante.
4. Se conserva el mejor checkpoint (la loss más baja).
5. Los modelos TTS se vuelven a cargar en el siguiente renderizado.

Un conjunto de datos pequeño se entrena en minutos. Como referencia, volver a entrenar una voz de biblioteca (200 clips)
tarda unos 5.4 minutos en una RX 9070 XT.

### Cómo funciona el entrenamiento

El entrenador sigue el método oficial de fine-tuning de Qwen3-TTS:

- **Ponderación de subloses:** la loss del code-predictor se pondera a 0.3×, como en el
  `sft_12hz.py` oficial.
- **Embedding de hablante:** se toma de un clip de referencia consistente, no de cada muestra.
- **Sin instrucciones durante el entrenamiento:** la voz se aprende solo del audio y el texto;
  las instrucciones se usan al generar.
- **Objetivos LoRA:** las capas de atención del talker (`q_proj`, `k_proj`, `v_proj`, `o_proj`),
  mediante PEFT.

## Probar y promocionar una voz

1. El adaptador aparece en **Trained Models** en la pestaña Training.
2. Haz clic en **Generate** para probarlo con tu propio texto e instrucción.
3. Las vistas de comparación y **blind review** te dejan escuchar frente a la voz actual sin
   saber cuál es cuál. Promocionar conserva la versión anterior, con un recibo, así que se puede revertir.
4. Si estás satisfecho, asigna el adaptador a un personaje en la pestaña Voices.

La **comprobación de parada** (puerta de identidad) se ejecuta antes de la promoción y detecta una voz que nunca deja
de hablar.

## Consejos para mejores voces

### Rango emocional

El factor más importante para una voz expresiva es la **variedad emocional en los datos de
entrenamiento**. Incluye:

- narración neutra;
- alegre o emocionada;
- enfadada o frustrada;
- triste o sombría;
- susurrada o tensa;
- gritando o dando órdenes.

Los datos solo neutros producen una voz que suena plana y se resiste a las instrucciones emocionales.

### La instrucción de la muestra de referencia

Al generar datos de entrenamiento, **no** le des a la muestra de referencia la instrucción que usarás
después (por ejemplo "Neutral, even narration."). Las voces entrenadas así tendían a acelerarse y
subir el volumen en frases más largas que el clip de referencia. Generar la referencia con una
**instrucción vacía** dio un ritmo más natural.

### Mezcla las longitudes de clip

Entrenar solo con clips cortos de una frase (4-8 s) causa cambios de tono entre frases,
porque la voz nunca aprende a pasar de una frase a la siguiente. Incluye un rango:

| longitud | contenido | enseña | cuántos |
|---|---|---|---|
| 1-3 s | exclamaciones cortas ("Oh!", "Right.", "No!") | a parar en líneas cortas | 3-5 |
| 4-8 s | frases sueltas, emociones variadas | la voz central y su rango | la mayor parte del conjunto |
| 15-20 s | 2-3 frases | transiciones, ritmo, fluidez | 5-8 |
| 20-30 s | 4-5 frases de narración | interpretación de formato largo | 2-3 |

### Cuántos datos

- **Mínimo:** 6 muestras con emociones variadas.
- **Bien:** 15-20 muestras que cubran un rango de emociones y estilos.
- **Calidad de biblioteca:** las voces publicadas usan 180-200 clips.
- Upstream encontró rendimientos decrecientes más allá de 25-30 muestras sintéticas, y sugería 15-30
  minutos de audio para una voz de primera calidad.

### ¿Reales o sintéticos?

- **Sintético (Dataset Builder):** más control sobre la emoción, calidad consistente, rápido de
  hacer. Bueno para personajes inventados.
- **Grabaciones reales:** más naturales, capturan cualidades sutiles de una voz real. Mejor cuando
  tienes grabaciones limpias de un solo hablante.
- Ambos funcionan. Los datos sintéticos con variedad emocional tienden a ser más expresivos que las grabaciones
  reales neutras.

## Otros idiomas

Un adaptador entrenado con un idioma lleva el acento de ese idioma. Un adaptador entrenado en inglés
lee texto alemán con pronunciación inglesa: a veces aceptable, a menudo no.

**Entrena un adaptador separado por idioma para cada hablante**, y ajusta el **Language** de la pestaña Training
al idioma del conjunto de datos (English, Chinese, Korean, Japanese, French, German,
Italian, Portuguese, Russian o Spanish). Un desajuste puede hacer que el adaptador pierda la identidad
del hablante.

## Solución de problemas

| problema | causa probable | corrección |
|---|---|---|
| la loss se queda por encima de 10 | audio inválido o transcripciones equivocadas | comprueba los archivos WAV y las transcripciones de `metadata.jsonl` |
| la voz nunca deja de hablar | tasa de aprendizaje demasiado alta, o sobreajuste | reentrena a 1e-6; usa menos epochs |
| audio distorsionado con texto nuevo | sobreajuste (loss demasiado baja) | menos epochs |
| audio claro pero sin identidad | poco entrenada (loss demasiado alta) | más epochs |
| voz plana, monótona | sin variedad emocional en los datos | añade pares de emoción y texto variados; prueba un rank más bajo (8-16) |
| no suena como las muestras | poco entrenada, o rank demasiado bajo | más epochs, un rank más alto, un `ref_audio` consistente |
| habla apresurada | la muestra de referencia se hizo con una instrucción | regenérala con una instrucción vacía |
| las líneas cortas llegan a `max_new_tokens` | la voz nunca aprendió a parar en líneas cortas | añade vocalizaciones cortas a los datos |
| un pequeño glitch al principio | alineación del prompt de clonación | menor; normalmente no aparece en un audiolibro completo |
| referencia no coincide | `ref_text.txt` no coincide con `ref.wav` | haz la transcripción exacta |
| acento equivocado en otro idioma | desajuste del ajuste de idioma | pon **Language** para que coincida con los datos |

**No** subas la tasa de aprendizaje para arreglar una loss alta: comprueba primero los datos.

## Configuraciones probadas por upstream

Estas ejecuciones las hizo Alexandria upstream con pequeños conjuntos de datos sintéticos. Se conservan por
sus observaciones de loss frente a calidad. **Las filas con 5e-6 y más son anteriores al hallazgo del
desboque** y no se recomiendan.

| adaptador | muestras | epochs | tasa de aprendizaje | alpha | loss | resultado |
|---|---|---|---|---|---|---|
| female-lora-01 | 33 | 3 | 1e-5 | 128 | 3.93 | funciona, ritmo algo rápido |
| female-lora-02 | 121 | 15 | 3e-6 | 128 | 3.03 | sobreajustada, distorsionada |
| female-lora-03 | 121 | 5 | 5e-6 | 128 | 3.10 | sobreajustada, nunca para |
| female-lora-04 | 121 | 2 | 5e-6 | 128 | 3.86 | comprensible, distorsiona, tonos raros |
| female-lora-05 | 121 | 1 | 5e-6 | 128 | 4.43 | clara, poco seguimiento de instrucciones |
| female-lora-06 | 121 | 3 | 2e-6 | 64 | 3.46 | distorsionada pero comprensible |
| **female-lora-07** | **121** | **3** | **2e-6** | **128** | **4.11** | **la mejor: audio claro, sigue instrucciones** |
| male-lora-01 | 61 | 5 | 1e-6 | 128 | 4.44 | clara pero plana, apenas sigue instrucciones |
| male-lora-02 | 61 | 7 | 1e-6 | 128 | 4.31 | emotiva, responde |
| **male-lora-03** | **61** | **10** | **1e-6** | **128** | **4.11** | **la mejor: expresiva, rica, sigue instrucciones** |
| male-lora-04 | 61 | 10 | 1e-6 | 128 | 4.12 | mismos ajustes que 03; algunas líneas distorsionadas (variación entre ejecuciones) |
| male-lora-05 | 61 | 9 | 1e-6 | 128 | 4.17 | limpia, expresiva, margen seguro |
| male-lora-06 | 61 | 12 | 1e-6 | 128 | 3.99 | muy expresiva pero la mitad de las líneas distorsionadas |
| male-lora-07 | 61 | 14 | 1e-6 | 128 | 3.89 | comprensible pero sobreajustada |
