[English](COLAB.md) | [中文](COLAB_CN.md) | [日本語](COLAB_JA.md) | [Español](COLAB_ES.md)

# Google Colab

Usa [el notebook de la rama main](https://colab.research.google.com/github/on22s/alexandria-audiobook2/blob/main/alexandria_colab.ipynb)
para una sesión temporal con GPU. La disponibilidad de GPU y la duración de la sesión varían; guarda
tu trabajo antes de desconectarte. El arranque normal de la interfaz web abre un túnel ngrok
público. Consulta [autenticación](../../README.md#authentication-optional) antes de
compartir el acceso y mantén privados los tokens y las claves de API.

## Iniciar una sesión

1. Selecciona **Runtime → Change runtime type → T4 GPU** y luego ejecuta **Check GPU Runtime**.
2. Ejecuta **Install Alexandria** y espera a que termine correctamente. Instala
   juntas la aplicación y las dependencias de Qwen-TTS con el Python del kernel del
   notebook. Los fallos de Git o de paquetes detienen la celda; no continúes a Start tras
   una instalación fallida.
3. De forma opcional, monta Drive **antes de la primera carga de modelo**. Esto cambia la
   ubicación de la caché de modelos de Hugging Face. No hace copia de seguridad automática de
   libros, asignaciones de voces, audio generado ni la caché de modelos separada de Ollama.
4. Configura ngrok y ejecuta **Start Alexandria**. Espera a la comprobación de disponibilidad
   del servidor y abre la URL que se muestra. Configura el LLM en la pestaña **Setup** de la app.
5. Si usas un LLM local, ejecuta **Install Ollama for Local LLM**. Si no, configura
   el proveedor de API que hayas elegido. Una API alojada puede generar cargos por uso.

Ejecuta las celdas por sus encabezados de sección. **Stop Ollama** y **Stop Server** son
acciones de limpieza, no pasos de configuración que debas ejecutar justo después de iniciar los servicios.

## Compartir la T4 entre el LLM y el TTS

Ollama y el TTS local compiten por la misma VRAM. Completa primero la generación de guion,
la revisión, las personas y cualquier otra tarea del LLM. Después ejecuta **Stop Ollama** antes
del TTS por lotes. Esa celda retira solo el árbol iniciado por este notebook y muestra
una instantánea real de la memoria del dispositivo. Otros procesos, cachés y contextos pueden
seguir ocupando memoria; detener un proceso no garantiza recuperar una cantidad fija.

La celda de configuración rechaza un listener existente en su puerto. No adopta ni
mata un servicio de Ollama ajeno. La falta de propiedad, los fallos de arranque y la limpieza
fallida generan errores. Si no se puede recuperar la propiedad, descarga tu trabajo
antes de reemplazar el entorno de ejecución; no uses un comando amplio de matar por nombre de proceso.

Mantén los límites de contexto y de salida dentro de la capacidad real del servidor de inferencia.
Aumentar un ajuste de la app no agranda el contexto cargado de Ollama. Mantén activadas
las protecciones de GPU/margen de la app y los reintentos de reducción de workers. Si
vuelves al trabajo del LLM más tarde, termina primero el TTS y comprueba la VRAM disponible antes de
cargar otro modelo.

## Diagnosticar fallos de instalación o arranque

La celda de instalación muestra la salida estándar y de error de los procesos hijos. Busca el **primer
paquete fallido y su traceback**, por encima del resumen final de pip. Un error posterior de módulo
faltante puede ser consecuencia de una instalación incompleta; instalar solo ese
módulo puede dejar otros requisitos sin instalar.

Los requisitos actuales usan `romkan2` en lugar del antiguo `romkan==0.2.1`, cuyo setup
importa `imp`, eliminado en versiones más recientes de Python. La app también usa anotaciones de
configuración compatibles con Python 3.13. Vuelve a abrir el notebook desde main cuando uses una copia
guardada antigua; actualizar solo el checkout no actualiza las celdas de código ya
cargadas en tu navegador. No sigas ejecutando celdas antiguas contra un checkout nuevo.

Anota la versión del kernel y el ejecutable al informar de un error:

```python
import sys
print(sys.version)
print(sys.executable)
```

Incluye el nombre del paquete fallido, su traceback y las versiones instaladas relevantes.
Elimina credenciales y texto privado de libros. No supongas que toda advertencia de dependencia
identifica el error fatal. Un entorno de ejecución cambiado puede requerir una nueva instalación;
guarda tu trabajo antes de reiniciarlo.

Para el arranque de la aplicación, las descargas de modelos o la generación, usa **View Server Logs**.
El log de la app es `/content/Alexandria/colab-server.log`, con dos copias de seguridad rotadas
(como máximo 3 MiB en total). El visor empieza por los últimos 64 KiB. Interrumpir el
visor deja el servidor en marcha. Un fallo de arranque también imprime la cola reciente;
descarga los logs útiles antes de que desaparezca el entorno de ejecución.

## Guardar y validar el resultado

Los modelos se descargan en el primer uso y pueden ocupar varios GB cada uno. Sin almacenamiento
persistente hay que descargarlos de nuevo en un entorno de ejecución nuevo. Descarga el
audiolibro terminado y conserva los guiones, las asignaciones de voces y los logs que necesites antes de
terminar la sesión. La caché opcional de Drive no es una copia de seguridad completa del proyecto.

Una instalación correcta, una comprobación de arranque HTTP o una vista previa corta no validan un
libro completo, todos los modos de voz ni la calidad de escucha. Comprueba que todos los fragmentos
solicitados terminan, y luego exporta y reproduce el resultado. Los hallazgos actuales del entorno de ejecución de Colab
y la validación de libros completos se siguen en [issue #1278](https://github.com/on22s/alexandria-audiobook2/issues/1278).

Usa **Stop Server** cuando termines. Intenta la limpieza del servidor y del Ollama propiedad del notebook
aunque falle la limpieza del túnel; los errores de limpieza notificados requieren atención.
