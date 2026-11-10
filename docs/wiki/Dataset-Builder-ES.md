# Dataset Builder

[English](Dataset-Builder.md) · [日本語](Dataset-Builder-JA.md) · [中文](Dataset-Builder-CN.md) · Español

La pestaña Dataset construye un conjunto de datos de entrenamiento LoRA de una muestra a la vez, dejándote escuchar cada
muestra antes de confirmarla.

## Cuándo usarlo

- Quieres ajustar las descripciones de voz y las emociones hasta que cada muestra suene bien.
- Quieres construir un conjunto de datos en varias sesiones.
- Quieres oír cómo cambian la voz distintas combinaciones de emoción y texto.

También es lo que abre el botón **Build New Dataset** de la pestaña Training.

## Flujo de trabajo

### 1. Crea un proyecto

Haz clic en **New Dataset** e introduce un nombre. Esto crea un espacio de trabajo en `dataset_builder/` que
se conserva entre sesiones.

### 2. Describe la voz

- **Root Voice Description** — la identidad de la voz; por ejemplo, "A warm, deep male narrator
  with a calm baritone". La [Referencia de voces](Voice-Reference-ES.md) explica qué palabras funcionan.
- **Global Seed** — opcional, para una generación reproducible; déjalo vacío para aleatoria.

### 3. Añade muestras

Haz clic en **Add Row** por cada muestra. Cada fila tiene:

| campo | significado |
|---|---|
| **Text** | lo que se dice en esta muestra |
| **Emotion** | una indicación de estilo añadida a la descripción de la voz ("cheerful", "tense whisper", "barking orders") |

La emoción es solo un prompt para **generar** el audio con VoiceDesign; no se guarda como
etiqueta de entrenamiento. (Si en cambio preparas grabaciones reales, consulta la
[Guía de entrenamiento](Training-Guide-ES.md#opción-2-subir-audio-real-zip).)

Consejos:

- Cubre una variedad de emociones: neutral, alegre, enojada, triste, tensa, autoritaria.
- Mezcla exclamaciones cortas ("Oh!", "Right.") con pasajes más largos.
- Incluye al menos un pasaje neutro largo para usarlo como muestra de referencia.
- La [Guía de entrenamiento](Training-Guide-ES.md) explica más sobre qué hace bueno a un conjunto de datos.

### 4. Genera y escucha

- **Generate** en una fila: renderiza solo esa muestra.
- **Generate Pending**: renderiza todas las muestras que aún no tienen audio.
- **Regen All**: vuelve a renderizar todas las muestras.
- **Cancel**: detiene un lote en ejecución; las muestras terminadas se conservan.

Escucha cada muestra y regenera las que no suenen bien, ajustando la emoción o la
descripción de la voz según haga falta.

### 5. Guarda como conjunto de datos

Cuando todas las muestras suenen bien:

1. Elige la **muestra de referencia**. Se convierte en `ref.wav`, el embedding de hablante usado en
   el entrenamiento: elige una línea clara y representativa, normalmente un pasaje neutro largo.
2. Haz clic en **Save as Training Dataset**. El proyecto se copia a `lora_datasets/` y aparece en
   la pestaña Training.

También puedes **Import / Export JSON** para mover las filas de un proyecto entre máquinas.

## Dónde se guardan los proyectos

- Cada proyecto vive en `dataset_builder/{name}/` en el servidor.
- Las filas, la descripción y la semilla se guardan en el `state.json` del proyecto, así que puedes cerrar
  el navegador y volver más tarde.
- Guardar un conjunto de datos **copia** los datos; el proyecto se queda, y puedes seguir trabajando en él.

## API

La [Referencia de la API](API-Reference-ES.md#dataset-builder) muestra cómo manejar el Dataset Builder
por HTTP.
