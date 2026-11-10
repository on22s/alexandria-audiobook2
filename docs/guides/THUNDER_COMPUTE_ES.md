[English](THUNDER_COMPUTE.md) | [中文](THUNDER_COMPUTE_CN.md) | [日本語](THUNDER_COMPUTE_JA.md) | [Español](THUNDER_COMPUTE_ES.md)

# Cuándo usar Thunder Compute (y para qué)

Una guía de decisión para este proyecto. **Lo normal es local**: Thunder es la
excepción, no la base.

Tu hardware: **AMD Radeon RX 9070 XT** (16 GB, RDNA4/ROCm) ejecutando LM Studio
en `localhost:1234`. La app usa `llm_mode: local` por defecto; también puede estar configurado
un perfil remoto de Thunder. Comprueba siempre `tnr status` antes de suponer
que no se está facturando nada.

---

## 1. La regla de una línea

> Usa Thunder cuando el trabajo **no pueda ejecutarse localmente en absoluto**, o cuando fuera a tardar
> tanto en local que el tiempo de reloj importe más que los dólares.
> Todo lo demás: quédate en local.

A **$0.35/h por una A6000** (precio comprobado el 2026-07-18), el coste rara vez es el factor decisivo para unas pocas
horas de trabajo. Los factores decisivos son la **VRAM**, **CUDA** y el **tiempo de reloj**.

---

## 2. Usa Thunder para

| Motivo | Por qué falla lo local | GPU sugerida |
|---|---|---|
| **El modelo necesita > 16 GB de VRAM** | La 9070 XT te limita a ~16 GB. Los modelos más grandes o el contexto largo no cargan. | A6000 (48 GB), $0.35/h |
| **Código solo para CUDA** | Tu tarjeta es ROCm. Cualquier cosa que asuma kernels CUDA (algunas compilaciones de `xformers`/`triton`/`sageattention`/`flash-attn`, la mayoría de los wheels ML de upstream) no se ejecutará. | A6000 / L40 |
| **Necesitas recuperar tu escritorio** | El trabajo local con LLM ocupa la GPU; el bloqueo global de GPU bloquea cualquier otra tarea de la app. | A6000 |
| **Generación de personas con el endpoint Gemma medido** | La A100 completó la cohorte de descubrimiento/compilación de producción 2,27× más rápido. | A100 80 GB |
| **Una carga de trabajo que realmente se quedó sin memoria (OOM) en local** | La A100 medida fue más lenta para todas las etapas de TTS y Voice Lab probadas, incluido el entrenamiento LoRA. Úsala solo cuando el bloqueo sea la capacidad, no una velocidad supuesta. | Dimensiona para la necesidad de VRAM observada |

## 3. NO uses Thunder para

- **Trabajo solo de CPU.** Audacity, M4B y el nombrado de Voice Lab se midieron en ambas
  máquinas; lo local fue más rápido en los tres casos.
- **Trabajos cortos o interactivos.** La sobrecarga de configuración (crear → SSH → cargar modelo →
  reenviar puerto) empequeñece unos minutos de inferencia.
- **Buscar paralelismo.** Ver §4: este es el punto importante.
- **Cualquier cosa que no hayas confirmado que está realmente limitada por la GPU.** Compruébalo primero; no
  alquiles una GPU para arreglar un problema de E/S o de latencia del LLM.

---

## 4. Thunder compra VRAM y CUDA: la velocidad hay que medirla

Esto está medido, no es teórico, y es lo más contraintuitivo
de todo:

> En una ejecución histórica de Thunder con **A6000** cuyo informe en bruto no está versionado,
> la revisión con `concurrency`/`parallel` **> 1 fue
> más lenta** que con 1, y **falló con 4**. Mantén la concurrencia en **1**. No
> escales `parallel` automáticamente.

En local, `lmstudio_settings.IDEAL_SETTINGS` fija deliberadamente `parallel: 1` por
seguridad de VRAM: es un compromiso intencionado (Rule 9), no un bug que haya que "arreglar"
alquilando una GPU. Así que ir a remoto **no** desbloquea solicitudes concurrentes. Si alquilas
una A6000 esperando 4× de rendimiento por paralelismo, pagarás 4× y obtendrás
menos de 1×.

Alquila Thunder por **margen de VRAM y CUDA**. No supongas que mejora ni la velocidad por token
ni el fan-out sin medir el modelo/runtime exactos.

