[English](BOOK_SWITCH_RECOVERY.md) | [中文](BOOK_SWITCH_RECOVERY_CN.md) | [日本語](BOOK_SWITCH_RECOVERY_JA.md) | [Español](BOOK_SWITCH_RECOVERY_ES.md)

# Guardados y cambios de libro interrumpidos

Cargar un libro guardado, o guardar el activo, reemplaza varios archivos a la vez:
el guion, los ajustes de voz, los archivos de capítulo, etc. La app hace esto como una transacción con diario (journal) para que un fallo, un corte de energía o un disco lleno nunca dejen
un libro mitad viejo y mitad nuevo. Esta guía cubre qué pasa tras esa
interrupción y qué hacer cuando la app no puede terminar el trabajo sola.

## Qué ocurre automáticamente

Antes de cada operación del libro que lo lee o lo cambia, la app busca un
cambio interrumpido: un diario llamado `.active_book_transaction.json` en la
carpeta de datos, con su carpeta de trabajo `.book-switch-<id>/` al lado. (La lectura
de estado de la página Home es la excepción: no recupera, así que puede mostrar un
cambio que `finish` todavía completaría.)

- **La recuperación termina o revierte el cambio** cuando cada archivo está donde el
  diario dice, con los bytes que registró. Es el caso habitual y no ves
  nada.
- **La cuarentena automática** se aplica cuando todos los archivos del libro siguen coincidiendo con el diario pero
  la carpeta de trabajo no: cambió un archivo preparado, apareció un archivo desconocido,
  o una limpieza se detuvo a medias. La carpeta de trabajo y el diario se mueven
  a `.book-switch-quarantine-<journal hash>-<nonce>/` y el libro se abre.
  No se borra nada.
- **En cualquier otro caso se rechaza el cambio** y el libro no se abre. Esto ocurre
  cuando cambió un archivo del libro, se intercambió una carpeta o el diario está
  dañado. La app no adivinará qué versión es la correcta.

Cuando un libro no se puede abrir por este motivo, la página Home nombra los comandos
de abajo, en el orden en que hay que probarlos: `inspect`, luego `finish`, y solo si `finish`
se niega, `quarantine --accept-current-files`.

## Restos de un fallo

Un fallo puede dejar dos cosas que ningún diario reclama:

- `.book-switch-<id>/`: la carpeta de trabajo de un cambio que murió antes de que se
  publicara su diario. Puede contener una copia preparada completa del libro.
- `.active_book_transaction.json.new-<id>`: un temporal de publicación del diario
  que se escribió pero nunca llegó a ser el diario.

`inspect` los lista como `orphaned_staging` y `journal_temporaries`, con
su recuento de entradas, bytes y marca de tiempo más reciente. El resumen de recuperación informa
solo los recuentos y los bytes totales (`leftovers`), nunca nombres ni rutas, y
informa `leftovers: null` mientras la app está ocupada (puede estar preparando algo ahora mismo).
Un nombre que solo se parece, un enlace o la carpeta de trabajo que nombra el propio diario
pendiente nunca se informan como huérfano.

La app nunca borra estos restos: una carpeta anterior al diario no lleva prueba
de que sea nuestra, así que liberar ese espacio (o no) es tu decisión. Después de comprobar que
el libro se abre y que la carpeta no es un cambio activo, bórrala tú mismo, como
harías con una carpeta separada. `inspect --no-lock` puede mostrar una entrada que en realidad es
staging de un cambio activo; vuelve a ejecutar `inspect` con el bloqueo antes de borrarla.

## Qué deja un fallo

Un cambio modifica los archivos del libro en un orden fijo. En Linux y macOS (donde
el sistema de archivos puede intercambiar dos nombres en un solo paso, el diario "v4"):

1. prepara cada archivo nuevo en una carpeta privada `.book-switch-<id>/`,
2. escribe el diario `.active_book_transaction.json` (fase `publishing`),
3. intercambia cada archivo preparado a su sitio, uno a la vez,
4. marca el diario como `committed`,
5. borra la carpeta de preparación y luego el diario.

Toda operación del libro ejecuta primero la recuperación (`recover_book_state_locked` en
`app/book_state_transaction.py`). Esto es lo que encuentras tras un fallo en cada
punto, y lo que pasa la próxima vez que se abre el libro. La función entre corchetes es
la que maneja ese caso.

