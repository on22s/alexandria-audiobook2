# Tipos de voz

[English](Voice-Types.md) · [日本語](Voice-Types-JA.md) · [中文](Voice-Types-CN.md) · Español

Cada hablante de un guion recibe una tarjeta de voz en la pestaña Voices. Esta página explica los tipos de voz
que puedes elegir y cuándo usar cada uno.

## Personas de voz para estados de personaje

La generación avanzada de personas globales crea una persona y una vista previa separadas para cada estado de edad/género asentado de un personaje. Cada estado usa su propio diálogo y la narración cercana, conservando los índices de evidencia del guion original. Los estados repetidos más adelante en la historia tienen tarjetas numeradas separadas.

En Voices, usa **Show cards** para mostrar todos los personajes, los personajes con tarjetas de estado o los personajes sin tarjetas de estado. Las tarjetas de estado tienen tipos de voz, audio/transcripciones de referencia, descripciones de persona, estilos, semillas, aprobaciones y pools de candidatos independientes. Genera una persona de estado que falte o esté obsoleta antes de editar su voz. Una persona de estado solo se queda obsoleta cuando cambia su propia parte del libro (las líneas del personaje o la narración del segmento de ese estado); las ediciones en otras partes del libro la dejan actual. La voz base del personaje y las versiones manuales se quedan en la sección expandible de voz base del personaje.

**Only characters without a voice yet** también genera las personas de estado que falten o estén obsoletas en modo avanzado. Regenerate persona en una tarjeta de estado se dirige solo a ese estado. Los fallos se informan por estado; los estados completados con éxito se guardan, y los ajustes anteriores de los estados fallidos se conservan.

La generación y la edición no aplican cambios de voz al audio renderizado. Abre los controles **Voice changes** del personaje en su sección base, revisa las versiones de estado sugeridas y pulsa **Apply**. El renderizado futuro usa esas versiones en sus límites de línea de tiempo. Un cambio dentro de un fragmento combinado, un diálogo editado que ya no se alinea o un guion desactualizado requieren reconstruir/revisar los fragmentos antes de aplicarlo; ese estado se omite con un aviso y los demás estados del personaje se siguen aplicando. Una voz de estado se aplica solo a través de esta línea de tiempo, nunca se elige como versión del personaje para todo el libro. Aplicar, editar o borrar una línea de tiempo de voz marca como pendientes solo las líneas cuya voz efectiva cambia. Usa **Render Pending** en el Editor para reemplazarlas. Los archivos de audio anteriores se quedan en disco, pero las líneas pendientes se desvinculan de las exportaciones hasta que un nuevo renderizado tenga éxito.

Un cambio de género conocido sin un salto grande de edad se marca como **Possible identity reveal** para revisión. Las etiquetas se quedan como están escritas en el guion. Si el personaje debería sonar igual en todo el libro, define su voz base, elige **Main voice throughout** y luego **Apply**; elegir solo no guarda nada. Mantén voces de estado separadas cuando el cambio sea intencionado.

Las voces guardadas del Designer con etiquetas de edad conocidas, audio de referencia reproducible y una transcripción de referencia también se ofrecen en **Voice changes** junto a las voces de la biblioteca LoRA. Se aplican como referencias de clonación. Las etiquetas de edad/género guían las sugerencias; los rangos infantiles declarados exactos se clasifican primero, y las voces usadas por otros personajes se muestran como reutilizadas. Las referencias ausentes o corruptas quedan excluidas. Para usar una como voz base de un personaje, selecciona su referencia guardada del Designer en **Voice Clone**.

Usa **Remove saved state persona** para quitar una versión de estado no aplicada, actual u obsoleta, conservando sus archivos de audio y otras voces. Borra primero sus puntos de línea de tiempo aplicados si la reproducción todavía la usa. Guardar un reparto en la biblioteca de voces conserva las versiones manuales pero nunca lleva versiones de estado ni la línea de tiempo de voz a otro libro.