El cuello de botella depende de toda la ruta de software, no solo de las especificaciones de la GPU.
La A100 perdió la calibración medida de entrenamiento LoRA pese a que el entrenamiento es
intensivo en cómputo. El benchmark no perfiló la causa; entre los posibles contribuyentes están
la configuración, los datos, el lote pequeño, los kernels y la sobrecarga del framework. No conviertas una ventaja teórica de cómputo
en una regla de colocación sin una medición con forma de producción.

### Confusiones conocidas en esta campaña

Se encontraron cuatro lagunas en el propio banco de pruebas de esta campaña a posteriori. Ninguna
significa que los números de abajo sean inventados, pero ninguna se controló
tampoco: trata cada conclusión de esta sección como provisional hasta que una
nueva ejecución las aborde. La generación y la revisión de guiones (ambos objetivos) se han
vuelto a ejecutar con el banco de pruebas corregido; consulta la nota de validación bajo cada
una de las dos primeras viñetas; las otras dos (muestreo de utilización,
rendimiento a escala) siguen aplicándose a todos los números de TTS/LoRA/VoiceLab de este
documento, que aún no se han vuelto a ejecutar.

- **Desajuste de compilación Torch/CUDA.** En local se ejecutó Torch 2.10.0+ROCm7.0; en Thunder
  una versión anterior, Torch 2.7.0+CUDA12.6. Cada comparación de TTS/LoRA/VoiceLab de abajo se
  midió entre dos pilas de software distintas, no solo entre dos GPU. El banco de pruebas del
  benchmark ahora huella la versión de Torch de ambos lados y
  (`benchmark_environment.verify_comparable_environments`) lanza un error ruidoso si
  divergen cuando ambas se recogen en un mismo preflight. **Validado
  el 2026-07-18:** el Torch de Thunder se actualizó in situ a 2.10.0+CUDA12.6
  (coincidiendo con la versión menor local; torchaudio/triton/deps de CUDA actualizados
  junto a él, transformers/peft/qwen_tts intactos y con reimportación limpia) y
  la comprobación de preflight pasó limpiamente para la nueva ejecución de generación/revisión de guiones.
  Las etapas de TTS/LoRA/VoiceLab aún no se han vuelto a ejecutar contra el Torch alineado.
- **No existía instrumentación de utilización para NVIDIA.** La nota "A100 ran at
  only about 15% utilization" bajo CustomVoice más abajo era una observación ad hoc y
  no repetida: `gpu_stats.py` tenía `rocm_smi_utilization` solo para AMD.
  `tts_benchmark.py` ahora muestrea `mean_gpu_utilization_pct` mediante el nuevo
  `gpu_stats.sample_gpu_utilization()` (NVIDIA o AMD, el que esté presente)
  durante cada llamada de generación, así que las ejecuciones futuras obtienen esto como métrica real
  en lugar de un aparte a ojo. Aún no se ha ejercitado en hardware TTS real: solo
  las etapas basadas en LLM se volvieron a ejecutar en esta pasada de validación.
- **Los benchmarks basados en LLM cronometraban el viaje de red, no solo la decodificación.**
  La generación/revisión de guiones, las personas y la detección de apodos llamaban antes al
  endpoint configurado de LM Studio directamente desde el orquestador local; para
  el objetivo "thunder" eso incorporaba un viaje de ida y vuelta por internet a través del
  túnel HTTPS reenviado por SSH en cada llamada de fixture×repetición, no
  solo una vez. Se ve más en los totales de menos de 2 segundos de la detección de apodos, donde
  un solo RTT puede duplicar el tiempo informado. Primera mitigación
  (2026-07-18): cada ejecutor de etapa LLM registra una sonda de referencia `network_rtt_seconds`
  por informe: medida de 0.083 s en local frente a 0.53-0.54 s en Thunder
  (~6.5×) para generación/revisión de guiones, lo que confirma que la confusión es real.
  Pero eso solo *mide* la confusión, así que llegó la corrección de verdad:
  `llm_benchmark_worker.py` ahora ejecuta todo el bucle **en** el host de Thunder
  por SSH (replicando el patrón de los workers de TTS/LoRA/VoiceLab: preparar, luego ejecutar
  en remoto, llamando a `localhost` desde allí en lugar de a la URL pública de reenvío),
  así que la red solo importa para el único viaje SSH que lanza
  un lote, no por llamada al LLM. Disponible para las 4 etapas LLM cuando
  los `settings.remote_root`/`remote_python` de un manifiesto están definidos, igual que las
  etapas de la familia TTS. Aún no se ha ejercitado en hardware real (no había ninguna instancia
  de Thunder activa cuando esto se publicó): solo probado con unit tests y SSH simulado.
