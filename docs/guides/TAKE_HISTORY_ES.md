[English](TAKE_HISTORY.md) | [中文](TAKE_HISTORY_CN.md) | [日本語](TAKE_HISTORY_JA.md) | [Español](TAKE_HISTORY_ES.md)

# Historial de tomas

En el Inspector del Editor, abre **Automatic take history**, elige **Refresh setting**
y luego **Turn on**. El ajuste del proyecto está desactivado por defecto y se aplica a la
regeneración futura. Puedes cambiarlo sin seleccionar una línea.

Con él activado, la interpretación anterior seleccionada se conserva antes de que se genere
o instale un reemplazo. Editar palabras, hablante, instrucción o número de toma mantiene
los metadatos de la interpretación guardada original. Las ediciones repetidas no reetiquetan el audio antiguo
con las palabras nuevas. La primera generación no tiene interpretación anterior que archivar.

Usa los controles de instantáneas del Inspector para escuchar interpretaciones guardadas, proteger una
copia, eliminarla con deshacer o quitarla permanentemente tras confirmación. La vista de archivo
del proyecto también incluye generaciones anteriores del libro. Escuchar no selecciona una
instantánea como audio actual.

En hosts Linux compatibles, una instantánea con evidencia de publicación registrada para el
libro y la línea actuales ofrece **Check & restore**. Guarda las ediciones pendientes antes de usarla.
La tarea de audio comprueba las entradas originales, la voz y la semilla, los ajustes del
modelo, la pronunciación, las ventanas de síntesis, las dependencias de ejecución nativas y los bytes
guardados reales. La comprobación puede cargar el modelo de voz nativo; no sintetiza audio nuevo.
Usa **Cancel audio task** para solicitar la cancelación y espera a que la tarea termine.

Una restauración correcta selecciona los bytes codificados originales, el número de toma y la revisión
del artefacto, conservando sus recibos de generación. Mantiene la interpretación desplazada
en el historial incluso cuando el historial automático está desactivado. No reescribe las palabras, el hablante
ni las instrucciones de interpretación. Una dependencia cambiada o una prueba no disponible provocan un rechazo
y conservan la interpretación seleccionada. Las instantáneas antiguas, otras identidades de libro/línea
y los hosts no compatibles siguen disponibles para escuchar. Las instantáneas protegidas se pueden
restaurar; las eliminadas hay que deshacer su eliminación primero.

El archivo permite 4.096 registros, 16 por línea y 1 GiB de audio en total.
Las copias eliminadas e interrumpidas cuentan hasta que se quitan permanentemente. Una copia que todavía
necesita un reemplazo pendiente o un deshacer de eliminación de fragmento activo no se puede quitar.
Si la admisión falla, libera espacio del archivo o desactiva el historial automático antes de
reintentar la generación. Las copias existentes siguen disponibles cuando está desactivado.
La restauración explícita sigue requiriendo espacio suficiente para conservar la toma desplazada.

Si se cancela la generación o falla la publicación, una interpretación anterior retenida
sigue protegida hasta que un reemplazo tenga éxito. Tras una interrupción del proceso, una
copia incompleta del archivo sigue visible y con su cuota; quita explícitamente esa copia
antes de reintentar. Una copia retenida completa se puede reutilizar en el reintento aunque su antigua
ranura de audio de trabajo haya cambiado. Una política dañada se puede reemplazar refrescando
el ajuste y desactivándolo, sin analizar ni reescribir el archivo.

Activar el historial no puede reconstruir audio ya separado por ediciones anteriores.
Reconstruir fragmentos o cargar otro libro crea una nueva identidad de libro/línea;
la retención automática no transfiere los metadatos de líneas pendientes entre esos cambios.
Las copias completas del archivo siguen siendo visibles en el archivo del proyecto.

Las instantáneas conservan los bytes guardados reales y las entradas registradas. Hacer una instantánea por sí solo
no certifica las dependencias exactas usadas en un render anterior.
Escucha un candidato antes de usarlo en un audiolibro.