Usa **Recover persona** en una tarjeta de estado para abrir la recuperación manual con ese estado exacto seleccionado. Cuando introduzcas un personaje tú mismo en el panel de recuperación, elige explícitamente su persona base o su estado numerado. La recuperación de estado y Resume conservan la base y los demás estados; usa solo el diálogo/contexto del estado seleccionado para el prompt copiado.


## Los modelos de voz

La app usa tres modelos Qwen3-TTS:

| modelo | ID | se usa para |
|---|---|---|
| **CustomVoice** | `Qwen3-TTS-12Hz-1.7B` | 9 voces integradas que siguen instrucciones de interpretación |
| **Base** | `Qwen3-TTS-12Hz-1.7B-Base` | clonar una voz a partir de audio de referencia, y voces LoRA |
| **VoiceDesign** | `Qwen3-TTS-12Hz-1.7B-VoiceDesign` | crear una voz a partir de una descripción de texto |

Los modelos se cargan cuando se necesitan por primera vez y se quedan en caché. Solo se carga en la memoria de GPU
el modelo que necesita el tipo de voz actual.

## Los tipos de voz de un vistazo

| tipo en la pestaña Voices | modelo | mejor para | ¿por lotes? |
|---|---|---|---|
| **Custom Voice** | CustomVoice | el narrador y los personajes principales | sí |
| **Built-in Voice** | Base + un LoRA incluido | voces entrenadas listas para usar, sin entrenamiento | sí |
| **Voice Clone** | Base | un personaje que necesita una voz concreta | sí |
| **LoRA Voice** | Base + tu LoRA | personajes recurrentes que necesitan una voz única y duradera | sí |
| **Voice Design** | VoiceDesign | personajes secundarios con 1-3 líneas | no: una línea a la vez |
| **Together** | las voces propias de los miembros | un grupo hablando a la vez | — |

## Elegir un tipo

```
¿Es un personaje principal con muchas líneas?
├─ Sí → ¿Necesitas una voz distinta de las 9 integradas?
│       ├─ Sí → LoRA Voice (entrena una) o Built-in Voice (usa una incluida)
│       └─ No → Custom Voice
└─ No → ¿El personaje tiene más de 3–4 líneas?
        ├─ Sí → Voice Clone (guarda una voz diseñada y úsala como referencia)
        └─ No → Voice Design (no hace falta configurar nada)
```

## Ancla de identidad y línea de tiempo de estilo

Cada tarjeta de voz tiene un campo **Character Style**. Es el **ancla de identidad** de la voz: una
descripción fija (por ejemplo "Heavy Scottish accent" o "Refined aristocratic tone") que se
envía con cada línea, para que las instrucciones de interpretación por línea no puedan cambiar a quién
suena el personaje.

- En las voces **Custom** el ancla va **primero** y la instrucción propia de la línea va después:
  `"Heavy Scottish accent Excited, bright energy."`. Medido en 120 líneas reales, esto redujo la
  deriva de tono del narrador de 3.49 a 2.53 semitonos y mantuvo la voz más cerca de su propia
  apertura (ECAPA 0.737 → 0.771).
- En las voces **LoRA** y **Built-in** el estilo del personaje se añade **después** de la
  instrucción de la línea: `"Excited, bright energy. Heavy Scottish accent"`.

La **línea de tiempo de estilo** permite que un personaje cambie a partir de una línea dada, "mayor tras el
salto temporal", sin convertirse en otro hablante. Cada punto de cambio fija un ancla nueva desde
esa línea en adelante.

## Generate Personas (asignar voces automáticamente)

En lugar de configurar cada hablante a mano, haz clic en **Generate Personas**:

1. El modelo de lenguaje lee líneas de muestra de cada hablante.
2. Escribe una descripción de voz ("A gruff middle-aged man with a deep, gravelly voice") y
   una frase de muestra.
