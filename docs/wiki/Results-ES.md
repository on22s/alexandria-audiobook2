# Resultados

[English](Results.md) · [日本語](Results-JA.md) · [中文](Results-CN.md) · Español

Un índice compacto de los resultados de atribución de hablantes. Los artefactos JSON en bruto son la fuente de
verdad; esta página es un mapa para comparar ejecuciones sin mezclar conjuntos de prueba. Cada artefacto está
listado en [RESULTS_INDEX.md](../../RESULTS_INDEX.md).

## Cómo leer estas tablas

- **Precisión** cuenta las líneas sin responder o inválidas como incorrectas.
- **base → adaptador** compara el mismo modelo servido en el mismo conjunto de prueba, con el adaptador
  desactivado (escala 0) y activado.
- **Dos resultados solo son comparables si** coinciden el conjunto de prueba, la variante de prompt, los ajustes de decodificación y
  el formato de salida. El prompt se muestra siempre, porque los adaptadores son específicos de prompt.
- **Ajustes por defecto**, salvo que una fila diga otra cosa:
  - temperatura 0;
  - tamaño de lote 25 y como máximo 4,096 tokens de salida;
  - un esquema JSON en cada solicitud;
  - 40 ventanas uniformemente espaciadas por libro.
- **Razonamiento:** "off" significa sin razonamiento; "low" significa el ajuste bajo del proveedor, con el
  presupuesto del servidor en 1,024 tokens cuando se registró. No se incluyen resultados de razonamiento alto.

> **Repuntuado el 2026-09-28.** PDNC lista algunos nombres que nunca hablan; por ejemplo "Small"
> junto al Wooden-Legged Man de The Sign of the Four, cuyo alias es Jonathan Small. Un modelo
> que respondía SMALL nombraba a la persona correcta pero se puntuaba como incorrecto. Cinco pares de nombres revisados ahora
> cuentan como correctos, y la tabla emparejada de abajo usa esa puntuación. Diez veredictos anteriores
> cambiaron; RECIPES.md §"Rescored 2026-09-28" los lista.

## Resultados emparejados de adaptadores: nueve novelas de PDNC

Todas las filas: nueve novelas de PDNC, 2,655 líneas, el prompt `michel2_full`, el mismo servidor para ambas ramas.
El valor p es una prueba de signos exacta sobre las líneas en las que las dos ramas discrepan (corregidas / rotas).
Los nombres de adaptador siguen las etiquetas de prompt de entrenamiento de
[Publicaciones en Hugging Face](Hugging-Face-Releases-ES.md).

