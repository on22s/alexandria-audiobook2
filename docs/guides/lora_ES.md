[English](lora.md) | [中文](lora_CN.md) | [日本語](lora_JA.md) | [Español](lora_ES.md)

# Guía de entrenamiento LoRA de Qwen3-TTS para Alexandria

## ¿Qué es LoRA? (en palabras sencillas)

LoRA (Low-Rank Adaptation) es una forma de **enseñar al modelo TTS una voz nueva** sin reentrenar todo el modelo. Piénsalo así:

- El modelo TTS base ya sabe hablar; simplemente no conoce **tu voz concreta**
- Un adaptador LoRA es un archivo pequeño (normalmente menos de 100 MB) que enseña al modelo cómo suena tu voz
- Una vez entrenado, puedes usar este adaptador para que el modelo TTS hable con esa voz de forma consistente

### ¿Para quién es esto?

- **Quieres una voz personalizada persistente** que puedas reutilizar en varios proyectos
- **Tienes muestras de audio limpias** de la voz que quieres clonar (al menos 30 clips cortos)
- **Tienes una GPU con 8+ GB de VRAM** (se recomiendan 16 GB+)

### ¿Cómo se relaciona esto con el resto de Alexandria?

1. **Pestaña Dataset Builder** — donde creas el conjunto de datos de entrenamiento (clips de audio + texto)
2. **Pestaña Training** — donde ejecutas el entrenamiento LoRA
3. **Pestaña Voices** — donde usas el adaptador LoRA entrenado seleccionando "LoRA Voice" y apuntando al archivo del adaptador

### Inicio rápido: entrena tu primera voz

#### Paso 1: prepara tu audio

Necesitas **30-120 clips de audio cortos** (de 2-10 segundos cada uno) de la voz que quieres clonar:
- Grabación clara, con el mínimo ruido de fondo
- Cada clip debe ser una sola frase o expresión hablada
- Guárdalos como archivos WAV (16 bits, 24 kHz preferiblemente)

#### Paso 2: crea un conjunto de datos

En la interfaz web de Alexandria:
1. Ve a la pestaña **Dataset**
2. Sube tus clips de audio uno a uno
3. Para cada clip, escribe el texto que se dijo
4. Haz clic en **Save Dataset** cuando termines

#### Paso 3: entrena el adaptador LoRA

1. Ve a la pestaña **Training**
2. Selecciona tu conjunto de datos
3. Usa estos ajustes para tu primer entrenamiento:
   - **Epochs:** 9-10 (para ~60 muestras)
   - **Learning Rate:** 1e-6 (el valor por defecto de la app)
   - **LoRA Rank:** 64
   - **LoRA Alpha:** 128
   - **Gradient Accumulation:** 4
4. Haz clic en **Start Training**
5. Observa el número de **Loss**: detén el entrenamiento cuando llegue a **4.1-4.2**

#### Paso 4: usa tu nueva voz

1. Ve a la pestaña **Voices**
2. Selecciona **LoRA Voice** como tipo de voz
3. Apunta a tu archivo de adaptador entrenado
4. ¡Tu nueva voz está lista para usarse!

### Entender el número de Loss

El **Loss** indica qué tan bien está aprendiendo el modelo tu voz:
- **Por encima de 4.5:** la voz suena clara pero no se parece mucho a tu objetivo (poco entrenada)
- **4.1-4.2:** el punto óptimo: audio claro, buen parecido de voz y sigue las instrucciones
- **Por debajo de 4.1:** riesgo de audio distorsionado o roto (sobreentrenada) — **detén el entrenamiento inmediatamente**
- **Por debajo de 3.5:** el audio se distorsiona de forma consistente — entrenaste demasiado

**Regla general:** revisa el loss cada pocos minutos. Cuando llegue a 4.15, detén el entrenamiento. No lo fuerces más abajo.

---

## Referencia rápida

| Tamaño del conjunto | Epochs | Learning Rate | LoRA r | LoRA Alpha | Grad Accum | Loss objetivo |
|-------------|--------|---------------|--------|------------|------------|-------------|
| ~30 muestras | no medido a 1e-6 | 1e-6 | 64 | 128 | 4 | 4.1-4.2 |
| ~60 muestras | 9-10 | 1e-6 | 64 | 128 | 4 | 4.1-4.2 |
| ~120 muestras | no medido a 1e-6 | 1e-6 | 64 | 128 | 4 | 4.1-4.2 |

> **Usa una tasa de aprendizaje de 1e-6.** Versiones anteriores de esta guía recomendaban 2e-6 a
> 5e-6. Los adaptadores entrenados a 5e-6 nunca dejan de hablar: no producen un fin del
> habla, y la loss de entrenamiento parece normal, así que la loss no puede avisarte (ver
> [RECIPES.md](../../RECIPES.md)). Las 9-10 epochs de arriba vienen de las ejecuciones de 61 muestras
> a 1e-6 de la tabla al final de esta guía (male-lora-03 y -05). No se ha medido
> ningún otro tamaño de conjunto a 1e-6, así que vigila la loss y escucha el resultado.

