# Generación por lotes

[English](Batch-Generation.md) · [日本語](Batch-Generation-JA.md) · [中文](Batch-Generation-CN.md) · Español

Cómo renderiza la app muchas líneas a la vez y cómo ajustarlo.

## Qué ruta toma tu renderizado

| Modo TTS (Setup) | qué hacen **Render Pending** / **Regenerate All** |
|---|---|
| `local` (el motor integrado) | renderizado **por lotes**: muchas líneas por llamada a Qwen3-TTS, agrupadas por tipo de voz |
| `external` (un servidor Gradio o un pool de servidores) | una llamada por línea, enviadas en paralelo |

El renderizado por lotes es la ruta rápida: unas 3-6× el tiempo real en una tarjeta de gama media. Usa una
semilla por llamada nativa al modelo. Con **Batch Seed** vacío o `-1`, cada llamada respeta la semilla de voz
efectiva guardada/seleccionada por la versión; una semilla de voz negativa deja esa llamada sin semilla. Una
**Batch Seed** no negativa anula todas las semillas de voz guardadas y vuelve a sembrar cada sublote
existente, como antes. La ruta por línea también usa las semillas de voz guardadas y funciona con todos los
tipos de voz, pero es más lenta.

Las llamadas nativas de custom/clone/LoRA no pueden tomar una semilla distinta por fila. Después de ordenar por
longitud y aplicar los topes de elementos/memoria, los lotes se dividen más cuando cambian las semillas
efectivas. Las semillas homogéneas mantienen el procesamiento por lotes nativo; las semillas distintas intercaladas pueden requerir
una llamada por línea, reduciendo el rendimiento. Es deliberado en lugar de ignorar en silencio las
semillas guardadas. Una anulación de lote compartida no negativa mantiene las particiones de lote originales pero
cambia qué semilla se usa. Poner semilla a una llamada de varias filas no promete la misma forma de onda
que renderizar cada línea individualmente, ni una identidad de voz audible a lo largo de un libro.

## El orden en que se renderizan los tipos de voz

1. **Voces Custom** — por lotes de forma nativa (lo más rápido).
2. **Voces Clone** — por lotes por hablante.
3. **Voces LoRA** — por lotes por adaptador.
4. **Voice Design** — una línea a la vez, porque cada línea puede tener una descripción distinta.

Los tipos con más líneas suelen ir primero, así que la mayor parte del libro termina pronto.

## Cómo se agrupa cada tipo

- **Custom** — se recogen todas las líneas Custom, se ordenan por longitud y se dividen
  en sublotes. Cada sublote es una llamada con una lista de textos.
- **Clone** — las líneas se agrupan por **hablante**, ya que cada hablante tiene su propio audio de referencia.
  El prompt de voz se construye una vez por hablante y se guarda en caché.
- **LoRA** — las líneas se agrupan por **adaptador**, ya que cada adaptador tiene sus propios pesos. La instrucción
  y el estilo de personaje de cada línea viajan con ella.
- **Voice Design** — se renderiza una a una después de que terminen todos los tipos por lotes.

## Sublotes

El modelo de voz sigue generando hasta que termina el texto más largo de un lote, así que los textos más cortos
del mismo lote desperdician tiempo de GPU en relleno. El subloteo agrupa textos de longitud similar para
reducir ese desperdicio.

Cómo funciona:

1. Ordena las líneas por longitud de texto, de la más corta a la más larga.
2. Recórrelas, recogiendo un grupo.
3. Empieza un grupo nuevo cuando el texto más largo sea más de *ratio* × el más corto **y** el
   grupo ya tenga al menos *min size* líneas.
4. Refina cada grupo en los cambios de semilla efectiva; cada grupo resultante se convierte en su propia llamada.

Desactivar la división por ratio de longitud sigue respetando los topes de memoria/manuales de elementos y los límites de semilla.