- **Todos los benchmarks de TTS/LoRA/VoiceLab son de un solo flujo o de lote pequeño**
  (ejecuciones seriales de seis generaciones, topes de lote ≤16 abajo): la forma en la que una A100 es
  más débil y una GPU de consumo más fuerte. Nada aquí prueba el
  régimen de rendimiento a escala para el que se construyen las GPU de centro de datos. El
  `tts_vram_benchmark.py --sizes 32 64` existente (su flag `--sizes` ya acepta
  cualquier lista de topes; no hace falta cambiar código) aún no se ha ejecutado en Thunder con
  esos topes mayores; ese es el siguiente experimento recomendado antes de dar por cierto
  "local gana en TTS" en todos los tamaños de lote, no solo en ≤16.

**Convención de archivo:** solo 2 de las 13 etapas de esta campaña tenían todavía sus
archivos originales `reports/benchmarks/<preflight_id>.json` cuando se necesitaron otra vez hoy;
los otros 11 se habían limpiado después de transcribir sus números a este documento, así que
reconstruir sus fixtures exactos costó un esfuerzo real. En adelante, archiva el informe de una etapa en
[`../benchmarks/raw/`](../benchmarks/raw/) (versionado en git, ver su
`README.md`) antes de que se pueda eliminar la copia temporal, para que los números de cualquier etapa
se puedan reverificar o volver a ejecutar después sin rehacer la selección de fixtures
desde cero.

### Matriz de colocación medida — 2026-07-18

Estos son resultados de calibración a escala de fixture, no promesas para cada modelo o libro.
Sí responden a la pregunta de colocación para los entornos probados RX 9070 XT/ROCm y A100
80 GB/CUDA. El resumen canónico legible por máquina es
[`../benchmarks/thunder_2026-07-18.json`](../benchmarks/thunder_2026-07-18.json).
Las entradas pendientes siguen siendo explícitas en lugar de inferirse de un componente.

| Carga de trabajo | Local | A100 Thunder | Decisión medida |
|---|---:|---:|---|
| Generación de guion, 9 llamadas | 329.5 s, 7/9 aprobadas | 679.4 s, 7/9 aprobadas con contexto de 98k | Local por rendimiento; la calidad sigue siendo estocástica |
| Revisión de guion, 9 lotes | 238.3 s | 474.3 s | Local, unas 2.0× más rápido |
| Descubrimiento + compilación de personas | 39.885 s | 17.557 s | **Thunder, 2.27× más rápido** |
| Detección de apodos | 0.796 s | 1.782 s | Local, 2.24× más rápido |
| Rendimiento en caliente de CustomVoice | 1.18× tiempo real | 0.28× tiempo real | Local, unas 4.2× más rendimiento |
| Lote nativo de CustomVoice, tope 8 | 87.3 s | 210.6 s | Local, 2.41× más rápido |
| Clonación base, seis generaciones | 44.8 s | 228.3 s | Local, 5.09× más rápido |
| Lote nativo de clonación, tope 8 | 37.3 s | 107.8 s | Local, 2.89× más rápido |
| VoiceDesign, seis generaciones | 40.7 s | 173.9 s | Local, 4.27× más rápido |
| Lote de producción del Dataset Builder, 2 muestras | 16.095 s, 2/2 completas | Pendiente; no se capturó el resultado del reintento | Aún sin decisión entre máquinas |
| Generación en caliente de LoRA TTS | 5.48 s | 32.05 s | Local, 5.85× más rápido |
| Entrenamiento LoRA, 8 muestras / 1 epoch | 2.1 s | 6.5 s | Local, 3.10× más rápido |
| ASR del preparer de Voice Lab | 11.24 s | 21.72 s | Local, 1.93× más rápido |
| Dedup de Voice Lab | 10.19 s | 19.09 s | Local, 1.87× más rápido |
| Perfilado de Voice Lab, en frío | 6.936 s | 78.689 s | Local, 11.35× más rápido |
| Nombrado de Voice Lab | 0.0216 s | 0.0541 s | Local, 2.50× más rápido |
| Exportación de Audacity | 0.040 s | 0.063 s | Local, 1.58× más rápido |
| Exportación M4B | 0.167 s | 0.247 s | Local, 1.48× más rápido |

Las comprobaciones de calidad pasaron en ambas máquinas para las cohortes completadas: audio válido,
entradas verificadas por hash, puertas estructurales de guion/revisión, contenido canónico exacto de Voice Lab
donde corresponde, cobertura completa de personas/evidencia, mapeo exacto de apodos,
contenidos válidos de Audacity y salida M4B AAC/capítulos analizable. No se espera que los hashes
de audio y de contenedores comprimidos sean idénticos entre backends.

