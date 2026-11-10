# Qué modelo para tu tarjeta

[English](Which-Model-For-Your-Card.md) · [日本語](Which-Model-For-Your-Card-JA.md) · [中文](Which-Model-For-Your-Card-CN.md) · Español

Qué modelo de lenguaje ejecutar para la atribución de hablantes, para cada cantidad de memoria de GPU.

**Cómo se midieron estos números.** Salvo que una fila diga otra cosa, cada cifra de precisión es la
misma medición:

- nueve novelas de dominio público de PDNC, 2,655 líneas con un hablante conocido;
- el prompt `michel2_full`, tamaño de lote 25, un esquema JSON en cada solicitud;
- temperatura 0 y razonamiento bajo;
- sin adaptador.

Nada se extrapola entre filas; donde un número no existe, la página lo dice. Las filas
de otro conjunto de prueba van etiquetadas, porque mezclar conjuntos de prueba en silencio es cómo un +4.3 se convirtió en un +0.3
en otra parte de este proyecto.

Las cifras solo de base de abajo son anteriores a la repuntuación del 2026-09-28, que cuenta como correctos nombres cortos
como SMALL para el Wooden-Legged Man de The Sign of the Four. Sube algunas bases hasta aproximadamente
1.3 puntos. Las cifras emparejadas repuntuadas están en [Resultados](Results-ES.md).

## Lee esto primero: la longitud de contexto decide si un modelo cabe

El tamaño del archivo no es la memoria que necesitas. Lo es la caché KV, y crece con el contexto que
pides. Medido en un modelo de 5.4 GB:

| contexto | memoria total de GPU |
|---|---:|
| `-c 4096` | **5.1 GB** |
| `-c 32768` | **11.3 GB** |

