# Prompts y adaptadores

[English](Prompts-and-Adapters.md) · [日本語](Prompts-and-Adapters-JA.md) · [中文](Prompts-and-Adapters-CN.md) · Español

Las reglas que debe seguir todo adaptador de atribución, y lo que dicen las mediciones sobre cuándo
merece la pena cargar un adaptador.

## Regla 1: entrena con la ventana que el producto envía de verdad

**Esto ha desperdiciado más ejecuciones de adaptadores que cualquier otra cosa en el proyecto.** Un conjunto de entrenamiento
puede llevar el *nombre* correcto de prompt y aun así presentar una tarea completamente distinta. Nada
más adelante lo nota: el adaptador se entrena, se convierte, se sirve y produce números plausibles.

Ha ocurrido dos veces, con dos familias de modelos distintas:

| adaptador | entrenado con | servido con | coste |
|---|---|---|---|
| Muse gen-3 | **1 línea** por ejemplo | 25 líneas más 2,000 caracteres de texto circundante | detectado antes del entrenamiento, pero 61 horas de muestreo quedaron inservibles |
| A3B rights-clean (2026-09-17) | **1 línea** por ejemplo, `max_length 2048` | lo mismo | publicado; **cada resultado que produjo medía el desajuste, no la receta** |

El caso A3B es el aviso. Pasó su propio preflight, se sirvió limpiamente en cada rama, y su
ficha informó números cuidados y de aspecto honesto: planos o negativos en cada quant. Durante ocho días
se leyeron como un veredicto sobre la receta. Eran un veredicto sobre el desajuste.

**Comprueba la ventana, no la etiqueta.** Una comparación lo detecta:

```
lines per training example  vs  lines the serving prompt asks for
```

Si difieren, para. La corrección no necesita datos nuevos: el constructor de ventanas es determinista y funciona
para cualquier modelo base, así que los mismos libros se vuelven a renderizar con la forma correcta en segundos.

## Regla 2: un adaptador pertenece a su prompt

Entrena con `default`, sirve con `default`; entrena con `michel2_full`, sirve con `michel2_full`.
Servirlo con otro prompt convierte la ejecución en una **prueba de transferencia**, y debe etiquetarse como tal.

El nombre de cada adaptador publicado termina con el prompt con el que se entrenó (`attrv1`, `attrv2`,
`michel2v1`, `michel2v2`); ver [Publicaciones en Hugging Face](Hugging-Face-Releases-ES.md).

## Las variantes de prompt

