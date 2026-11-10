# Alexandria Audiobook2 Wiki

[English](Home.md) · [日本語](Home-JA.md) · [中文](Home-CN.md) · Español

Alexandria Audiobook2 convierte un libro en un audiolibro con una voz distinta para cada
personaje. Un modelo de lenguaje determina quién dice cada línea y cómo debería sonar; un
modelo de texto a voz (Qwen3-TTS) la lee; y obtienes un MP3 o un M4B con capítulos.

Este fork mide cada decisión que toma. Las páginas de investigación de abajo dicen qué se midió
y cómo; las páginas de guía de usuario dicen cómo usar la app.

## Empieza aquí

- **Usa la app:** [Instala](../../README_ES.md#instalación), conecta un LLM y luego sigue
  [Tu primer audiolibro](../../README_ES.md#tu-primer-audiolibro). Empieza con la
  [muestra corta](../examples/first-book.txt) original, asigna presets de CustomVoice, renderiza, combina y
  descarga un MP3 antes de intentar un libro completo.
- **中文入门：**[安装与第一本有声书](../../README_CN.md#从这里开始)。本 Wiki 的详细页面也有[中文版](Home-CN.md)。
- **Investiga o desarrolla:** las [páginas de investigación de abajo](#investigación),
  [GOALS](../../GOALS.md), [RECIPES](../../RECIPES.md) y
  [las instrucciones para colaboradores](../../README_ES.md#colaborar) conservan las mediciones,
  los pasos de reproducción y el flujo de desarrollo. Nada de eso es requisito para hacer un libro.

- **日本語：**[日本語 Wiki](Home-JA.md)と[インストール・最初のオーディオブック](../../README_JA.md#インストール)。
- **Español:** [Wiki en español](Home-ES.md) e [instalación y primer audiolibro](../../README_ES.md#instalación).

## Usar la app

- [Configuración y servicio del modelo](Setup-and-Serving-ES.md) — conectar un modelo de lenguaje y ajustes de servicio
  que se midieron y funcionan.
- [Generación de guion](Script-Generation-ES.md) — las tres pasadas que convierten texto en guion,
  la pasada de revisión y los prompts.
- [Tipos de voz](Voice-Types-ES.md) — CustomVoice, Clone, LoRA y Voice Design, y cuándo usar
  cada uno.
- [Referencia de voces](Voice-Reference-ES.md) — un vocabulario para describir voces e instrucciones
  de interpretación, con resultados de pruebas.
- [Generación por lotes](Batch-Generation-ES.md) — cómo se agrupa el renderizado y cómo ajustarlo.
- [Editor y exportación](Editor-&-Export-ES.md) — editar líneas, modos de renderizado, combinación y exportación.
- [Solución de problemas](Troubleshooting-ES.md) — problemas habituales y sus correcciones.

## Construir voces

- [Dataset Builder](Dataset-Builder-ES.md) — construye un conjunto de datos de entrenamiento de voz con una vista previa de
  cada muestra.
- [Guía de entrenamiento](Training-Guide-ES.md) — entrena una voz LoRA, con ajustes que funcionaron.

## Referencia

- [Referencia de la API](API-Reference-ES.md) — ejemplos HTTP en curl, Python y JavaScript.

## Investigación

- [Qué modelo para tu tarjeta](Which-Model-For-Your-Card-ES.md) — el mejor modelo de lenguaje para
  cada cantidad de memoria de GPU, con velocidades.
- [Resultados](Results-ES.md) — resultados de modelos, quant, prompt y adaptadores en un solo lugar.
- [Prompts y adaptadores](Prompts-and-Adapters-ES.md) — las reglas que deben seguir los adaptadores y cuándo
  ayuda un adaptador.
- [Recetas de evaluación](Evaluation-Recipes-ES.md) — cómo se ejecuta una evaluación de adaptador.
- [Operaciones de Thunder](Thunder-Operations-ES.md) — ejecutar trabajos en GPU remotas.
- [Publicaciones en Hugging Face](Hugging-Face-Releases-ES.md) — qué debe incluir una publicación pública.

## La única regla para los adaptadores

Un adaptador debe evaluarse con el prompt con el que se entrenó. Un resultado con un prompt
distinto es una prueba de transferencia, no un resultado de ese adaptador.

## Comprobar una instalación

El repositorio incluye una suite de pruebas de API en vivo. La forma segura de ejecutarla arranca una copia
desechable de la app con su propia carpeta de datos vacía, así que tus libros y voces nunca se tocan:

```bash
cd app
python run_isolated_api_tests.py          # quick tests — no TTS or LLM needed
python run_isolated_api_tests.py --full   # also tests generation — needs a GPU and an LLM
```

Para probar una app que ya está en ejecución, usa `python -m tests.test_api --url http://127.0.0.1:<port>`
desde la carpeta `app`, pero ten en cuenta que crea y borra subidas, guiones y ajustes de voz
en los datos de esa app. (Ejecutar `python tests/test_api.py` directamente falla con
`ModuleNotFoundError: utils`; hay que ejecutarlo con `-m` desde `app`.)

- **El modo rápido** informa de sus propios recuentos de ejecutados/omitidos y cubre los viajes de ida y vuelta de ajustes,
  la subida, la biblioteca de guiones, los ajustes de voz, los fragmentos, el sondeo de estado, el diseño de voz, los listados
  de LoRA, el Dataset Builder y el manejo de errores, sin cargar ningún modelo.
- **El modo completo** añade generación de guion, generación de audio, renderizado por lotes, vistas previas de diseño de voz
  y pruebas de LoRA.

Pasar el modo rápido comprueba las rutas de API sin modelo que ejercita esa suite. No
descarta bugs de la aplicación en generación, carga de modelos, ejecución en GPU o integración con proveedores.
Usa los logs de la tarea que falla para distinguir esas causas; el modo completo cubre rutas adicionales
pero no es una prueba exhaustiva de cada modelo o libro.