### Benchmark de generación de guiones en A100 — 2026-07-18

Esta es una cohorte controlada pequeña, no una clasificación universal de GPU. Se usó el mismo modelo
Gemma 4 E4B Q8, tres fragmentos históricos de ~6.000 caracteres, tres repeticiones
cada uno y `max_retries: 0` en ambas máquinas. Cero reintentos mide
la fiabilidad bruta de un solo intento; no es la tasa de finalización de producción.

| Entorno | Aprobadas | Tiempo total | Llamada correcta media | Tasa de finalización agregada |
|---|---:|---:|---:|---:|
| RX 9070 XT, contexto 32,768, paralelo 2 | 7/9 | 329.5 s | 43.9 s | 65.5 tok/s |
| A100 80 GB, contexto 98,304, paralelo 2 | 7/9 | 679.4 s | 94.9 s | 32.9 tok/s |
| A100 80 GB, contexto 32,768, paralelo 2 | 6/9 | 1,032.7 s | 105.3 s | 20.6 tok/s |

Los fallos fueron sobre todo paradas tempranas estocásticas o salida estructuralmente inválida;
las puertas de calidad rechazaron correctamente los guiones incompletos. Reducir el contexto de Thunder
**no** arregló su velocidad de un solo flujo, así que la asignación KV de 98k
no era el cuello de botella demostrado.

Un barrido de concurrencia con forma de producción y tres ensayos a contexto 32,768 encontró:

| Paralelo de LM Studio / solicitudes simultáneas | Tasa de finalización agregada mediana |
|---|---:|
| 1 / 1 | 43.5 tok/s |
| 2 / 2 | 17.9 tok/s |

El paralelo 2 fue un 59% más lento, así que la política de seguridad se detuvo antes de 4/8/16. Para
esta combinación exacta A100/Gemma/LM Studio, **el paralelo 1 es el
óptimo medido**. La recomendación práctica es mantener la generación de guiones con Gemma en
la RX 9070 XT local; usa esta A100 solo cuando la VRAM/CUDA o una carga de trabajo distinta medida
lo justifiquen. No extrapoles estos resultados de decodificación a TTS, entrenamiento
ni Voice Lab; sus resultados medidos por separado se recogen abajo.

El `REMOTE_IDEAL_SETTINGS` protegido actual sigue configurando `parallel: 2`,
y la autocorrección remota puede restaurar ese valor. El resultado medido de paralelo 1
es orientativo hasta que ese ajuste de producción sensible a la seguridad se cambie y valide
deliberadamente; esta guía no lo anula en silencio.

### Benchmark de revisión de guiones en A100 — 2026-07-18

Se midió la ruta de producción `review_batch` con tres lotes inmutables de 25 entradas
de los Volúmenes 3, 9 y 10, con tres repeticiones y cero reintentos.
Ambos entornos usaron el mismo modelo y contexto 32,768; Thunder usó su
óptimo medido de paralelo 1 mientras local conservó su configuración de paralelo 2.

| Entorno | Aprobadas | Tiempo total | Lote medio | Tasa de finalización agregada | Ratio medio de palabras |
|---|---:|---:|---:|---:|---:|
| RX 9070 XT local | 9/9 | 238.3 s | 26.5 s | 63.9 tok/s | 0.9993 |
| A100 80 GB Thunder | 9/9 | 474.3 s | 52.7 s | 32.0 tok/s | 0.9978 |

Ambos preservaron la estructura y se mantuvieron dentro de la puerta de producción de pérdida de texto del 95-105%.
Thunder no aportó ventaja de fiabilidad en esta cohorte y fue casi
exactamente 2× más lento. **Mantén la revisión de guiones con Gemma en local** para este modelo/runtime.
Esto refuerza el resultado de generación de guiones, pero sigue sin decir nada sobre
el rendimiento del entrenamiento LoRA.

### Benchmark de TTS CustomVoice en A100 — 2026-07-18

Se midió la ruta de producción `TTSEngine.generate_custom_voice` con los
mismos tres fixtures de narración corto/medio/largo, semillas fijas, tope de 1.024 tokens
generados y dos repeticiones por fixture. La carga del modelo y la generación se
cronometraron por separado. A ambos entornos les faltaba `flash-attn` y usaron la ruta manual
de atención de PyTorch; local usó Torch 2.10.0+ROCm 7.0 y Thunder usó Torch
2.7.0+CUDA 12.6. LM Studio permaneció residente en ambas máquinas.