| ajuste (Setup) | por defecto | significado |
|---|---|---|
| Sub-batching | activado | divide los lotes por longitud |
| Min Sub-batch Size | 4 | mínimo antes de una división por ratio de longitud; los topes y las semillas pueden dividir grupos más pequeños |
| Length Ratio | 5 | el ratio máximo permitido entre el más largo y el más corto |

**Ejemplo:** con ratio 5, textos de 10, 15, 20, 50, 55 y 200 caracteres se dividen en [10, 15,
20], [50, 55] y [200], porque 200 es más de 5 × 10.

## Ajuste

**Haz clic primero en Auto-Configure.** Lee la memoria de tu GPU y fija los ajustes de lote a partir de
mediciones en una RX 9070 XT (el modelo por sí solo ocupa unos 4.2 GB; los lotes de 4, 8 y 16 líneas
llegan a unos 7.3, 9.6 y 11.8 GB).

| ajuste | qué hace |
|---|---|
| TTS Mode | `local` es necesario para el renderizado por lotes |
| Parallel Workers | el tamaño de lote para el renderizado local; Auto-Configure fija 1-4 según la memoria de la GPU. Más es más rápido mientras quepa: en la RX 9070 XT, 1 / 4 / 16 workers renderizaron a 1.2 / 2.2 / 3.9× el tiempo real (2026-09-28) |
| Max Items/Batch | un tope de líneas por lote |
| Compile Codec | decodificación 3-4× más rápida tras un calentamiento único de 30-60 s |
| Sub-batching | activado: reduce el desperdicio por relleno |

Alexandria upstream midió recuentos de workers mayores (20-60) en una RX 7900 XTX de 24 GB. Esos
números no se volvieron a medir en este fork; empieza por Auto-Configure y sube el valor solo
si la memoria lo permite.

### Benchmarks de upstream (RX 7900 XTX, 24 GB, ROCm 6.3)

| configuración | velocidad |
|---|---|
| una línea a la vez | unas 1× el tiempo real |
| por lotes, sin compilación de códec | unas 2× el tiempo real |
| por lotes con compilación de códec | **3-6× el tiempo real** |
| LoRA, una línea a la vez | unas 0.5× el tiempo real |
| LoRA, por lotes | **unas 5.5× el tiempo real** |

Un audiolibro de 273 líneas (unos 54 minutos de audio) se renderizó en unos 16 minutos con lotes y
compilación de códec.

## Memoria de GPU

- Entre sublotes la app ejecuta `gc.collect()` y `torch.cuda.empty_cache()` para evitar
  la fragmentación de memoria.
- Antes de que empiece un lote, una comprobación de margen de memoria rechaza un lote que no cabría.
- Los tipos de voz se renderizan por separado, así que la memoria máxima es la del grupo más grande,
  no la suma.
- Si te quedas sin memoria, baja **Parallel Workers** y **Max Items/Batch**.
- Un modelo de lenguaje en la misma tarjeta quita memoria al TTS. Si el tuyo está alojado en otro sitio,
  abre **Provider request options** y pon **Runs on this machine's GPU?** en **No** en Setup.

## Compilación del códec

Con **Compile Codec** activado, `torch.compile` optimiza el decodificador del códec la primera vez que se
usa. Eso añade 30-60 segundos una vez, y luego la decodificación es 3-4× más rápida durante el resto de la sesión.
No recompila en cada lote: solo tras reiniciar la app.

## Notas sobre AMD (ROCm)

La app aplica las correcciones específicas de ROCm automáticamente; no necesitas configurar nada:

- **Modo fast-find de MIOpen**: evita fallos de asignación de espacio de trabajo.
- **Flash attention AMD de Triton**: habilita flash attention para el codificador de Whisper.
- **Un shim de compatibilidad `triton_key`**: corrige `torch.compile` en pytorch-triton-rocm.
- **APUs** (clase 660M/680M/780M) ejecutan el TTS en fp32, porque bf16 está roto ahí.