Con el historial automático activado, los renders CustomVoice locales integrados que cumplen los requisitos también
registran un recibo de publicación acotado. Esto requiere actualmente semillas deterministas,
ventanas de síntesis internas e identidad de servicio de recuperación cualificada. La entrada de voz
observada exacta se captura de forma independiente del ASR y luego se vincula al hash y la forma
decodificada del archivo codificado terminado. También se vinculan las entradas seleccionadas de voz/modelo, las decisiones
de pronunciación, la política de ventanas, la identidad de libro/línea y la revisión de audio.
Otros renders e instantáneas anteriores siguen sin prueba. Las instantáneas existentes no se
certifican retroactivamente. Un recibo es un candidato a restauración; la acción explícita
debe seguir verificando sus dependencias actuales y su audio.

Los recibos de publicación y los metadatos del predecesor pendiente comparten un presupuesto de 16 MiB por libro.
Cada recibo de publicación ocupa como máximo 4 KiB; los límites inmutables de registro/carga del archivo
siguen aplicándose. El audio válido de primera pasada fuera de los límites del recibo de 64 MiB, mono/estéreo,
192 kHz y diez minutos puede publicarse sin prueba. La generación nueva reemplaza su recibo anterior de forma atómica con la selección
de audio, o lo borra cuando la prueba completa no está disponible. Navegar por el historial
no carga un modelo, no calcula el hash del audio seleccionado ni ejecuta verificación de voz.

## Validación MPEG y disponibilidad del decodificador

Las instantáneas MPEG se comprueban de forma independiente con FFprobe y FFmpeg antes de estar
listas. El decodificador lee el archivo abierto admitido mediante entrada de descriptor con búsqueda,
comprueba el recuento completo de fotogramas PCM y la geometría, y vuelve a comprobar el contenido de origen.
No remuestrea, normaliza ni reemplaza el original archivado. Las instantáneas WAV, FLAC y
OGG conservan su ruta de validación existente; las muestras WAV float finitas fuera del rango
normalizado siguen aceptándose.

Esto requiere compilaciones de FFprobe y FFmpeg que funcionen y admitan entrada `fd` con búsqueda.
Si faltan o no son compatibles, actualiza las herramientas del decodificador. Una captura fallida o cancelada
conserva su copia pendiente y su cargo de cuota; quita explícitamente la
copia incompleta antes de reintentar. La interpretación actual seleccionada se conserva.

El decodificador compartido también protege la conversión de referencias de entrenamiento MPEG en estéreo:
el staging privado de checkpoints recibe una conversión mono completa antes de la publicación,
mientras la referencia original permanece sin cambios. Los tiempos de espera del decodificador detienen y recogen
los procesos propios. La retrollamada de escritura de archivos de un llamador conserva la propiedad hasta que su
escritura termina; una escritura de sistema de archivos bloqueada no se abandona tras un tiempo de espera.

## Metadatos compactos

Las escrituras del archivo pueden guardar una vez el texto repetido, las entradas de generación, la procedencia y la política
de síntesis, direccionados por su hash de contenido. El formato compacto se usa solo
cuando ahorra al menos 4 KiB y el 5% de los bytes del manifiesto. Los recibos de origen completos
y el audio seleccionado conservan su significado. Se aplican los mismos límites de registro, audio, profundidad
y manifiesto completamente expandido de 16 MiB. Esto reduce los metadatos repetidos;
cada guardado sigue validando y reemplazando el manifiesto completo.

Los archivos v1/v2 existentes siguen siendo legibles. Los archivos compactos usan el formato v3, que
las aplicaciones más antiguas se niegan a reescribir. Antes de volver a una aplicación más antigua,
expande los metadatos con la aplicación actual. Desde `app/`, con el
entorno Python de la aplicación activo:

```sh
python -m audio_take_history /path/to/project-data --expand-metadata
python -m audio_take_history /path/to/project-data --compact-metadata
```

Usa el directorio de datos que contiene `take_history/`. Ambos comandos toman el bloqueo del archivo
y usan su límite existente de archivo pendiente/reemplazo atómico. La conversión
conserva la valla del escritor v1/v2 original, las instantáneas, los recibos y la selección de audio
actual. Una escritura posterior del archivo puede volver a compactar los metadatos expandidos. Las cargas referenciadas
faltantes, cambiadas o inválidas rechazan la conversión; conserva el archivo
para inspección en lugar de editar las referencias a mano. Quitar una instantánea descarta
solo las cargas que ya no usa ningún registro retenido, eliminado o incompleto.