| Entorno | Aprobadas | Carga del modelo en caché | Rendimiento de audio en caliente | Salidas recortadas |
|---|---:|---:|---:|---:|
| RX 9070 XT local | 6/6 | 6.2 s | 1.18 audio-sec/wall-sec | 0/6 |
| A100 80 GB Thunder | 6/6 | 21.8 s | 0.28 audio-sec/wall-sec | 0/6 |

La semilla fija produjo hashes idénticos entre repeticiones en cada máquina;
los hashes y duraciones entre backends difirieron ligeramente, lo cual es esperable con
kernels de Torch/GPU distintos. Todos los WAV eran PCM mono de 24 kHz válidos, no silenciosos
y sin recortes.

Para esta ruta serial exacta de CustomVoice, la RX 9070 XT local entregó aproximadamente
**4.2× más audio por segundo de reloj** y ya era más rápida que el tiempo real;
la A100 se ejecutó a solo un 15% de utilización durante la observación. La gran capacidad de VRAM de la A100
por tanto no se tradujo en velocidad de un solo flujo. Mantén la generación serial
de CustomVoice en local con esta pila de software. El procesamiento por lotes nativo de listas
se midió por separado más abajo. El entrenamiento LoRA sigue siendo una cohorte aparte, así que
ese resultado se midió por separado más abajo.

#### Capacidad de lote nativo de CustomVoice

Se barrió la ruta de producción `_local_batch_custom` con 16 fragmentos de fuente idénticos
de longitud mixta en cada máquina. Los topes por encima de 8 no ayudaron: con 16,
la división por ratio de longitud seguía produciendo dos sublotes y el relleno autorregresivo
reducía el rendimiento. El barrido inicial de capacidad encontró:

| Tope de lote | RX 9070 XT tiempo real | A100 tiempo real | Fallos de A100 |
|---:|---:|---:|---:|
| 2 | 1.71× | 0.47× | 0/16 |
| 4 | 3.12× | 0.86× | 0/16 |
| 8 | 4.93× | 1.32× | 0/16 |
| 16 | 4.03× | 0.89× | 0/16 |

Ese barrido expuso dos defectos de medición: la herramienta informaba solo el pico de VRAM del
último sublote y no fijaba la semilla del TTS. Ambos se corrigieron antes
de la ejecución de confirmación. Con `batch_seed=42` y tope 8, local completó las 16
entradas en **87.3s** con un pico de 9.64 GB; la A100 las completó en **210.6s** con
un pico de 10.12 GB. Las duraciones de salida difieren entre kernels ROCm/CUDA incluso con la
misma semilla, así que el tiempo de reloj con entradas iguales es la comparación principal: local fue
**2.41× más rápido**. El óptimo medido es el tope 8 en ambas máquinas; la capacidad de la A100
no permitió un escalado útil más allá de eso para esta carga de trabajo de longitud mixta.

#### Clonación de voz con el modelo base

La ruta de producción de clonación usó el mismo audio de referencia Watson verificado por hash
y la misma transcripción en ambas máquinas. Se ejecutaron tres textos objetivo corto/medio/largo
dos veces con semillas fijas y un tope de 1,024 tokens. La carga del modelo, la construcción
reutilizable del prompt de clonación y la generación se cronometraron por separado.

| Entorno | Aprobadas | Carga del base | Construcción del prompt | Seis generaciones | Rendimiento de audio agregado |
|---|---:|---:|---:|---:|---:|
| RX 9070 XT local | 6/6 | 6.44 s | 0.99 s | 44.8 s | 1.15× tiempo real |
| A100 80 GB Thunder | 6/6 | 30.14 s | 7.20 s | 228.3 s | 0.25× tiempo real |

Todas las salidas eran PCM mono de 24 kHz válidos, deterministas dentro de cada backend,
no silenciosas y sin recortes. Para las mismas entradas objetivo, la generación local fue
**5.09× más rápida**. Mantén la clonación serial con el modelo base en local con estas pilas
Torch/Qwen.

Luego se barrió la ruta de lote nativo de clonación con las mismas 16 entradas deterministas
de longitud mixta usadas para CustomVoice:

| Tope de lote | RX 9070 XT tiempo / tiempo real / pico | A100 tiempo / tiempo real / pico |
|---:|---:|---:|
| 2 | 80.5 s / 1.91× / 6.13 GB | 322.3 s / 0.47× / 5.72 GB |
| 4 | 45.5 s / 3.45× / 7.17 GB | 183.7 s / 0.84× / 7.21 GB |
| 8 | **37.3 s / 4.27× / 9.25 GB** | **107.8 s / 1.43× / 10.17 GB** |
| 16 | 40.1 s / 3.88× / 11.84 GB | 153.5 s / 1.02× / 13.87 GB |