**Antes de que exista el diario (durante el paso 1 o 2).**
- En disco: una carpeta `.book-switch-<id>/` y, posiblemente, un
  `.active_book_transaction.json.new-<id>` a medio escribir. Todos los archivos del libro están intactos.
- Siguiente apertura: no hay nada que deshacer, así que el libro se abre tal como estaba. `inspect` lista la
  carpeta bajo `orphaned_staging` y el archivo a medio escribir bajo
  `journal_temporaries`; la página Home muestra su recuento y tamaño. No se
  borra nada por ti. [`recover_book_state_locked`, `_get_leftover_staging_entries`]
- Tú: cuando estés seguro de que no hay ningún cambio en marcha, bórralos a mano si quieres
  recuperar el espacio.

**Después del diario, antes de intercambiar ningún archivo.**
- En disco: el diario dice `publishing`; cada archivo del libro sigue siendo el antiguo.
- Siguiente apertura: cada archivo se comprueba contra el diario, se eliminan la carpeta de preparación y
  el diario, y el libro se abre sin cambios. [`_recover_book_exchange_journal`]
- Tú: nada.

**A mitad de los intercambios (paso 3).**
- En disco: algunos archivos son nuevos y otros viejos. La versión antigua de cada archivo intercambiado (o,
  para un archivo recién creado, un marcador de posición de 64 bytes) está en la carpeta de preparación.
  Un archivo que se está creando puede mostrar brevemente ese marcador en su nombre real.
- Siguiente apertura: se revierte cada intercambio, tras comprobar de nuevo
  ambos lados, así que el libro vuelve exactamente a como estaba antes del cambio. Los archivos que
  el cambio estaba creando desaparecen otra vez. [`_recover_book_exchange_journal`, `_undo_book_exchange`]
- Tú: nada.

**Todos los archivos intercambiados, pero el diario aún no está `committed`.**
- En disco: cada archivo del libro es nuevo; la carpeta de preparación guarda las versiones antiguas.
- Siguiente apertura: se trata igual que el caso anterior. El cambio se revierte
  y el libro se abre con sus archivos antiguos, porque hasta que se escribe `committed`
  nada prometía que el cambio hubiera terminado. La única excepción es un diario en el que
  cada fila es un reemplazo, cada archivo demuestra su contenido nuevo y no queda ninguna copia
  antigua que restaurar: ese se completa hacia el estado nuevo. Una mezcla de archivos terminados
  y sin terminar que no se pueden restaurar se rechaza en lugar de abrirse como
  un libro mitad viejo y mitad nuevo. [`_recover_book_exchange_journal`]
- Tú: nada, salvo que se niegue ("cannot be completed consistently"). Entonces
  ejecuta `inspect`, y `quarantine --accept-current-files` conserva los archivos como están
  ahora.

**Después de `committed`, durante la limpieza (paso 5).**
- En disco: el diario dice `committed`; cada archivo del libro es nuevo; puede quedar
  parte de la carpeta de preparación.
- Siguiente apertura: cada archivo del libro se comprueba contra su contenido nuevo, luego solo
  se borra lo que creó el propio cambio y se elimina el diario. Todo lo que
  haya en la carpeta de preparación que no se pueda probar que sea nuestro se mueve, junto con
  el diario, a `.book-switch-quarantine-<hash>-<nonce>/`, y el libro
  se abre igualmente con los archivos nuevos. [`_recover_book_exchange_journal`, `_quarantine_book_transaction`]
- Tú: nada. Borra la carpeta de cuarentena cuando ya no quieras la evidencia.

**Durante una cuarentena (su `receipt.json` todavía dice `prepared`).**
- En disco: la carpeta de cuarentena, con la carpeta de preparación y el diario movidos
  a ella o todavía en la raíz del libro.
- Siguiente apertura: si el diario sigue en la raíz, la cuarentena se termina,
  pero solo si todo sigue coincidiendo con lo que registró el recibo; si no,
  la recuperación se niega y conserva todo. Si el diario ya se movió, el
  libro se abre, y el recibo se marca `completed` una vez que se vuelven a comprobar el tamaño y el hash del diario movido
  y la identidad de la carpeta de preparación movida. Un
  recibo que falla esa comprobación sigue `prepared` y no se mueve nada.
  [`_quarantine_book_transaction`, `_complete_interrupted_quarantines`]