| modelo | quant | adaptador | razonamiento | líneas | base | adaptador | cambio | corregidas / rotas, p | artefacto |
|---|---|---|---:|---:|---:|---:|---|---|
| Qwen3-8B | Q8_0 | window25 `michel2v2` | low | 2,655 | 77.2% | 83.9% | **+6.7** | +378/−201, p=2e-13 | `qwen3-8b-window25-michel2v2-q8_0-paired-michel2_full-tnr-0-pdnc9-20260928` |
| Qwen3-8B | Q4_K_M | window25 `michel2v2` | low | 2,655 | 76.6% | 82.8% | **+6.2** | +372/−207, p=7e-12 | `qwen3-8b-q4km-window25-paired-michel2_full-post616-pdnc9-local9070xt-20260926` |
| Qwen3-8B | UD-Q3_K_XL | window25 `michel2v2` | low | 2,655 | 71.8% | 80.8% | **+9.0** | +456/−218, p=3e-20 | `qwen3-8b-ud-q3kxl-window25-michel2v2-paired-michel2_full-pdnc9-local9070xt-20260928` |
| Qwen3-8B | Q3_K_M | window25 `michel2v2` | low | 2,655 | 72.3% | 83.4% | **+11.0** | +476/−183, p=6e-31 | `qwen3-8b-window25-michel2v2-q3km-paired-michel2_full-tnr-0-pdnc9-20260928` |
| Qwen3-8B | UD-IQ3_XXS | window25 `michel2v2` | low | 2,655 | 66.7% | 79.9% | **+13.2** | +539/−189, p=8e-40 | `qwen3-8b-window25-michel2v2-ud-iq3xxs-paired-michel2_full-tnr-4-pdnc9-20260928` |
| Qwen3-8B | Q2_K | window25 `michel2v2` | low | 2,655 | 62.5% | 73.0% | **+10.5** | +575/−296, p=2e-21 | `qwen3-8b-window25-michel2v2-q2k-paired-michel2_full-tnr-2-pdnc9-20260928` |
| Qwen3-8B | UD-Q2_K_XL (seed 1) | window25 `michel2v2` | low | 2,655 | 62.3% | 74.5% | **+12.2** | +597/−274, p=2e-28 | `qwen3-8b-window25-michel2v2-ud-q2kxl-paired-michel2_full-tnr-2-pdnc9-20260928` |
| Qwen3-8B | UD-Q2_K_XL (seed 2, A100) | window25 `michel2v2` | low | 2,655 | 65.0% | 77.0% | **+11.9** | +530/−213, p=5e-32 | `qwen3-8b-window25-michel2v2-seed2-ud-q2kxl-paired-michel2_full-tnr-1-a100-pdnc9-20260928` |
| Qwen3-8B | UD-IQ2_M | window25 `michel2v2` | low | 2,655 | 60.0% | 75.9% | **+15.9** | +644/−223, p=4e-48 | `qwen3-8b-window25-michel2v2-ud-iq2m-paired-michel2_full-tnr-2-pdnc9-20260928` |
| Qwen3.8-27B | Q4_K_M | `michel2v1` | off | 2,655 | 94.4% | 95.9% | **+1.5** | +68/−29, p=9e-5 | `qwen38-q4km-off-michel2_full-pdnc9-tnr4-20260922` |
| Qwen3.8-27B | Q4_K_M | `michel2v1` | low | 2,655 | 95.8% | 96.4% | **+0.6** | +59/−44, p=0.17 | `qwen38-q4km-on-michel2_full-pdnc9-tnr4-20260922` |
| Qwen3.8-27B | UD-Q3_K_XL | `michel2v1` | low | 2,655 | 95.3% | 96.1% | **+0.8** | +66/−45, p=0.057 | `qwen38-q3kxl-on-michel2_full-pdnc9-tnr2-20260922` |
| Qwen3.8-27B | Q2_K_XL | `michel2v1` | low | 2,655 | 93.8% | 93.9% | **+0.1** | +91/−88, p=0.88 | `qwen38-rightsclean-michel2-q2kxl-paired-michel2_full-tnr-0q-pdnc9-20260924` |
| Qwen3.8-27B | UD-IQ2_XXS | `michel2v1` | low | 2,655 | 89.0% | 90.1% | **+1.0** | +126/−99, p=0.083 | `qwen38-rightsclean-michel2-iq2xxs-paired-michel2_full-tnr-0-pdnc9-20260924` |
| Qwen3.6-35B-A3B | UD-IQ1_M | window25 `michel2v2` | low | 2,655 | 90.7% | 92.1% | **+1.5** | +138/−99, p=0.013 | `a3b-window25-rightsclean-michel2-iq1m-paired-michel2_full-tnr-1-pdnc9-20260926` |
| Qwen3.6-35B-A3B | UD-IQ2_XXS | window25 `michel2v2` | low | 2,655 | 90.9% | 91.6% | **+0.7** | +135/−117, p=0.28 | `a3b-window25-rightsclean-michel2-iq2xxs-paired-michel2_full-tnr-1-pdnc9-20260927` |
| Qwen3.6-35B-A3B | UD-IQ3_XXS | window25 `michel2v2` | low | 2,655 | 92.5% | 92.8% | **+0.4** | +96/−86, p=0.5 | `a3b-window25-rightsclean-michel2-iq3xxs-paired-michel2_full-tnr-2-pdnc9-20260927` |
| Qwen3.6-35B-A3B | UD-Q4_K_XL | window25 `michel2v2` | low | 2,655 | 93.2% | 91.1% | **−2.1** | +78/−134, p=1e-4 | [`a3b-window25-rightsclean-michel2-q4kxl-paired-michel2_full-tnr-1-pdnc9-20260927`](../../ab_test_runtime/cloud_pull_20260928/lora_serving_eval__a3b-window25-rightsclean-michel2-q4kxl-paired-michel2_full-tnr-1-pdnc9-20260927.json) |
| Gemma 4 12B | Q4_K_M | window25 | low | 2,655 | 84.4% | 87.0% | **+2.6** | +174/−104, p=3e-5 | [`gemma4-12b-w25-q4km-paired-michel2_full-tnr-0-pdnc9-20260927`](../../ab_test_runtime/cloud_pull_20260928/lora_serving_eval__gemma4-12b-w25-q4km-paired-michel2_full-tnr-0-pdnc9-20260927.json) |
| Gemma 4 E4B | Q4_K_M | window25 (con su prompt de entrenamiento) | low | 2,655 | 63.5% | 66.5% | **+3.0** | +267/−188, p=2e-4 | `gemma4-e4b-w25-newprompt-q4km-paired-michel2_full-tnr-4-pdnc9-20260927` |
| Gemma 4 E2B | Q4_K_M | window25 (con su prompt de entrenamiento) | low | 2,655 | 42.3% | 53.1% | **+10.8** | +460/−174, p=1e-30 | `gemma4-e2b-w25-newprompt-q4km-paired-michel2_full-tnr-0-pdnc9-20260927` |
| Muse-Glimmer-30B | IQ3_XXS | window25b | low | 2,655 | 93.6% | 93.5% | **−0.1** | +87/−89, p=0.94 | `muse-window25b-iq3xxs-paired-michel2_full-tnr-2-post616-pdnc9-20260926` |
| Muse-Glimmer-30B | Q3_K_XL | window25b | low | 2,655 | 94.1% | 94.2% | **+0.1** | +81/−79, p=0.94 | [`muse-window25b-q3kxl-paired-michel2_full-tnr-4-pdnc9-20260926`](../../ab_test_runtime/cloud_pull_20260927/lora_serving_eval__muse-window25b-q3kxl-paired-michel2_full-tnr-4-pdnc9-20260926.json) |
| Muse-Glimmer-30B | Q4_K_M | window25b | low | 2,655 | 94.7% | 93.6% | **−1.1** | +58/−86, p=0.024 | `muse-window25b-q4km-paired-michel2_full-tnr-0-pdnc9-20260924` |
| Muse-Glimmer-30B | Q3_K_XL | KL seed 1 / seed 2 (ver RECIPES) | low | 2,655 | 94.6% | 95.1% | **+0.6** | +61/−46, p=0.18 | `muse-kl-window25-q3kxl-paired-michel2_full-tnr-2-pdnc9-20260926` |
| Muse-Glimmer-30B | IQ3_XXS | KL seed 2 | low | 2,655 | 93.1% | 94.6% | **+1.5** | +103/−63, p=0.0024 | `muse-kl-seed2-window25-iq3xxs-paired-michel2_full-tnr-1-pdnc9-20260927` |
| Qwen3-14B | Q4_K_M | rights-clean `attrv1`, prompt `default` | low | 2,655 | 68.4% | 73.2% | **+4.8** | +433/−306, p=3e-6 | `qwen3-14b-rightsclean-default-tnr2-pdnc9lite-low-schema-20260917` |
| Qwen3-14B | Q4_K_M | rights-clean `attrv1`, prompt `michel2_full` | low | 2,655 | 85.3% | 84.4% | **−1.0** | +193/−219, p=0.22 | `qwen3-14b-rightsclean-michel2_full-tnr2-pdnc9lite-low-schema-20260917` |