3. El modelo VoiceDesign renderiza el audio de referencia a partir de cada descripción.
4. A cada hablante se le asigna ese audio como **Voice Clone**.

Opciones:

- **Scope**: por defecto solo los personajes *sin voz todavía*; puedes elegir todos los personajes.
- **Save the current voices to the library first**: conserva lo que ya tienes.
- **Advanced**: fija el tamaño de lote (40 por defecto), para guiones con repartos muy grandes.

Notas:

- Las voces de persona son voces de clonación: mantienen una identidad consistente pero un tono neutro, porque
  las voces de clonación no siguen instrucciones de interpretación.
- Para voces más expresivas, empieza con personas y luego cambia los personajes clave a LoRA o
  Custom.
- El audio de referencia generado se guarda en `designed_voices/` y se puede reutilizar.

**Suggest LoRA Voices** hace algo parecido con voces entrenadas: empareja las voces LoRA
que tienes con tus personajes.

## Alias

El desplegable **Alias of** de una tarjeta de voz hace que un hablante use la voz de otro hablante.

Úsalo para:

- variantes de nombre: "DR. SMITH" → "SMITH";
- variantes de edad: "YOUNG ELENA" → "ELENA";
- títulos: "KING MARCUS" → "MARCUS";
- reducir el número de voces que hay que configurar.

Cómo funciona:

- Cuando se genera audio, un hablante con alias usa la voz del hablante objetivo.
- Los alias encadenan: si A → B → C, el hablante A usa la voz de C.
- Un bucle (A → B → A) se detecta, se registra como advertencia y se detiene.
- El alias se guarda en `voice_config.json` como el campo `alias_of` del hablante.

## Custom Voice

**Modelo:** CustomVoice. **Mejor para:** el narrador y los personajes principales.

Elige una de las 9 voces integradas, con control total sobre la emoción y la interpretación mediante la
instrucción de la línea.

- **Voces:** Aiden, Dylan, Eric, Ono_anna, Ryan, Serena, Sohee, Uncle_fu, Vivian.
  - La mayoría no son hablantes nativos de inglés; Ryan y Aiden sí.
- **Ajustes:** la voz, y el **Character Style** (el ancla de identidad).
- **Lotes:** sí: las líneas se agrupan por lotes y se dividen en sublotes por longitud.

## Built-in Voice

**Modelo:** Base con un adaptador LoRA incluido. **Mejor para:** una voz lista para usar sin entrenamiento.

El repositorio incluye un conjunto de voces entrenadas en `builtin_lora/`. Se descargan de Hugging
Face la primera vez que usas una, y se comportan como voces LoRA.

## Voice Clone

**Modelo:** Base. **Mejor para:** un personaje que necesita una voz concreta.

Clona una voz a partir de una grabación de referencia de 5-15 segundos.

- **Ajustes:**
  - **Designed Voice**: elige una voz guardada en la pestaña Designer (rellena la ruta del audio); o
  - **Custom Path**: la ruta a cualquier archivo WAV;
  - **Reference Text**: la transcripción exacta del audio de referencia.
- **Consejos:**
  - Usa audio limpio sin ruido de fondo.
  - Las referencias de menos de unos 7 segundos dan una voz inestable; la comprobación de importación
    las normaliza y mide.
  - La pestaña Designer puede crear audio de referencia a partir de una descripción de texto.
  - Las referencias subidas registran su transcripción, origen y base de derechos.
- **Las instrucciones de interpretación se ignoran** en las voces de clonación: la voz viene por completo de la
  referencia.
- **Lotes:** sí: las líneas se agrupan por hablante y se procesan juntas.

Las voces de clonación locales reutilizan la preparación de la referencia cuando las etiquetas de hablante o las rutas de archivo
apuntan a audio idéntico y a la misma transcripción de referencia. El audio se comprueba
de nuevo antes de reutilizarlo; los bytes de referencia cambiados, la transcripción o un modelo residente reconstruido
rehacen el trabajo afectado. El almacenamiento de caché está acotado, y las referencias desalojadas se preparan
de nuevo automáticamente. Descargar el modelo de clonación borra sus cachés de referencia.