Estas cifras reportadas ilustran el uso de memoria dependiente del contexto de un modelo; no
predicen la memoria de otras arquitecturas o cuantizaciones. La guía anterior informa de una
ventana de atribución de 5,966 tokens, pero no enlaza el artefacto de recuento de tokens subyacente.
Esa longitud de entrada por sí sola no puede establecer el contexto necesario para una ejecución completa: la salida,
el razonamiento, los repartos más largos y las solicitudes de revisión también necesitan espacio. Empieza pequeño y sigue
[Elegir un tamaño de contexto](Setup-and-Serving-ES.md#elegir-un-tamaño-de-contexto). 8,192 puede ahorrar
memoria frente a 32,768, pero ninguno es el ajuste correcto para toda carga de trabajo.

## Las recomendaciones

| tu tarjeta | tarjetas típicas | ejecuta esto | archivo | base | ¿cargar el adaptador? |
|---|---|---|---:|---:|---|
| **32 / 24 GB** | RTX 5090, 4090, 3090 | Qwen3.8-27B **UD-Q3_K_XL** | 12.5 GB | **95.2%** | **sí**: +1.0 en las novelas con las que nunca se entrenó (p=0.004) |
| **16 GB** | RTX 5080, 5070 Ti, RX 9070 XT / 9070, 9060 XT 16 GB | Qwen3.8-27B **UD-Q3_K_XL** | 12.5 GB | **95.2%** | **sí**: igual que arriba |
| **12 GB** | RTX 5070, Intel Arc B580 | Qwen3.8-27B **Q2_K_XL** | 9.4 GB | **93.7%** | no: +0.1 (p=0.88) |
| **8 GB** | RX 9060 XT 8 GB, RTX 5060 | Qwen3.8-27B **UD-IQ2_XXS** | 6.9 GB | 88.7% | **sí**: +1.3 en las ocho retenidas (p=0.045) |
| **6 GB** | GTX 1660, RTX 2060, tarjetas de laptop | Qwen3-8B **Q4_K_M** | 4.7 GB | **76.6%** | **sí**: +6.2 (p=7e−12) |
| **sin GPU utilizable** | — | un modelo alojado, o el transporte manual | — | **94.9-95.4%** *(cuatro libros)* | n/a |

El adaptador de Qwen3.8 (`michel2v1`) se entrenó con The Sun Also Rises, una de las nueve novelas, así que sus
ganancias honestas son las ocho novelas que nunca vio. El adaptador window25 de Qwen3-8B nunca se entrenó con
ninguna de las nueve.

### 16 GB: la mejor relación calidad-precio

Este nivel cubre a la mayoría de la gente: la RTX 5080 y la 5070 Ti y toda la línea RX 9070 de AMD.
Qwen3.8-27B UD-Q3_K_XL ocupa 12.5 GB, así que con `-c 8192` queda cerca de 13 GB con margen de sobra y
puntúa **95.2%**. **Nada por encima de 16 GB compra un número mejor**: una 5090 ejecuta el mismo archivo con la
misma puntuación.

### 12 GB

Qwen3.8 **Q2_K_XL** puntúa **93.7%** con **9.4 GB**: un gigabyte menos que el Muse IQ3_XXS
que se recomendaba aquí antes, y 1.1 puntos por delante de él, con margen real con `-c 8192`.

**Sáltate el adaptador en este nivel.** Ninguno de los adaptadores medidos aquí ayuda:

- el adaptador de Qwen3.8 es +0.1 (p=0.88) en Q2_K_XL, y −0.7 (p=0.17) en sus ocho retenidas;
- el adaptador window25 de Muse era +0.2 en IQ3_XXS, +0.4 en IQ3_M y +0.2 en Q2_K_XL antes de la
  repuntuación (ninguno significativo), y la repuntuación deja las celdas que cubre dentro de ±0.5.

Una nota anterior aquí citaba un coste de −3.6. Eso venía del adaptador gen-3 de Muse, que se entrenó
con ejemplos de una sola línea y se sirvió con solicitudes de 25 líneas, así que medía el desajuste, no la
receta (ver [Prompts y adaptadores](Prompts-and-Adapters-ES.md)).

Una salvedad: las tres cifras de Muse window25 no se midieron todas con el mismo texto de prompt.
La celda IQ3_XXS se ejecutó en la A100 con el `MICHEL2_SYSTEM` más nuevo (2,071 caracteres), las celdas IQ3_M
y Q2_K_XL en A6000 con el antiguo (1,825 caracteres). En ese conjunto, el prompt y el tipo de GPU estaban perfectamente
confundidos, así que léelos como tres nulos pequeños separados que casualmente
coinciden, no como una tendencia.

### 8 GB

Qwen3.8-27B en UD-IQ2_XXS ocupa 6.9 GB y puntúa **88.7%**: un modelo 27B en una tarjeta de gama de entrada,
4.4 puntos por delante de un 14B en Q4_K_M que de todos modos no cabría. Su adaptador añade +1.3 en las ocho
novelas retenidas.

### 6 GB: elige un modelo pequeño que funcione, no uno grande encogido

Qwen3-8B Q4_K_M ocupa 4.7 GB y puntúa **76.6%** en las nueve novelas (77.0% en el conjunto de cuatro libros):
el mejor de los modelos pequeños, con margen con `-c 8192`.

**Carga su adaptador window25.** Lleva el modelo a **82.8% (+6.2, p=7e−12)** en las nueve
novelas, y a 82.3% (+5.3) en el conjunto de cuatro libros. En el conjunto de cuatro libros corrigió 90 de los
177 errores del base pero rompió el 8.3% de las líneas que tenía bien: la mayor rotura de cualquier adaptador medido,
así que la ganancia es real pero no gratis; ver RECIPES §"The adapter does two separable things".

Encoger un modelo grande no funciona aquí:

- Qwen3.8 `Q1_L` es **más grande** (5.7 GB) y puntúa **30.1%**;
- Muse `Q1_0` (4.5 GB) nunca terminó una ejecución: tres intentos, unas 37 horas de tarjeta en dos máquinas,
  y nunca mantuvo el formato JSON de 25 líneas el tiempo suficiente.

**La longitud de contexto importa sobre todo con 6 GB.** Entre `-c 8192` y un `-c 32768` por defecto, la
diferencia medida fue de **unos 6 GB** en un modelo de 5.4 GB: todo el presupuesto de una tarjeta de 6 GB.

### Sin GPU utilizable

No tener GPU no te detiene, y no es un premio de consolación: es la opción con mayor puntuación
de esta página.

- **Un modelo alojado.** Define un perfil LLM remoto en Setup y apúntalo a cualquier endpoint compatible con
  OpenAI. En el conjunto de cuatro libros con `michel2_full`, DeepSeek v4-pro puntúa **94.9%** con
  thinking off y **95.4%** con thinking low (presupuesto 8k), por unos $0.50-0.75 por ejecución de ese
  conjunto. En las nueve novelas puntúa **98.0%**. Un modelo alojado también deja tu GPU libre para renderizar
  audio mientras anota.
- **El transporte manual**, donde *tú* eres el modelo. La app escribe cada solicitud en un archivo y
  espera tu respuesta, así que puedes pegarla en cualquier modelo de chat que ya uses y pegar la
  respuesta de vuelta. Sin clave de API, sin GPU, sin coste: más lento y manual, pero completa libros enteros.
  Los issues detrás de #609-#616 se encontraron llevando un libro de principio a fin así.

## Si la velocidad importa más que los últimos puntos: Qwen3.6-35B-A3B

La tabla elige el archivo más preciso para cada tarjeta, no el más rápido, y la brecha de velocidad es
grande. A3B es un modelo de mezcla de expertos: 35B de pesos, pero solo unos 3B activos por token, así que
corre a velocidad de modelo pequeño con precisión casi de modelo grande.

Medido el 2026-09-27 en una RX 9070 XT, la misma prueba para cada fila (28 novelas de PDNC, 12 ventanas
cada una, `michel2_full`, razonamiento bajo, sin adaptador), tiempo de extremo a extremo por ventana de 25 líneas:

| modelo | archivo | precisión | tiempo por ventana | ejecución completa de 28 libros |
|---|---:|---:|---:|---:|
| Qwen3-8B Q4_K_M | 5.0 GB | 77.4% | 18.6 s | 1.7 h |
| Qwen3.5-9B Q4_K_M | 5.7 GB | 77.4% | 23.3 s | 2.2 h |
| **Qwen3.6-35B-A3B UD-IQ1_M** | 10.0 GB | **88.8%** | **19.7 s** | **1.8 h** |
| **Qwen3.6-35B-A3B UD-IQ2_XXS** | 10.8 GB | **87.5%** | **20.2 s** | **1.9 h** |
| Qwen3.8-27B UD-Q3_K_XL | 13.1 GB | 93.7% | 49.0 s | 4.6 h |

- A3B corre tan rápido como los modelos 8B y puntúa unos **11 puntos más**.
- Es unas **2.4× más rápido que Qwen3.8** por unos 6 puntos menos.
- Elígelo cuando importe el rendimiento (una biblioteca entera, volver a ejecutar libros, una tarjeta compartida con otro
  trabajo); elige la recomendación de 16 GB cuando importe la precisión.
- Su adaptador window25 (`qwen3.6-35b-a3b-rightsclean-window25-michel2v2`) solo ayuda en
  **IQ1_M** (+1.5, p=0.01); está plano en IQ2_XXS e IQ3_XXS, y **cuesta 2.1 puntos en
  Q4_K_XL**.

Estas son una tarjeta y una ejecución cada una. Los archivos más pequeños de Qwen3.8 (IQ2_XXS, Q2_K_XL) no se cronometraron
con esta prueba. No se espera que un 27B denso alcance la velocidad de un MoE en ningún quant, pero eso es una
expectativa, no una medición.

## Tres sorpresas medidas

**No pagues por Q4.** Qwen3.8-27B en Q3_K_XL (12.5 GB) puntuó **95.2%**; en Q4_K_M (15.7 GB),
**94.9%**. Los gigabytes extra no compraron nada: la diferencia está dentro del ruido. Para Muse,
Q4_K_M alcanzó 94.6% frente a 93.3% en Q3_K_XL, por 3 GB más.

**Un quant pequeño de un modelo grande supera a un quant grande de un modelo pequeño.** Qwen3.8-27B en IQ2_XXS ocupa
**6.9 GB y da 88.7%**; Qwen3-14B en Q4_K_M ocupa **9.0 GB y da 84.3%**. Si estás eligiendo entre un
14B que corre cómodo y un 27B que apenas cabe, haz sitio al 27B.

**El acantilado está entre Q3 e IQ2, no en Q4.** Qwen3.8 mantiene 95.2% en Q3_K_XL y cae a
88.7% en IQ2_XXS: un escalón, 6.5 puntos. Por encima de ese escalón, la escalera es casi plana. Si puedes
llegar a 12.5 GB, llega.

## ¿Deberías cargar un adaptador?

**Solo donde se ha medido que ayuda.** La regla de [Prompts y adaptadores](Prompts-and-Adapters-ES.md):
el valor de un adaptador depende de cuánto está sufriendo el base. Cada fila de abajo está emparejada en las
nueve novelas y usa la repuntuación del 2026-09-28. La columna "¿se sirvió con su prompt de entrenamiento?" se comprobó
celda por celda: cada fila anterior se ejecutó con el texto de prompt con el que se entrenó su adaptador, así que sus pérdidas
son reales, no un desajuste de prompt.

| base | adaptador (prompt de entrenamiento) | quant | ¿se sirvió con su prompt de entrenamiento? | base → adaptador | veredicto |
|---|---|---|---|---|---:|---|
| Qwen3-8B | window25 (`michel2v2`) | Q4_K_M | sí | 76.6 → **82.8** (+6.2) | **cárgalo** |
| Qwen3-8B | window25 (`michel2v2`) | UD-Q3_K_XL | sí | 71.8 → **80.8** (+9.0) | **cárgalo** |
| Qwen3.8-27B | `michel2v1` | Q4_K_M, reasoning off | sí | 94.4 → **95.9** (+1.5, p=9e−5); ocho retenidas +1.2 (p=6e−4) | **cárgalo** |
| Qwen3.8-27B | `michel2v1` | Q4_K_M, reasoning low | sí | 95.8 → 96.4 (+0.6, p=0.17); ocho retenidas **+1.1** (p=0.003) | **cárgalo** |
| Qwen3.8-27B | `michel2v1` | Q3_K_XL | sí | 95.3 → 96.1 (+0.8, p=0.06); ocho retenidas **+1.0** (p=0.004) | **cárgalo** |
| Qwen3.8-27B | `michel2v1` | IQ2_XXS | sí | 89.0 → 90.1 (+1.0, p=0.08); ocho retenidas **+1.3** (p=0.045) | **cárgalo** |
| Qwen3.8-27B | `michel2v1` | Q2_K_XL | sí | 93.8 → 93.9 (+0.1, p=0.88) | no |
| Qwen3.6-35B-A3B | window25 (`michel2v2`) | UD-IQ1_M | sí | 90.7 → **92.1** (+1.5, p=0.01) | **cárgalo** |
| Qwen3.6-35B-A3B | window25 (`michel2v2`) | UD-IQ2_XXS | sí | 90.9 → 91.6 (+0.7, p=0.28) | plano |
| Qwen3.6-35B-A3B | window25 (`michel2v2`) | UD-IQ3_XXS | sí | 92.5 → 92.8 (+0.4, p=0.50) | plano |
| Qwen3.6-35B-A3B | window25 (`michel2v2`) | UD-Q4_K_XL | sí | 93.2 → 91.1 (**−2.1**, p=0.0002) | **no** |
| Muse-Glimmer-30B | KL (`michel2v2`), seeds 1 / 2 | UD-Q3_K_XL | sí | 94.6 → 95.1 / 95.3 (+0.6 / +0.7) | marginal |
| Muse-Glimmer-30B | KL (`michel2v2`), seeds 1 / 2 | IQ3_M | **no** (texto anterior) | 92.3 → 93.3 / 93.2 (+1.0 / +0.9, p=0.08) | nueva ejecución con su prompt en curso |
| Qwen3-14B | `attrv1` | Q4_K_M, prompt `default` | no registrado | 68.4 → 73.2 (+4.8) | inútil: ver abajo |
| Qwen3-14B | `attrv1` | Q4_K_M, prompt `michel2_full` | no: una prueba de transferencia | 85.3 → 84.4 (−1.0) | no |
| Qwen3.6-35B-A3B | `michel2v1` | IQ1_M / IQ2_XXS | sí | −4.0 / −3.2 | **no** |
| Qwen3.6-35B-A3B | `michel2v1` | IQ3_XXS / Q4_K_XL | sí | **−20.3 / −27.5** | **no** |
| Muse-Glimmer-30B | `michel2v1` (gen 3) | IQ3_XXS / Q4_K_M | sí | −3.6 / −7.2 | **no** |

**La fila de Qwen3-14B es la trampa que merece la pena entender.** Con el prompt `default` el adaptador
parece una gran victoria: +4.8 puntos. Pero 73.2% con el adaptador sigue muy por debajo del **85.3%** que
el mismo modelo alcanza con `michel2_full` y sin adaptador. El adaptador estaba recuperando terreno que el
prompt regala. Si una victoria de adaptador parece grande, comprueba qué prompt usó el base.

**Los adaptadores son específicos de prompt y de quant.** Uno entrenado para `michel2` y servido con
`default` es una prueba de transferencia, no un resultado emparejado. Y un número de un quant no se traslada
a otro: el antiguo adaptador A3B es −20.3 en IQ3_XXS pero −3.2 en IQ2_XXS.

**Comprueba que se cargó, y comprueba la salida.** Un adaptador de Muse puntuó **4.7%**: el servidor estaba
activo y respondiendo, y casi todo estaba mal. Solo una ejecución emparejada en un servidor, con el
adaptador desactivado y activado, muestra eso; una ejecución con el adaptador solo habría parecido un
modelo malo.

## La escalera medida completa

Solo modelos base, nueve novelas:

| modelo | quant | archivo | precisión |
|---|---|---|---:|
| Qwen3.8-27B | UD-Q3_K_XL | 12.5 GB | **95.2%** |
| Qwen3.8-27B | UD-Q4_K_M | 15.7 GB | 94.9% |
| Muse-Glimmer-30B | KQuant Q4_K_M | 16.0 GB | 94.6% |
| Qwen3.8-27B | Q2_K_XL | 9.4 GB | 93.7% |
| Muse-Glimmer-30B | UD-Q3_K_XL | 12.75 GB | 93.3% |
| Muse-Glimmer-30B | IQ3_XXS | 10.6 GB | 92.6% |
| Qwen3.6-35B-A3B | UD-Q4_K_XL | 22.4 GB | 92.1% |
| Qwen3.6-35B-A3B | UD-IQ3_XXS | 12.6 GB | 91.6% |
| Qwen3.6-35B-A3B | UD-IQ2_XXS | 10.3 GB | 90.5-91.6% |
| Qwen3.6-35B-A3B | UD-IQ1_M | 9.6 GB | 89.3% |
| Qwen3.8-27B | UD-IQ2_XXS | 6.9 GB | 88.7% |
| Qwen3-14B | Q4_K_M | 9.0 GB | 84.3% |
| Qwen3-8B | Q4_K_M | 4.7 GB | 76.6% *(repuntuado)* |
| Qwen3.8-27B | Q1_L | 5.7 GB | 30.1% |
| Muse-Glimmer-30B | IQ2_XXS | 7.4 GB | 1.1%: por debajo del suelo del formato JSON |

- La fila A3B IQ2_XXS es un rango porque dos ejecuciones de la misma celda en máquinas distintas dieron
  91.6% y 90.5%. Esa dispersión de 1.1 puntos es una escala útil para todas las demás filas: las diferencias por debajo
  de un punto no son diferencias.
- Muse IQ2_XXS no es una puntuación débil sino ausente: 2,613 de 2,655 respuestas estaban vacías. No lo
  ejecutes sin el adaptador, que lo rescata a 77.8% en el conjunto de cuatro libros (+61.9): el único
  escalón donde un adaptador es esencial en lugar de opcional.

## El prompt importa más que todo esto

En el conjunto de cuatro libros, cambiar el prompt de `default` a `michel2_full` movió a Qwen3.8-27B
de 82.9% a 89.8%, y a Qwen3-14B de 65.9% a 82.0%. Eso es más que la brecha entre la mayoría de las filas
vecinas de arriba, y no cuesta nada.

Un ejemplo de equivocarse con esto: una ejecución de nueve novelas con el prompt `default` y una línea por
solicitud puntuó a Qwen3-8B en **33.7%**, frente a 76.6% para el mismo modelo con `michel2_full` y
lote 25. Elige `michel2_full` (el valor por defecto incluido) antes de gastar dinero en una tarjeta más grande.

## Aún no medido

- **El adaptador KL en IQ3_XXS.** La semilla 1 puntuó −1.5 en una A6000 y la semilla 2 +1.5 en una A100; hay en cola
  una ejecución de la semilla 2 en la A6000, para distinguir semilla de GPU.
- **La velocidad de los archivos más pequeños de Qwen3.8** (IQ2_XXS, Q2_K_XL) con la prueba de velocidad de arriba.