**Loss objetivo: 4.1-4.2** — este es el punto óptimo para identidad de voz + seguimiento de instrucciones + audio limpio. La loss 4.1 es el suelo; por debajo, la distorsión se vuelve cada vez más probable. Ten en cuenta que ajustes idénticos pueden producir losses ligeramente distintas entre ejecuciones, así que apunta a 4.15-4.2 para tener un margen fiable.

## Principios clave

- **Más datos = menos epochs.** Cada epoch enseña más con un conjunto mayor, así que hacen falta menos pasadas antes del sobreajuste.
- **La exposición total importa.** Muestras x epochs debería quedar alrededor de 250-400 pasadas hacia adelante en total. Pasar de 600 arriesga sobreajuste.
- **Loss por debajo de 4.1 = riesgo de distorsión.** La varianza entre ejecuciones hace que la misma configuración pueda caer a un lado u otro de 4.1. Por debajo de 3.5, la salida se distorsiona de forma consistente o no llega a EOS.
- **Loss por encima de 4.5 = poco entrenada.** Audio claro pero identidad de voz débil y seguimiento de instrucciones tenue.

## Qué hace cada ajuste

| Ajuste | Efecto |
|---------|--------|
| **Epochs** | Número de pasadas completas por el conjunto de datos. Más = ajuste más estrecho. |
| **Learning Rate** | Cuánto se ajustan los pesos por paso. Más alto = aprendizaje más rápido pero más arriesgado. |
| **LoRA Rank (r)** | Capacidad del adaptador (número de dimensiones entrenables). 64 es un buen valor por defecto. |
| **LoRA Alpha** | Factor de escala. La relación alpha/r controla el peso efectivo del adaptador. 128/64 = 2x es el valor por defecto probado. |
| **Grad Accumulation** | Simula tamaños de lote mayores. 4 es estable en la mayoría de los casos. |
| **Batch Size** | Muestras por paso. Mantenlo en 1 (limitado por VRAM). |

## Síntomas de sobreajuste

| Loss | Calidad del audio | Seguimiento de instrucciones | Veredicto |
|------|--------------|-------------------|---------|
| 4.4+ | Claro, sin distorsión | Leve/tenue | Poco entrenada |
| 4.1-4.2 | Claro, expresivo | Bueno | Punto óptimo |
| 3.9-4.1 | Expresivo pero con riesgo de distorsión | Fuerte | Al filo de la navaja: la varianza entre ejecuciones puede distorsionar |
| 3.4-3.8 | Distorsionado pero legible | Fuerte | Empieza a sobreajustar |
| 3.0-3.3 | Distorsionado / sin EOS | N/A | Sobreajustada, inutilizable |

## Preparación del conjunto de datos

### Usar el Dataset Builder (recomendado)

1. Ve a la pestaña **Dataset** de Alexandria
2. Escribe una descripción de la voz y añade filas (pares de emoción + texto)
3. Genera muestras: cada fila produce un WAV mediante VoiceDesign
4. Elige una línea clara y representativa como **muestra de referencia** (se usa como `ref.wav` para el embedding de hablante durante el entrenamiento)
5. Guarda como conjunto de datos: crea la carpeta de entrenamiento automáticamente

### Consejos para buenos conjuntos de datos

- **Incluye variedad:** mezcla emociones, ritmos, niveles de volumen y longitudes de frase
- **Incluye expresiones cortas:** "Oh!", "Hmm.", "Right." — ayudan al modelo a aprender el comportamiento de EOS con entradas cortas
- **Termina con un pasaje neutro:** un párrafo largo, tranquilo y descriptivo es una muestra de referencia ideal
- **Usa una semilla consistente** para la muestra de referencia, para mantener estable el embedding de hablante entre regeneraciones
- **15-30 minutos** de audio total es el objetivo para un perfil de voz de calidad

### Estructura del conjunto de datos

```
lora_datasets/{name}/
├── metadata.jsonl      # {audio_filepath, text} per line
├── ref.wav             # Reference audio for speaker embedding
├── ref_text.txt        # Transcript of ref.wav (must match exactly)
└── sample_000.wav ...  # Training audio files
```

### Formato de metadatos

```json
{"audio_filepath": "sample_000.wav", "text": "I told you never to come back here!"}
{"audio_filepath": "sample_001.wav", "text": "I just don't know what to do anymore."}
```

## Multilingüe

Los adaptadores LoRA entrenados con datos de un solo idioma llevan el acento y la pronunciación de ese idioma. Un adaptador entrenado con muestras en inglés renderizará texto en alemán con pronunciación inglesa: el modelo base a veces da suficiente guía para obtener resultados aceptables, pero es inconsistente.

**Entrena un LoRA separado por idioma para cada hablante.** Esto produce una pronunciación limpia sin pelear con la fonología aprendida del adaptador.

### Idioma nuevo frente a identidad de voz