Las 128 salidas de ambas máquinas y los cuatro topes se completaron correctamente.
El tope 8 es el óptimo medido en ambas GPU. En ese óptimo compartido, local fue
**2.89× más rápido por tiempo de reloj con entradas iguales**. El tope 16 empeoró porque los últimos
tres fragmentos largos formaron un segundo sublote más lento; la A100 aún tenía aproximadamente
67 GB sin usar, así que la capacidad extra no era la respuesta.

#### Vistas previas de VoiceDesign

Se midió la llamada de producción `generate_voice_design` con tres descripciones de voz
distintas y textos objetivo corto/medio/largo, dos repeticiones con semilla fija
cada uno y un tope de 1,024 tokens. El benchmark incluye la creación real del WAV de vista previa
y mueve ese archivo al directorio de salida del benchmark.

| Entorno | Aprobadas | Carga del modelo en caché | Seis generaciones | Rendimiento de audio agregado |
|---|---:|---:|---:|---:|
| RX 9070 XT local | 6/6 | 6.07 s | 40.7 s | 1.15× tiempo real |
| A100 80 GB Thunder | 6/6 | 24.21 s | 173.9 s | 0.27× tiempo real |

La generación local de VoiceDesign fue **4.27× más rápida por tiempo de reloj con entradas iguales**.
Todas las salidas eran válidas, no silenciosas y sin recortes. Las repeticiones locales fueron
idénticas bit a bit. Una repetición media de la A100 difirió en valores minúsculos a nivel de muestra
pese a la semilla fija, mientras que su duración y métricas de salud fueron efectivamente
idénticas; no trates la salida de VoiceDesign en CUDA como garantía de determinismo bit a bit.
Mantén las vistas previas de VoiceDesign y la generación de fragmentos de voces diseñadas en local con estas
pilas de software.

#### Inferencia LoRA TTS

La ruta de producción LoRA usó el mismo adaptador Watson verificado por hash, semilla y
prompt en ambas máquinas. Midió por separado la carga de modelo/adaptador, la construcción
reutilizable del prompt y la generación en caliente.

| Fase | RX 9070 XT | A100 Thunder |
|---|---:|---:|
| Carga del modelo + adaptador | 4.54 s | 23.43 s |
| Construcción del prompt | 0.86 s | 7.02 s |
| Generación en caliente | 5.48 s | 32.05 s |

La generación en caliente local fue **5.85× más rápida**. Ambos entornos produjeron salidas
sanas y deterministas en todas las repeticiones. Mantén la inferencia LoRA en local con esta
pila de adaptador/modelo.

### Utilidades LLM de personas y apodos

El benchmark avanzado de personas capturó intencionadamente el límite de la vista previa
en lugar de generar TTS otra vez. Cada objetivo hizo una llamada de descubrimiento y
dos llamadas de compilación sobre el mismo fixture de dos hablantes. Thunder terminó en
17.557 s frente a 39.885 s en local: **Thunder fue 2.27× más rápido**, con un 100% de cobertura
de hablantes y evidencia en ambos. Esta fue la única victoria medida de la A100 en tiempo de reloj.

La detección de apodos usó un caso contextual explícito `BETTY → BEATRICE`. Local
terminó en 0.796 s frente a 1.782 s en remoto. Ambos lograron precisión 1.0,
recall 1.0 y cobertura de evidencia 1.0. Mantén los trabajos cortos de apodos en local; considera
Thunder para cohortes mayores de descubrimiento/compilación de personas después de medirlas.

### Exportaciones de CPU

Audacity y M4B se ejecutaron en Thunder en lugar de descartarse por las especificaciones de GPU.
Local siguió ganando: 0.040 s frente a 0.063 s en Audacity, y 0.167 s frente a 0.247 s
en la nueva ejecución reforzada de M4B. La estructura de miembros/etiquetas de Audacity coincidió exactamente.
Ambos M4B contenían audio AAC y exactamente dos capítulos con título y duración positiva. Los hashes
binarios difirieron porque las marcas de tiempo del ZIP y las versiones de FFmpeg son específicas de cada plataforma.

---

## Caso de estudio: Voice Lab — componentes medidos por etapa

Los componentes de Voice Lab listados se ejecutaron en local y en la misma instancia A100 80 GB
mediante fixtures respaldados por producción y verificados por hash. El resultado del preparer cubre
solo su fase de ASR; la alineación y la anotación no se midieron.

