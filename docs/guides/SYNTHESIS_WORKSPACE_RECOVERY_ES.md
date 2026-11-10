[English](SYNTHESIS_WORKSPACE_RECOVERY.md) | [中文](SYNTHESIS_WORKSPACE_RECOVERY_CN.md) | [日本語](SYNTHESIS_WORKSPACE_RECOVERY_JA.md) | [Español](SYNTHESIS_WORKSPACE_RECOVERY_ES.md)

# Recuperación de espacios de trabajo de síntesis desechables

Las ventanas de síntesis locales propias en Linux ahora registran sus WAV de trabajo antes de
escribir audio. Las categorías participantes son custom, clone, design, LoRA y
ensemble. Cuando un proceso muere, el siguiente inicio inactivo de la aplicación
o la admisión de una tarea de audio recupera los archivos desechables verificados. Esto también
se aplica cuando la recuperación de segmentos reutilizables está desactivada. Por tanto, un reintento puede eliminar
copias de trabajo abandonadas sin borrar tomas guardadas ni evidencia de caché reutilizable.

Para las rutas locales de Project participantes, el WAV padre sin procesar también tiene su propio
registro, creado antes de asignar el archivo y conservado
durante la codificación y la publicación final de la toma seleccionada. Las ventanas internas usan un espacio de trabajo separado;
terminar esas ventanas no retira el padre sin procesar. La propiedad del padre sin procesar es
independiente de las ventanas de síntesis opcionales y de la recuperación reutilizable. Los renders por defecto
con ambas funciones desactivadas siguen capturando la tarea activa, el libro, el UID y la autoridad de entrada
necesarios para la limpieza de desechables. Sus políticas de caché reutilizable, captura de entrada de voz y
prueba de publicación no cambian; la propiedad por sí sola no habilita ninguna de
esas funciones opcionales.

Para el codificador de fragmentos original de Project, el mismo registro del padre también posee
el intermedio codificado y su scratch del codificador. No asigna otro
espacio de trabajo ni reduce el presupuesto de padres paralelos. La codificación MP3 y el respaldo WAV
permanecen dentro de ese espacio de trabajo hasta que la publicación existente de la ranura seleccionada mueve
el resultado a `voicelines`. Por tanto, una terminación abrupta antes de la publicación deja
scratch propio en lugar de un archivo `.chunk-*` o `.render-*` sin registrar.
El codificador lee el padre sin procesar mediante un descriptor comprobado y sin seguimiento.
Sus archivos PCM, de salida y de error se crean en exclusiva; el hijo comprueba sus
identidades de inodo registradas antes de leerlos o truncarlos. El respaldo WAV
también verifica que su nombre de archivo completado siga nombrando el archivo que escribió.
El inodo completado permanece abierto durante la publicación de la ranura seleccionada. Ambos
renombrados usan directorios de origen y destino fijados, y el destino seleccionado debe nombrar
ese mismo inodo antes de cambiar los metadatos. Un reemplazo se conserva como evidencia
y no puede convertirse en una toma correcta solo porque se decodifique como audio válido.
El hijo confirma la versión de su salida ya volcada (flush) en el archivo de metadatos del codificador
acotado existente. El respaldo WAV verifica los bytes copiados mediante descriptores
retenidos, en bloques cancelables fuera de los bloqueos de publicación. Las importaciones
relativas se congelan antes de que el hijo entre en el scratch fijado; su cwd de lanzamiento se
captura antes del arranque para que un cambio posterior de cwd no pueda redirigir esas importaciones.
Los lectores de fragmentos, codificadores o ayudantes de publicación de archivos sobrescritos conservan su comportamiento
de staging existente; no reciben esta autoridad de codificación adicional.

La misma autoridad capturada también cubre las salidas por lotes locales participantes de Project
para custom, clone, design, LoRA y ensemble. Cada fila participante tiene un espacio de trabajo padre separado asignado
antes del despacho y conservado durante todos los finalizadores del lote. El procesamiento por lotes nativo de listas, la
clasificación por longitud, el subprocesamiento por lotes y la reducción de tamaño por OOM permanecen sin cambios. Las rutas
internas vinculan el directorio del proyecto, el índice y la reserva activa (claim) a la ruta registrada;
la captura de entrada de voz usa esa misma ruta. Las filas no mapeadas conservan sus rutas de salida
normales. Los escritores de lotes sobrescritos no reciben rutas de salida registradas.

El registro es `.synthesis-workspaces-v1/registry.json` bajo la raíz de datos del proyecto.
Guarda IDs de espacio de trabajo opacos, procedencia de la reserva y del libro con hash, identidades de directorio
y el recuento de ventanas declarado. No contiene texto de origen. La limpieza
usa los arrendamientos de tareas del kernel existentes y el bloqueo del libro; una tarea activa o un libro ocupado
posponen el mantenimiento. La cancelación normal limpia el espacio de trabajo desechable del mismo propietario,
mientras que un propietario revocado lo deja para una operación inactiva posterior.

La caché separada `.synthesis-recovery-v1` guarda segmentos verificados reutilizables;
vaciar esa caché no autoriza a borrar otros archivos. La publicación del padre completado
y la adjunción de la toma guardada conservan sus comprobaciones de propiedad existentes.
Un fallo de limpieza del espacio de trabajo se registra y no puede convertir un padre publicado en un
render fallido.