Las losses objetivo de esta guía (4.1-4.2) se establecieron para el entrenamiento de **identidad de voz en inglés**, donde el modelo base ya conoce el idioma y el adaptador solo aprende una voz nueva. Cuando se entrena un **idioma que el modelo base no admite de forma nativa**, el adaptador debe aprender a la vez la nueva fonología y la identidad de voz. Es una tarea más difícil que desplaza el límite del sobreajuste hacia arriba:

| Objetivo del entrenamiento | Loss objetivo | Suelo de distorsión |
|--------------|-------------|--------------|
| Identidad de voz (idioma admitido) | 4.1-4.2 | ~4.1 |
| Idioma nuevo + identidad de voz | 4.5-5.5 | ~4.1-4.5 (sigue distorsionando con una loss absoluta similar) |

Para idiomas nuevos, usa tasas de aprendizaje mucho más bajas y espera una loss final más alta. Una loss de 5.0-5.5 puede producir habla limpia y natural con buena pronunciación: el adaptador ha aprendido el idioma pero no se le ha empujado al sobreajuste. El seguimiento de instrucciones será más débil con estas losses más altas, pero la identidad de voz y la pronunciación son la prioridad.

**Ejemplo probado:** catalán femenino, 2321 muestras, 1 epoch, lr=2e-7, r=64, alpha=128 — loss final 5.38, habla limpia con pronunciación catalana correcta. Los intentos anteriores con lr=1e-5 (loss 3.78) produjeron salida distorsionada pese a estar en el rango "normal" de identidad de voz en inglés.

## Solución de problemas

| Problema | Causa | Solución |
|---------|-------|-----|
| Audio distorsionado con texto nuevo | Sobreajuste (loss demasiado baja) | Reduce las epochs o el lr |
| La generación se cuelga / sin EOS | Sobreajuste severo | Reentrena con menos epochs |
| Claro pero sin identidad de voz | Poco entrenada (loss demasiado alta) | Aumenta las epochs o el lr |
| Habla rápida/apresurada | Los datos de entrenamiento tenían un ritmo rápido | Usa "slow, even narration" en el instruct, o reajusta el conjunto de datos |
| Los textos cortos se cuelgan en max_new_tokens | El modelo nunca aprendió el EOS de expresiones cortas | Añade vocalizaciones cortas a los datos de entrenamiento |
| Glitch de audio inicial | Artefacto de alineación del prompt de clonación | Menor — normalmente no aparece en la generación de un audiolibro completo |
| ref.wav no coincide | ref_text.txt no coincide con el contenido de ref.wav | Asegúrate de que ref_text.txt contiene la transcripción exacta de ref.wav |

## Configuraciones probadas (Alexandria)

| Adaptador | Muestras | Epochs | LR | Alpha | Loss | Resultado |
|---------|---------|--------|----|-------|------|--------|
| female-lora-01 | 33 | 3 | 1e-5 | 128 | 3.93 | Funciona, ritmo algo rápido |
| female-lora-02 | 121 | 15 | 3e-6 | 128 | 3.03 | Sobreajustada, distorsionada |
| female-lora-03 | 121 | 5 | 5e-6 | 128 | 3.10 | Sobreajustada, sin EOS |
| female-lora-04 | 121 | 2 | 5e-6 | 128 | 3.86 | Comprensible, distorsiones + tonos raros |
| female-lora-05 | 121 | 1 | 5e-6 | 128 | 4.43 | Clara, instrucciones débiles |
| female-lora-06 | 121 | 3 | 2e-6 | 64 | 3.46 | Distorsionada pero legible |
| **female-lora-07** | **121** | **3** | **2e-6** | **128** | **4.11** | **La mejor: audio claro, buenas instrucciones** |
| male-lora-01 | 61 | 5 | 1e-6 | 128 | 4.44 | Clara pero plana, poco seguimiento de instrucciones |
| male-lora-02 | 61 | 7 | 1e-6 | 128 | 4.31 | Emotiva, responde a las instrucciones |
| **male-lora-03** | **61** | **10** | **1e-6** | **128** | **4.11** | **La mejor: expresiva, rica, buenas instrucciones** |
| male-lora-04 | 61 | 10 | 1e-6 | 128 | 4.12 | Misma configuración que 03, algunas líneas distorsionadas (varianza entre ejecuciones) |
| male-lora-05 | 61 | 9 | 1e-6 | 128 | 4.17 | Limpia, expresiva, margen seguro |
| male-lora-06 | 61 | 12 | 1e-6 | 128 | 3.99 | Muy expresiva pero 50% distorsionada |
| male-lora-07 | 61 | 14 | 1e-6 | 128 | 3.89 | Legible pero sobreajustada |
| catalan-f-01 | 2321 | 1 | 1e-5 | 128 | 3.78 | Distorsionada: lr demasiado alto para un idioma nuevo |
| **catalan-f-07** | **2321** | **1** | **2e-7** | **128** | **5.38** | **Catalán limpio, buena pronunciación** |