| Etapa | Local | A100 Thunder | Comparación de calidad |
|---|---:|---:|---|
| ASR del preparer, 36.08 s de audio | 11.24 s | 21.72 s | Misma transcripción normalizada de 91 palabras; los floats de alineación difirieron |
| Dedup, dos volúmenes / 8 muestras | 10.19 s | 19.09 s | Mismo clúster y contenido combinado canónico idéntico |
| Entrenamiento LoRA, 8 muestras / 1 epoch | 2.1 s | 6.5 s | Cero omisiones por OOM; ambos adaptadores verificados por hash |
| Perfilado, Qwen2.5 14B Q6 en frío | 6.936 s | 78.689 s | Métricas acústicas idénticas; ambas líneas de casting válidas |
| Nombrado, tres casos de colisión | 0.0216 s | 0.0541 s | Manifiestos idénticos byte a byte y nombres de directorio idénticos |

La conclusión medida es más fuerte que la estimación anterior: **mantén en local los componentes probados
de Voice Lab por velocidad**, no solo porque la configuración remota sea
incómoda. La calibración acotada de entrenamiento LoRA fue 3.10× más rápida en local, y
el perfilado fue 11.35× más rápido en local. La sobrecarga de transferencia y aprovisionamiento se
excluyó de esos resultados cronometrados del caso, así que incluirla solo refuerza la
recomendación local.

Esto no prueba que cada futuro trabajo grande de entrenamiento sea más rápido en local. Sí
prueba que las especificaciones de GPU por sí solas eran un mal predictor para esta pila.
Si un conjunto de datos sustancialmente mayor se queda sin memoria en la tarjeta de 16 GB, Thunder sigue siendo una
vía de escape por capacidad; mide esa nueva forma de conjunto de datos antes de tratar la A100
como una mejora de rendimiento.

Los trabajos de producción de Voice Lab lanzados desde la interfaz siguen siendo solo locales. Los adaptadores
de benchmark por SSH son infraestructura de calibración, no enrutamiento remoto de trabajos de producción.
Usar Thunder como vía de escape por capacidad requiere actualmente un flujo de trabajo manual;
el envío automático de Voice Lab a Thunder no se ha construido.

### Desglose de la fase de perfilado

El resultado del perfilado en frío combinó dos cargas de trabajo muy distintas:

| Fase | RX 9070 XT | A100 Thunder |
|---|---:|---:|
| Carga del modelo GGUF | 4.246 s | 57.344 s |
| Acústica de Librosa | 1.307 s | 16.696 s |
| Descripción de llama.cpp | 1.381 s | 4.642 s |

El GGUF exacto de 12.1 GB tenía el mismo SHA-256 en ambas máquinas. El texto con semilla puede
seguir difiriendo ligeramente entre backends ROCm/CUDA; los valores acústicos coincidieron exactamente.

---

## 5. Elegir una GPU (precios comprobados el 2026-07-18, por hora)

| GPU | $/h | Úsala cuando |
|---|---|---|
| **a6000_x1** | **0.35** | Vía de escape de capacidad/CUDA más barata. La concurrencia histórica de revisión por encima de 1 empeoró; no supongas velocidad. |
| l40_x1 | 0.79 | Solo si has medido que supera a la A6000 para tu trabajo. |
| l40s | 0.99 | Igual: mide primero. |
| a100xl_x1 | 1.09 | Capacidad de 80 GB y CUDA. En esta campaña su única victoria de tiempo de reloj fue la generación de personas; perdió la calibración medida de entrenamiento LoRA. |
| h100_x1 | 2.19 | No medida en este proyecto. Alquílala solo para una carga de trabajo que justifique una nueva calibración. |
| *_x2 / _x4 / _x8 | 2–23 | **No para este proyecto.** Ver §4: no puedes usar el paralelismo. |