- Tú: nada en el caso habitual. Un recibo que queda en `prepared` es evidencia de que
  algo cambió; `inspect` lo muestra.

**Otro programa cambió un archivo del libro mientras el cambio estaba en marcha.**
En cualquier paso posterior al diario, si un archivo del libro no es ni la versión antigua ni
la nueva del cambio, la recuperación se niega: no intercambia, borra ni aparta un archivo
que no escribió. El libro sigue cerrado hasta que decidas, con `inspect` y
luego `quarantine --accept-current-files`. (Una excepción: un cambio de estilo Windows
que falla antes de mover ninguno de sus archivos y encuentra el cambio de otro programa
mientras se rinde, elimina su diario igualmente. Nada del cambio
se aplicó, el cambio del otro programa permanece, y el log de la app recibe un aviso
que empieza por "Book switch ... was retired without recording recovered".)

**Windows y sistemas de archivos que no pueden intercambiar nombres (el diario "v3").**
Estos publican cada archivo con dos renombrados: el archivo antiguo a `old-<i>` en la
carpeta de preparación, y luego el nuevo a su sitio. No hay marcadores de posición, así que un
fallo entre los dos renombrados deja el archivo del libro brevemente *ausente*. La recuperación
sigue las mismas reglas que arriba: comprueba el archivo antiguo guardado, mueve cualquier
archivo nuevo instalado a `discard-<i>` y devuelve el antiguo a su sitio, o
se completa hacia el estado nuevo si el diario ya dice `committed`.
[`_recover_book_journal`; `apply_book_state_locked` elige v3 siempre que el intercambio
no esté disponible, lo que incluye todos los cambios en Windows.]

Todos estos siguen una regla, de #1816: la recuperación nunca renombra sobre un nombre
del que ya no puede probar que es suyo. Cuando no puede decir qué archivo es nuestro, conserva
el diario, la carpeta de preparación y ambos archivos, y se niega a abrir el libro
en lugar de adivinar.

## Lectores durante un cambio

Mientras se crea o elimina un archivo en Linux o macOS, su nombre puede
contener brevemente un marcador de posición de 64 bytes: el texto `alexandria-book-placeholder-v1 `,
un id aleatorio y un salto de línea (`app/book_placeholder.py`). Nunca es contenido
real. La mayor parte de la app lee el libro solo después de tomar el bloqueo del libro
(`ensure_book_state`), que termina o rechaza primero cualquier cambio pendiente, así que
nunca ve un marcador de posición. Los pocos lectores que se ejecutan sin el bloqueo lo comprueban:

- **Las descargas y exportaciones** responden 409 `SWITCH_IN_PROGRESS` ("A book switch is
  in progress. Wait a moment and download again.") en lugar de servir el
  marcador de posición. La ruta comprueba el nombre (`_require_export_download`,
  `app/routers/editor.py`), y la respuesta comprueba el archivo que realmente abrió
  (`ExportFileResponse`, `app/export_download.py`), así que un cambio que empieza en
  medio sigue detectándose. Las descargas ZIP omiten los archivos de marcador de posición y responden 409
  si todos los archivos lo eran (`build_zip_download`).
- **Las lecturas de fragmentos** (`ProjectManager._read_chunks`, `app/project.py`) se niegan con
  "A book switch is in progress; chunks are not available yet". Nunca analizan
  el marcador de posición, lo renombran como corrupto ni regeneran sobre él, y comprueban
  los bytes que leen en lugar del nombre, así que un intercambio intermedio también se detecta.
- **Las exportaciones de ámbito** tratan un `state.json` de marcador de posición como "todavía sin inventario"
  (`is_scope_export_historical`, `app/project_scope_exports.py`).

## El comando de recuperación

Ejecútalo en una terminal desde la carpeta de Alexandria, con el Python de la app (bajo
Pinokio, el entorno en `app/env`):

```
python app/book_state_recovery.py inspect --hash
python app/book_state_recovery.py finish
python app/book_state_recovery.py quarantine --accept-current-files
python app/book_state_recovery.py migrate-legacy --trust-journal-hashes
```

| Comando | Qué hace |
|---|---|
| `inspect` | Informe de solo lectura: el diario pendiente, el estado esperado y actual de cada archivo, la carpeta de trabajo, cada carpeta separada con su recibo y cualquier resto de fallo (`orphaned_staging`, `journal_temporaries`). Añade `--hash` para calcular el hash de cada archivo e informar si contiene los bytes `old` del diario, sus bytes `new` o `neither`. Las marcas de tiempo por sí solas pueden pasar por alto una reescritura del mismo tamaño: las unidades FAT/exFAT guardan tiempos de 2 segundos. `--no-lock` lee mientras la app está ocupada, como una instantánea de mejor esfuerzo; una entrada que muestre puede ser staging de un cambio activo. |
| `finish` | Ejecuta ahora la misma recuperación que ejecuta la app. |
| `quarantine --accept-current-files` | Conserva los archivos del libro exactamente como están ahora, aunque no coincidan con el diario. La carpeta de trabajo y el diario se apartan, no se borra nada y el libro se abre. Úsalo después de que `inspect` muestre que los archivos actuales son los que quieres. |
| `migrate-legacy --trust-journal-hashes` | Para un diario de una versión anterior de la app (v1), que registraba el hash de cada archivo pero no su identidad. Con el flag, se confía en un archivo que contiene el hash registrado por su fase. Cuando todos los archivos coinciden, la carpeta de trabajo y el diario antiguo se mueven a `.book-switch-migration-<hash>-<nonce>/` y el libro se abre. Un guardado interrumpido a mitad de publicación no coincide con ninguno de los dos estados: cada archivo no coincidente se lista y no se mueve nada. |

Opciones comunes: `--json` imprime un solo objeto JSON, `--data-dir DIR` nombra la
carpeta de datos (por defecto la de la propia app: `ALEXANDRIA_DATA_DIR`, si no, la
carpeta de Alexandria), y `--wait SECONDS` espera a que termine otra operación del libro.
Sin `--wait`, una app ocupada se informa de inmediato en lugar de quedarse colgada.

| Código de salida | Significado |
|---|---|
| 0 | Hecho, o no había nada pendiente. |
| 1 | Error del sistema, incluida una escritura de diario incierta (evidencia conservada; reintentar). |
| 2 | Error de uso, por ejemplo un flag de confirmación ausente. |
| 3 | Rechazado; el mensaje dice por qué y no se movió nada. También se devuelve cuando `inspect` no puede leer el diario. |
| 4 | Ocupado: otra operación del libro tiene el bloqueo. |
| 5 | Hay un diario v1 heredado pendiente; usa `migrate-legacy`. |

## Carpetas apartadas

Cada carpeta `.book-switch-quarantine-*` o `.book-switch-migration-*` contiene:

- `receipt.json`: `prepared` mientras el movimiento está en curso, `completed` después.
  También registra el hash y la fase del diario y el motivo del movimiento:
  `forced` es true para una cuarentena de operador, y una migración añade
  `kind: legacy-migration` y `trust: journal-digests`.
- `journal.json`: el propio diario, movido en lugar de copiado, así que es
  idéntico byte a byte.
- `workspace/`: la carpeta de trabajo, movida, cuando existía.

La app nunca borra estas carpetas. Bórralas tú cuando ya no
necesites la evidencia; `inspect` las lista todas. Si un movimiento se interrumpió mientras
el diario seguía en la carpeta de datos, la siguiente recuperación (cuarentena) o el
mismo comando (cuarentena de operador, migración) lo termina. Si se detuvo después de
mover el diario, la siguiente operación del libro (o `finish`) completa el recibo,
pero solo tras volver a comprobar que `journal.json` sigue teniendo el tamaño y el hash exactos del recibo
y que `workspace/` sigue siendo la carpeta que nombra el recibo. Si
eso no se puede probar, el recibo sigue `prepared`, no se mueve nada y
`inspect` muestra `journal_held: true` para él.

## Límite de confianza

Los recibos no están firmados. Cualquiera que pueda escribir en la carpeta de datos ya puede
cambiar los archivos del libro, así que las herramientas confían en esa carpeta. El flag `forced` de un recibo
se respeta solo una vez que su carpeta de trabajo se ha movido realmente, así que un
recibo escrito a mano no puede convertir la recuperación ordinaria en una cuarentena forzada.