| variante | qué envía | notas |
|---|---|---|
| `default` | la solicitud de lote simple | sin texto circundante |
| `michel` | la lista de reparto y el pasaje | no usa `MICHEL2_SYSTEM` |
| `michel2` | la lista de reparto y un pasaje marcado | |
| **`michel2_full`** | **un pasaje marcado más 2,000 caracteres de texto circundante** | **el prompt del producto** |
| `michel2_shot` | `michel2` más un ejemplo resuelto | |
| `michel2_spoken` | solo se marcan las líneas habladas; la narración es contexto, rellenada como NARRATOR por el código | la más reciente; +6.5 puntos sobre `michel2_full` en Qwen3-14B sin adaptador, ~3× menos tiempo en el paso 2 (#1171) |

`michel2_full` es la única variante que marca las líneas de narración con `[n]`. Eso importa: reformular
`MICHEL2_SYSTEM` en el #616 movió `michel2_full` en **+5.2** (Qwen3-8B, p<0.001) y **+3.0**
(Qwen3.5-9B, p=0.025), y no produjo ninguna diferencia medible en `michel2` ni en `michel2_shot` en
ninguno de los dos modelos, ni en Muse en absoluto (695 de 768 líneas antes y después).

## Cuándo ayuda un adaptador y cuándo te cuesta

En cuatro familias de modelos, la ganancia de un adaptador sigue **cuánto está degradado el modelo base**, no
lo bueno que sea el adaptador:

| puntuación base en esta tarea | qué hace el adaptador |
|---|---|
| **por debajo de ~35%** (el formato de salida se está desmoronando) | **+57 a +62 puntos**: recupera la capacidad de responder, directamente |
| 85-90% | +1 a +4, a menudo no significativo |
| **por encima de ~90%** | **±1-2 puntos**: el adaptador A3B window25 cuesta 2.1 en Q4_K_XL; Muse window25b gana 1.1 en Q4_K_M con su propio prompt |

Ejemplos de cada extremo: Muse IQ2_XXS **15.9 → 77.8** (A6000) y **22.2 → 79.0** (RX 9070 XT);
Muse Q4_K_M **90.9 → 88.1** (cuatro libros, servido con el texto de un prompt anterior; con su prompt de entrenamiento
la celda de nueve novelas en Q4_K_M es 94.2 → 95.2).

*Corregido el 2026-09-27 y el 2026-09-28:*

- El ejemplo de Gemma E2B que aparecía aquí (7.4 → 58.5) venía de un archivo base sin plantilla de chat,
  así que medía un fallo de empaquetado, no a Gemma.
- El 86.9 → 85.2 de cuatro libros de Gemma 12B eran 3 líneas corregidas frente a 6 rotas (p=0.51): ruido. En las
  nueve novelas el mismo adaptador es +1.1 (p=0.09) una vez puntuados los nombres cortos silenciosos de PDNC (antes
  se leía +2.1).

**Cómo funciona**, medido línea a línea en 2,655 líneas: el adaptador recupera alrededor de **la mitad** de
lo que rompe la cuantización, conserva el **97%** de lo que ya estaba bien, y rompe alrededor del **3.4%** de
esas líneas correctas en cada quant. Donde no hay nada que recuperar, solo se ve la rotura.

**Regla práctica:** si tu modelo base ya puntúa por encima de ~90% en esta tarea, un adaptador
lo mueve un punto o dos como mucho, en cualquier dirección: mídelo con su prompt de entrenamiento antes de
fiarte de él. Carga uno donde el base falla visiblemente (respuestas en blanco, JSON malformado, número
de líneas equivocado), no para perseguir unos puntos en un modelo que ya funciona.

**Sirve los adaptadores de Muse con la fuerza de razonamiento con la que se entrenaron (`low`), excepto en IQ2_XXS,**
donde `low` midió peor para ambos adaptadores (window25b 71.8 → 65.8, KL 83.4 → 82.4) porque el
modelo de 2 bits sigue hasta el límite de longitud; sirve IQ2 con "none". El
`--chat-template-kwargs` del servidor tiene prioridad sobre la solicitud, y `{"reasoning_strength":"none"}` se imprime
literalmente como un valor no entrenado; RECIPES §"September 28: prompt text, reasoning strength, and the
cast list" tiene la tabla completa.

## El conjunto de prueba cambia la respuesta

El mismo adaptador, los mismos quants, dos conjuntos de prueba:

| | conjunto de cuatro libros (352 líneas) | conjunto de nueve novelas de PDNC (5,310 líneas) |
|---|---|---|
| IQ3 agrupado | **+4.3, p = 0.014** | **+0.3, p = 0.489** |

Con quince veces más líneas el efecto desaparece, porque el base de nueve novelas puntúa 92.4
y el base de cuatro libros 85.5, a un lado y otro del punto donde el adaptador deja de compensar.
**Nunca promociones basándote en un solo conjunto de prueba**, y di siempre de cuál viene un número.

El puntuador también importa. El 2026-09-28, contar como correctos nombres cortos como SMALL para el
Wooden-Legged Man de The Sign of the Four cambió diez veredictos de adaptadores. A algunos adaptadores se les había acreditado
haber aprendido la costumbre de etiquetado de PDNC; uno había estado ocultando errores reales (RECIPES §"Rescored
2026-09-28").

El hardware importa menos, pero no cero: la misma celda en una A6000 y una RX 9070 XT movió el
cambio medido de +4.5 a +1.1, mientras que dos A6000 con la misma compilación coincidieron exactamente (0 de 352
predicciones difirieron).

## Dónde están las preguntas abiertas (actualizado el 2026-09-28)

- **A3B reentrenado con la forma de ventana correcta: hecho.** El adaptador window25 solo ayuda en IQ1_M
  (+1.5, p=0.01); está plano en IQ2_XXS e IQ3_XXS, y cuesta 2.1 puntos en Q4_K_XL (p=0.00015).
  Cárgalo solo en IQ1_M.
- **Entrenar en 4 bits (QLoRA): hecho.** Redujo la rotura un 18% (105 → 86 líneas), pero no de forma
  significativa (p=0.099), y la reparación cayó más o menos lo mismo, así que la red no se movió.
- **Mantener el adaptador cerca del base (penalización KL): en curso.** Dos semillas coinciden en IQ3_M
  y Q3_K_XL, pero discrepan en IQ3_XXS (semilla 1 −1.5 en una A6000, semilla 2 +1.5 en una A100). Hay
  en cola una ejecución de la semilla 2 en la A6000, para separar semilla de GPU.
- **Longitud del reparto: probada, y una lista de reparto más corta perjudica.** La ganancia del adaptador había sido +2.4
  con 26-40 nombres candidatos y −4.1 con 71 o más, así que una celda ofreció solo los personajes
  mencionados en la ventana más los hablantes de la ventana anterior, con un máximo de 30
  (`--roster-mode mentioned`; Muse window25b, IQ3_M, nueve novelas):

  | lista de reparto | nombres ofrecidos (mediana) | base | con adaptador |
  |---|---:|---:|---:|
  | completa | 57 | 92.3% | 92.1% |
  | solo mencionados, tope 30 | 12 | **63.6%** | 81.1% |

  La lista corta omitió al hablante real mucho más a menudo (se ofreció en el 37% de las líneas,
  frente al 74%; ese flag cuenta de menos en ambas ejecuciones, así que solo la comparación significa algo), e
  incluso en las 993 líneas en las que se ofreció, el base puntuó 93.5% frente a 95.9% con la
  lista completa. El +17.6 del adaptador es sobre todo reparación del daño que causó la lista corta.

  **Replicado en DeepSeek v4-pro** (razonamiento desactivado, las mismas nueve novelas): 98.0% con la lista completa,
  86.0% con la corta. En las líneas en las que la lista corta seguía ofreciendo al hablante puntuó
  98.7% frente a 99.0%, así que para un modelo fuerte toda la pérdida es el hablante que falta. La lista
  completa de reparto se queda.

Recetas, artefactos y desgloses por libro: [RECIPES.md](../../RECIPES.md). Resultados actuales:
[Resultados](Results-ES.md).