Almacenamiento: disco $0.0003/GB/h, instantáneas $0.00006849/GB/h (las instantáneas son ~4×
más baratas que un disco en ejecución y ~1000× más baratas que una A6000 en ejecución).
Comprueba los [precios](https://www.thundercompute.com/pricing) actuales de Thunder y
la [documentación de facturación](https://www.thundercompute.com/docs/billing) antes de alquilar.

---

## 6. Modelo de coste: la parte que duele

**No hay parada automática por inactividad ni stop/pause. La facturación termina solo cuando
la instancia se ELIMINA.**

Una A6000 que olvidas:

| Olvidada durante | Coste |
|---|---|
| Una noche (12 h) | $4.20 |
| Un fin de semana | $16.80 |
| Un mes | **$252** |

El flujo de trabajo que se deduce de esto:

1. **Haz una instantánea antes de borrar** para preservar el estado: el almacenamiento de instantáneas es
   efectivamente gratis al lado de una instancia en ejecución.
2. **Borra la instancia** en el momento en que termina el trabajo.
3. **Verifica con `list_instances`**: no supongas que ha desaparecido.

> **La app no puede ayudarte aquí.** No hay integración de API de facturación/saldo
> en ningún lugar de este código. El prompt `confirmIfRemote()` de
> `static/index.html` (antes de `batch_review` / generación de guiones por lotes) es un sustituto
> deliberado: **avisa del coste, no lo calcula.** Nada te dirá
> que una instancia sigue en ejecución. Eso depende de ti.

---

## 7. Trampas de configuración (todas estas han mordido antes)

- **LM Studio debe enlazar `0.0.0.0`.** Un LM Studio remoto que informa "running"
  sigue siendo inalcanzable a través de la URL de reenvío sin
  `--bind 0.0.0.0`. Esto parece un bug de red y no lo es.
- **La salida de SSH lleva un banner decorativo.** Analiza la **última línea no vacía**;
  nunca supongas stdout limpio (ver `lmstudio_settings.get_remote_lmstudio_status`).
- **El SSH con varias sentencias debe ser UN solo elemento argv preentrecomillado:**
  `"bash -lc " + shlex.quote(cmd)`. Pasar `"bash", "-lc", cmd` por separado deja que
  `ssh` los vuelva a unir con espacios simples y rompe los comandos separados por `;`.
- **El `concurrency` en caché se queda obsoleto.** Tras un cambio de modelo o endpoint, comprueba
  que `concurrency_for` coincide con el `base_url::model_name` actual antes de fiarte
  del número en caché de `llm_remote`.
- **El reenvío de puertos** sigue `https://<uuid>-<port>.thundercompute.net`.
- **Una sola fuente de "es remoto".** Usa `lmstudio_settings.is_remote_llm`; nunca
  lo vuelvas a deducir por tu cuenta (Rule 15; esto ya causó una deriva real).
- **El wheel CUDA de llama.cpp necesita rutas de bibliotecas de NVIDIA.** Thunder usó
  `llama-cpp-python==0.3.23` del índice oficial de wheels CUDA 12.4. Importarlo
  directamente falló al principio porque `libcudart.so.12` estaba en los directorios
  `site-packages/nvidia/*/lib` del entorno de Python en lugar de la ruta
  del cargador del sistema. El envoltorio persistente `/home/ubuntu/ttsenv/bin/python-cuda`
  descubre esos directorios, los antepone a `LD_LIBRARY_PATH` y luego
  ejecuta `/home/ubuntu/ttsenv/bin/python`.
- **Verifica la ruta real de la GPU, no solo que la importación funcione.** El envoltorio se probó
  con inferencia GGUF real: llama.cpp detectó CUDA, descargó el modelo en la
  A100 y generó tokens. El benchmark de perfilado luego ejercitó el mismo
  envoltorio de extremo a extremo.
- **Usa los mismos bytes de modelo.** La comparación de perfilado usó
  `Qwen2.5-14B-Instruct-Q6_K.gguf` en ambas máquinas, SHA-256
  `18cd6b7d5feb00c57ff81ede8f2164ffd86be90dbee9c05bf09ded1ab179740d`.
  Thunder también tenía un Gemma GGUF distinto; no se sustituyó en la comparación
  justa de perfilado.
- **M4B requiere FFmpeg/FFprobe.** La imagen de la A100 no incluía FFmpeg. La
  comparación de exportaciones instaló Ubuntu FFmpeg 4.4.2. Haz preflight de esta dependencia en
  una instancia nueva en lugar de descubrirla después de preparar el audio.

---

## 8. Lista de comprobación rápida

Antes de alquilar:

- [ ] ¿Está esto realmente limitado por la GPU? (Las exportaciones se midieron en ambas máquinas y
      ganó local; usa Thunder solo si la colocación remota sirve para otra necesidad.)
- [ ] ¿Necesita >16 GB de VRAM o CUDA? (Si no → **probablemente quédate en local**.)
- [ ] ¿Es la ejecución local lo bastante larga como para que las horas importen? (Si no → **quédate en local**.)
- [ ] ¿Espero una victoria por paralelismo? (Si sí → vuelve a leer §4 y mide el
      endpoint exacto; el óptimo A100/Gemma fue paralelo 1.)

Después del trabajo:

- [ ] Haz una instantánea si el estado importa.
- [ ] **Borra la instancia.**
- [ ] `list_instances` → confirma vacío.
- [ ] Vuelve a poner `llm_mode` en `local`.