Notas:

- **The Sun Also Rises** es una de las novelas de entrenamiento de los adaptadores rights-clean `michel2v1`
  (las filas de Qwen3.8). Su cifra honesta en datos retenidos es la de las otras ocho novelas:
  +1.0 a +1.3 en Q4_K_M, UD-Q3_K_XL y UD-IQ2_XXS. Los adaptadores window25 y KL nunca
  se entrenaron con ninguna de las nueve.
- **Muse KL seed 1 en IQ3_XXS** puntuó −1.5 (p=0.003) en una A6000, mientras que la semilla 2 de arriba puntuó
  +1.5 en una A100. Hay en cola una prueba de la semilla 2 en la A6000, para distinguir semilla de GPU.
- Varios artefactos solo existen en las carpetas de experimentos y no están versionados; se da el nombre
  para que se puedan encontrar en [RESULTS_INDEX.md](../../RESULTS_INDEX.md).

## Resultados solo de base

Estas filas miden un modelo y un quant sin adaptador. Úsalas para elegir una base, no para
juzgar un adaptador.

| Modelo | Quant | Adaptador | Prompt | Ajustes | Fixture | Filas | Precisión | Precisión condicional | Artefacto / notas |
|---|---|---|---|---|---|---|---:|---:|---:|---|
| Muse-Glimmer-30B | UD-Q3_K_XL | none | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 9 novelas de PDNC | 2,655 | **93.33%** | 95.21% (1,869/1,963) | [resultado en bruto](../../ab_test_runtime/experiments/lora_serving_eval__muse-q3-michel2_full-tnr1-pdnc9lite-low-schema-20260917.json) |
| Muse-Glimmer-30B | Q4 K-quant | none | `michel2_full` | temp 0; low / 1,024; batch 25; schema | 9 novelas de PDNC | 2,655 | **94.61%** | 95.42% (1,873/1,963) | [resultado en bruto](../../ab_test_runtime/experiments/lora_serving_eval__muse-q4-kquant17-michel2_full-tnr4-pdnc9lite-low-schema-20260921.json) |
| Qwen3.8-27B | Q4_K_M | none | `michel2_full` | temp 0; reasoning off; batch 25; schema | 9 novelas de PDNC | 2,655 | 93.5% | — | Ver la fila emparejada de abajo |
| Qwen3.6-35B-A3B | UD-Q4_K_XL | none | `michel2_full` | temp 0.6; low / 1,024; batch 25; schema | fixture de producto de 4 libros | 768 | **89.6%** | 89.6% | Escalera de quant en [RECIPES.md](../../RECIPES.md) |
| Qwen3.6-35B-A3B | UD-IQ3_XXS | none | `michel2_full` | temp 0; low / 1,024; batch 25; schema | fixture de producto de 4 libros | 768 | 88.0% | 88.0% | Escalera de quant en [RECIPES.md](../../RECIPES.md) |
| Qwen3.8-27B | UD-Q4_K_M | none | `michel2_full` | temp 0; low / 1,024; batch 25; schema | fixture de producto de 4 libros | 768 | 89.8% | 89.8% | Escalera de quant en [RECIPES.md](../../RECIPES.md) |

