[English](SCORING_POLICY.md) | [中文](SCORING_POLICY_CN.md) | [日本語](SCORING_POLICY_JA.md) | [Español](SCORING_POLICY_ES.md)

# Política de puntuación de atribución

Congelada antes de cualquier comparación de modelos, según el §28 de `BRIEF_2026-07-26_ATTRIBUTION.md`.
Decidir esto después permitiría elegir las reglas para que encajen con un resultado.

Estado: **borrador, pendiente de aprobación del propietario.** Cada punto enuncia una recomendación
y el razonamiento; puedes anular cualquiera de ellas, pero registra la decisión aquí antes de
puntuar.

---

## 1. Habla genuinamente anónima

**Recomendación: excluida del denominador de precisión, informada por separado.**

Una línea dicha por un personaje sin nombre — un grito de batalla, un miembro de la multitud, un
hombre tuerto que nunca se nombra — no tiene respuesta correcta en una clave basada en nombres. Puntuarla
como `AMBIGUOUS` premiaría la abstención; puntuarla como incorrecta penalizaría al
modelo por el corpus.

Se informa como un recuento separado para que el tamaño de la fracción no atribuible siga
visible. Medida en el conjunto existente, es de alrededor del 3-5%.

## 2. Errores de segmentación de `NARRATOR`

**Recomendación: excluidos de la precisión de atribución, informados como defecto de la pasada 1.**

Cuando un juez marca una línea como `NARRATOR`, significa que la pasada 1 dividió narración, un cartel
o un encabezado en una entrada `SPOKEN`. Una línea de referencia resultó ser un cartel en una
pared. Eso es un fallo de segmentación, y contarlo contra la atribución
mide la pasada equivocada y esconde un defecto real detrás de un número de precisión.

El juez 1 encontró 3 en 400 filas muestreadas de grimgar03. Eso es una tasa de defecto de la pasada 1
**muestreada** de ~0.75% en las líneas aptas para juzgar, no la tasa global de error de segmentación
del libro: la muestra excluye narración y líneas repetidas por
construcción, así que no puede dar cuenta de las entradas de las que nunca tomó muestras. Vale la pena seguirlo
por sí mismo, y medirlo bien si alguna vez guía una decisión.

## 3. Normalización de alias y honoríficos

**Recomendación: los alias se declaran por fixture, se comparan sin distinguir mayúsculas,
nunca se infieren.**

Un personaje nombrado de dos formas es un personaje: `RUDI` y `RUDEUS` costaban 14 de 147
líneas antes de que el puntuador entendiera esto. Los alias viven en el fixture, donde un
lector puede comprobarlos contra el texto, no en el código.

Nunca `str.title()`. Pone mayúscula después de cada carácter que no sea letra, convirtiendo `BRI-CHAN`
en `Bri-Chan`, y ha causado tres defectos distintos en este repositorio en un solo
día. La comparación no distingue mayúsculas en todo el proceso.

Un nombre inventado nunca es un alias. `FUTURE_ME` es una respuesta incorrecta, no una forma de
`RUDEUS`, y un test asegura que ningún fixture publicado lo declare.

## 4. Aceptar una respuesta respaldada por el `book`

**Recomendación: el acuerdo de dos jueces es suficiente; el desacuerdo va a
arbitraje.**

Una respuesta cuyo nombre aparece en el libro pero no en la ventana mostrada es
inferencia a partir de un contexto más amplio, que es lectura legítima. Exigir arbitraje
humano explícito para cada fila así costaría más de lo que protege, dado que
dos jueces independientes coincidieron en un 94-97% en rondas anteriores.

Donde los jueces discrepan, decide un humano, que ya es el protocolo.
`support_summary()` informa la división ventana/libro para que el tamaño de esta clase sea
visible por fixture.

## 5. Filas que siguen sin respaldo en el tope de expansión

**Recomendación: excluidas del denominador primario, y contadas.**

La ventana se expande a 40 entradas antes y 20 después. Un hablante cuyo *nombre*
no aparece en unas 60 entradas no puede puntuarse contra una clave basada en nombres
sin que el juez importe conocimiento que la ventana no contiene.

Dicho con cuidado, porque un borrador anterior exageraba: esto **no** significa
que un humano o un modelo no puedan atribuir tales líneas. Las cadenas de pronombres, la estructura
del discurso y la alternancia de turnos pueden identificar a un hablante sin ningún nombre impreso cerca.
Lo que significa es que este fixture no puede arbitrar esas líneas con justicia, así que
se excluyen de *esta* medición en lugar de declararlas imposibles.

Por la misma razón el recuento no es automáticamente el mínimo de la revisión humana. Es
el mínimo de lo que *este benchmark* puede puntuar, que es una afirmación más pequeña.

## 6. Rama de comparación, significación, multiplicidad

**Recomendación:**

- **Brazo primario: `open`** — reparto completo, nombre libre. Es la configuración
  más cercana a producción. `closed-oracle` es un **diagnóstico condicional** — la
  precisión dado que el hablante verdadero está entre cinco candidatos proporcionados — y
  nunca debe citarse como cifra de precisión ni como techo. No limita nada
  fuera de su propia condición; un prompt, una representación o una construcción de candidatos
  distintos podrían moverla.
- **Primero por libro, después agrupado, siempre ambos.** Una puntuación agrupada puede ocultar una
  interacción modelo-por-libro, que es la razón principal para juzgar ambos libros.
- **McNemar exacto emparejado** en líneas idénticas, ya que todos los modelos ven las mismas
  entradas congeladas. Informa los recuentos discordantes, no solo el valor p.
- **Corrección de Holm** entre las comparaciones por pares de modelos dentro de un libro. Seis
  modelos dan 15 pares; sin corrección, se espera aproximadamente un resultado espurio.
- **No significativo significa no resuelto, nunca equivalente.** Afirmar equivalencia
  práctica requiere una pérdida aceptable declarada de antemano y un diseño de
  no inferioridad. Aquí no existe ninguno de los dos.
- **Un resultado en un libro no es una clasificación para toda la biblioteca** a menos que su dirección
  se mantenga en el otro.

---

## Denominador, dicho una vez

```
scored = judged lines
       - anonymous speech            (item 1)
       - NARRATOR segmentation errors (item 2)
       - unsupported at cap           (item 5)
```

Todo informe declara `scored` y cada recuento de exclusiones junto a la precisión, para que
un número nunca pueda leerse sin saber sobre qué se calculó.