Los diagnósticos recientes del TTS incluyen aciertos, fallos de caché (misses), desalojos y bytes contabilizados. Estos recuentos describen las cargas de caché retenidas; no miden la memoria total
del proceso ni de la GPU.

## LoRA Voice

**Modelo:** Base con un adaptador LoRA. **Mejor para:** personajes recurrentes que necesitan una voz única
y duradera que siga las instrucciones de interpretación.

Un adaptador LoRA afina el modelo Base con un pequeño conjunto de muestras de voz. El resultado es más
consistente que un clon, y responde a las instrucciones de emoción e interpretación.

- **Ajustes:** el adaptador (de la pestaña Training, o uno integrado), y el **Character
  Style**.
- **Cómo funciona:**
  1. Entrena un adaptador con tus muestras de voz (ver la [Guía de entrenamiento](Training-Guide-ES.md)).
  2. En el momento de la generación, el adaptador se carga encima del modelo Base.
  3. Un clip de referencia del entrenamiento aporta el prompt de voz.
  4. El ancla más la instrucción de la línea controlan la interpretación.
- **¿Clone o LoRA?**
  - **Clone**: rápido, sin entrenamiento; la voz viene de un clip de referencia.
  - **LoRA**: más consistente en muchas líneas y sigue la dirección emocional, pero necesita
    entrenamiento.
- **Lotes:** sí: las líneas se agrupan por adaptador; el grupo de cada adaptador se procesa por su cuenta.

## Voice Design

**Modelo:** VoiceDesign. **Mejor para:** personajes secundarios con 1-3 líneas.

Crea una voz al vuelo a partir de una descripción de texto. **Cada línea recibe su propia voz generada al momento**,
así que no hay identidad duradera entre líneas.

- **Ajuste:** **Description**: la voz base del personaje ("Young strong soldier", "Old
  gravelly bartender").
- **Cómo se combinan la descripción y la instrucción de la línea:**
  - descripción: `"Young strong soldier"`;
  - instrucción de línea: `"Shouting in rising desperation, with hints of panic"`;
  - enviado al modelo: `"Young strong soldier, Shouting in rising desperation, with hints of panic"`.
- **Consejos:**
  - Mantén la descripción sobre la voz en sí (edad, género, calidad vocal), y deja
    la emoción y la interpretación a las instrucciones.
  - Evita contradicciones como "calm gentle voice" con "screaming furiously": pueden
    causar glitches de audio.
  - Para un personaje con más de 3-4 líneas, guarda una voz diseñada y úsala como clon.
- **Lotes:** no: cada línea puede tener una descripción distinta, así que estas líneas se renderizan
  una a una después de todos los tipos por lotes.

## Together

Para una línea dicha por varios personajes a la vez. Cada miembro dice la línea con la voz que
ya tiene, y los clips se mezclan. Los clips se alinean con el más largo, así que
el resultado suena como un coro en lugar de una unión exacta.

## ¿Qué tan consistente es cada tipo?

| tipo | consistencia | por qué |
|---|---|---|
| Custom | alta | la misma voz integrada cada vez (con una semilla) |
| Clone | alta | el mismo audio de referencia da la misma voz |
| LoRA | alta | una identidad entrenada, estable entre líneas |
| Voice Design | de baja a media | la misma descripción mantiene voces parecidas, no idénticas |

En Voice Design, la consistencia depende de lo parecidas que sean la descripción combinada y la instrucción
de una línea a otra. Si la descripción se mantiene y solo cambia la emoción, las
voces suenan lo bastante parecidas como para no chirriar.

**Check Voices** del Editor lo mide directamente: compara cada línea renderizada con la referencia
de su hablante y marca las líneas que se desviaron.