Hay más escaleras solo de base, incluida la comparación de 28 novelas y las velocidades de los modelos, en
[Qué modelo para tu tarjeta](Which-Model-For-Your-Card-ES.md).

## Líneas base de API alojadas

Los resultados de API son solo de base. No los compares con los cambios de adaptador como si fueran el mismo
experimento.

- **DeepSeek v4-pro** (thinking off), nueve novelas: **98.0%** tras la repuntuación (96.7% antes),
  respondiendo a todas las líneas.
- **Nemotron 3 Ultra 550B A55B** mediante OpenRouter de pago, por libro. Son líneas base independientes
  de un solo libro: no las agrupes en una sola puntuación. OpenRouter informó del modelo
  solicitado, pero no se observaron el proveedor ni el hardware.

| Libro | Filas | Correctas | Precisión | Artefacto en bruto |
|---|---:|---:|---:|---|
| Mansfield Park | 734 | 710 | **96.7%** | [resultado en bruto](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-mansfieldpark-michel2full-b8-low-20260922.json) |
| Northanger Abbey | 672 | 656 | **97.6%** | [resultado en bruto](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-northangerabbey-michel2full-b8-low-20260922.json) |
| Persuasion | 309 | 300 | **97.1%** | [resultado en bruto](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-persuasion-michel2full-b8-low-20260922.json) |
| The Sign of the Four | 353 | 301 | **85.3%** | [resultado en bruto](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-thesignofthefour-michel2full-b8-low-20260922.json) |
| The Sun Also Rises | 1,759 | 1,536 | **87.3%** | [resultado en bruto](../../ab_test_runtime/experiments/lora_serving_eval__nemotron3-ultra-paid-clean-thesunalsorises-michel2full-b8-low-20260922.json) |

Ajustes de Nemotron: temperatura 0, razonamiento bajo con un presupuesto de 1,024 tokens, tamaño de lote 8, como
máximo 4,096 tokens de salida, `michel2_full`, salida estructurada desactivada. Estas puntuaciones son anteriores a la
repuntuación; The Sign of the Four sube de 85.3% a 96.6% una vez que SMALL cuenta. Ver
[Recetas de evaluación](Evaluation-Recipes-ES.md).

## Procedencia

Cada artefacto publicado registra su variante de prompt, modelo, quant y runtime, ruta y hash
del adaptador, hashes de los archivos de referencia, ajustes de decodificación, estado de validación y el resultado de cada
línea. Cuando se repite una ejecución, se conservan ambos artefactos y la repetición se etiqueta:
la medición anterior nunca se reemplaza.
