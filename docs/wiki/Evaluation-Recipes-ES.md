# Recetas de evaluación

[English](Evaluation-Recipes.md) · [日本語](Evaluation-Recipes-JA.md) · [中文](Evaluation-Recipes-CN.md) · Español

Cómo se ejecuta una evaluación de adaptador. Los ajustes medidos y su evidencia están en
[RECIPES.md](../../RECIPES.md).

## La configuración emparejada

Usa el evaluador del repositorio, con **un servidor** y dos escalas de adaptador:

- **rama base:** escala de adaptador 0.0;
- **rama del adaptador:** la escala declarada del adaptador: 1.0, salvo que un barrido de escala registrado
  eligiera otro valor.

Ambas ramas usan:

- temperatura 0;
- tamaño de lote 25;
- un esquema JSON en cada solicitud;
- la misma variante de prompt.

## Qué informar

- la precisión, y el número de líneas sin responder y de fallos del analizador;
- la puntuación de cada libro;
- el hash del adaptador y el hash del modelo base;
- la variante de prompt;
- detalles de ejecución (compilación del servidor, GPU, longitud de contexto).

## Cuándo se puede promocionar un adaptador

- **Nunca a partir de una prueba de humo con tamaño de lote 1.** La promoción requiere la evaluación con la ventana
  del producto y un preflight limpio del contrato servido (ver [Setup y servido](Setup-and-Serving-ES.md)).
- **Si un piloto elige una escala distinta de 1.0**, repite exactamente esa escala sobre el conjunto de prueba completo
  e informa de todas las escalas que se probaron. Nunca reetiquetes un fallo con escala 1 como éxito.
- **Cita el conjunto de prueba cada vez.** El mismo adaptador puede mejorar en un conjunto de prueba y no en
  otro (ver [Prompts y adaptadores](Prompts-and-Adapters-ES.md)).
- **Comprueba también el puntuador, no solo el modelo.** La repuntuación del 2026-09-28 cambió diez veredictos
  porque los nombres cortos correctos se puntuaban mal.

## Líneas base de API de pago

Mantén los resultados de API alojadas, que son solo de base, separados de las evaluaciones de adaptadores.

Las ejecuciones de Nemotron 3 Ultra 550B A55B del 2026-09-22 pasaron por OpenRouter con
`michel2_full`, razonamiento bajo, temperatura 0, tamaño de lote 8 y sin salida estructurada. Puntuaciones
por libro:

| libro | precisión |
|---|---:|
| Mansfield Park | 96.7% |
| Northanger Abbey | 97.6% |
| Persuasion | 97.1% |
| The Sign of the Four | 85.3% (96.6% tras la repuntuación del 2026-09-28) |
| The Sun Also Rises | 87.3% |

- Son libros independientes: no los agrupes en una sola puntuación de modelo, ni los leas
  como efectos de adaptador.
- OpenRouter informó del modelo solicitado. El enrutamiento prefirió Baseten, luego Venice, con
  alternativas; no se observó el hardware del proveedor.
- Denominadores y artefactos:
  [RECIPES.md](../../RECIPES.md#september-22-follow-up-low-quant-adapter-and-nemotron-api-baselines).