Una fila de cero ventanas declara un espacio de trabajo solo de padre; las filas de ventanas ordinarias siguen
declarando de dos a 1.024 ventanas. Las versiones más antiguas de la aplicación que rechazan una
fila de cero ventanas conservan el registro en lugar de limpiarlo. Usa una versión
que entienda las filas de padre para reanudar el mantenimiento automático.

Las nuevas filas de padre con codificación habilitada tienen un marcador explícito `encoding: true`,
confirmado junto con ambas identidades de directorio antes de que se ejecute ningún escritor. Su
directorio privado `join` existente es el directorio temporal del codificador. Las filas antiguas
de cinco campos conservan sus reglas estrictas originales de archivos join. Un cliente antiguo
que no reconoce el marcador de codificación conserva el registro y rechaza
las nuevas asignaciones registradas; usa una versión que entienda la codificación para terminar el mantenimiento
inactivo antes de degradar. Las tomas guardadas y la evidencia de recuperación reutilizable están
separadas de estas filas desechables.

La retirada usa la raíz canónica del libro y la identidad de directorio capturadas durante
el registro. Redirigir un enlace simbólico de la raíz del libro no puede desviar la limpieza a un
libro distinto. Una raíz reemplazada o un propietario revocado conservan el registro cargado
original para la recuperación inactiva admitida; la limpieza no busca directorios movidos
ni fuerza la eliminación después de perder la autoridad.

## Límites y evidencia conservada

- Se registran como máximo 256 espacios de trabajo, con como máximo 1.024 ventanas cada uno. Este es
  un límite de admisión de almacenamiento desechable; los ajustes de workers paralelos solicitados siguen
  sin cambios. El agotamiento rechaza un render participante antes de asignar más
  audio de trabajo.
- Los registros de padre y de ventanas internas comparten ese presupuesto de 256 espacios de trabajo. Un
  render de Project con ventanas participante activo puede ocupar dos ranuras. El rechazo
  por capacidad nunca recurre a un archivo padre sin registrar.
- Cada fila de lote participante ocupa una ranura de padre, más una ranura de ventana
  interna mientras se renderiza un padre dividido. Todas las ranuras de padre del intento de lote
  actual deben admitirse antes del trabajo del proveedor. Los reintentos por OOM usan ámbitos y registros
  nuevos; las rutas del intento anterior no se reutilizan.
- Solo los archivos de ventana registrados, el padre ensamblado privado y el scratch join
  fijo, además de sus archivos de staging de publicación de diseño WAV declarados, son
  desechables. Los archivos desconocidos, enlaces simbólicos, enlaces duros, identidades de directorio
  cambiadas y la autoridad de registro inválida o ausente se conservan.
- Las filas de padre marcadas con codificación declaran además nombres de archivo fijos para el codificado, el PCM y el error
  del codificador. Su directorio join privado fijado delega como máximo
  16 archivos temporales regulares, de un solo enlace y del usuario actual, que suman como máximo 8 GiB,
  al codificador. Los directorios anidados, los alias, el scratch excesivo y las entradas no seguras
  conservan todo el registro. Cada hijo declarado se comprueba
  antes de la primera eliminación; el marcador no adopta archivos históricos.
- Un fallo entre la creación del directorio y la confirmación de su identidad puede dejar
  un directorio ambiguo vacío y una ranura reservada. Ningún proveedor puede escribir audio
  antes de la confirmación de identidad. Esta ranura queda reservada para inspección explícita;
  la limpieza no infiere autoridad a partir de su nombre.
- Los directorios existentes sin registrar `.synthesis-render-*` y `.synthesis-join-*`
  permanecen intactos. Inspecciona las rutas conocidas con la aplicación detenida antes de cualquier
  eliminación manual; nunca quites tomas guardadas ni artefactos de recuperación basándote en un
  prefijo de nombre de archivo compartido.
- La recuperación automática actualmente cubre categorías locales conocidas y sin modificar en
  Linux (custom, clone, design, LoRA y ensemble). Windows/macOS, los proveedores sobrescritos
  y los llamadores directos de Python sin arrendamiento conservan su comportamiento normal
  de archivos temporales. Todavía no tienen una cualificación declarada de recuperación tras un fallo duro.
- El registro de ensemble exige de forma conservadora que los escritores de miembros ordinarios de custom, clone,
  design y LoRA estén sin modificar, incluso cuando una categoría sobrescrita
  no forma parte de ese ensemble. De lo contrario, conserva las rutas temporales
  ordinarias. Un ensemble participante no puede superar el límite de 1.024 ventanas;
  rechaza en lugar de saltarse el almacenamiento acotado con un respaldo sin registrar.
- El scratch de los miembros de ensemble y las vistas previas de diseño de Project usan espacios de trabajo
  de ventana registrados. Las vistas previas de la pestaña Designer en `designed_voices/previews` siguen siendo
  una ruta de staging separada y no están cubiertas por este registro.
- Las filas de lote fuera de las rutas de categorías locales participantes, las llamadas directas
  sin arrendamiento, el staging heredado en `voicelines` y las exportaciones de capítulos/exportaciones PCM/mezclas
  fuera del codificador de fragmentos quedan fuera de este registro. Los archivos padre existentes
  sin registrar se conservan; este cambio no autoriza la limpieza de archivos históricos
  basada en el nombre de archivo.

Las advertencias identifican registros conservados o autoridad de registro no disponible.
Conserva esas rutas y metadatos para el diagnóstico. La aplicación no restablece un
registro corrupto, no barre el proyecto de forma recursiva ni recurre a un desbordamiento no registrado
cuando el registro participante está lleno.
