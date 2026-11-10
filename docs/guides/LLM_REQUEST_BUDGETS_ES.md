[English](LLM_REQUEST_BUDGETS.md) | [中文](LLM_REQUEST_BUDGETS_CN.md) | [日本語](LLM_REQUEST_BUDGETS_JA.md) | [Español](LLM_REQUEST_BUDGETS_ES.md)

# Presupuestos de solicitudes al LLM

En **Setup → LLM Settings**, cada perfil Local o Remote tiene tres límites de tokens opcionales:

| Ajuste | Comportamiento si está vacío | Efecto |
| --- | --- | --- |
| Total context budget | Usa el contexto cargado que se detecte, cuando esté disponible | Limita la suma del prompt, la salida y la reserva de seguridad. Una declaración no puede elevar un contexto detectado que sea menor. |
| Input ceiling | Hereda el límite global de entrada de Advanced, si está definido | Limita el prompt completo estimado, incluidas las instrucciones, el reparto y el texto circundante. |
| Output ceiling | Sin tope por perfil: cada tarea conserva su propio tope de reintentos | Cuando se define, limita las asignaciones de finalización y la escalada de reintentos, incluido el razonamiento cuando el proveedor lo cuenta como salida. |

Los valores deben ser números enteros positivos. No existe un tope superior universal ni un ajuste de ilimitado/omisión. Los techos de entrada y salida son restricciones separadas, no es prueba de que su suma quepa en el contexto cargado. Por ejemplo, un techo de entrada y un techo de salida de 4.096 cada uno no pueden consumirse ambos en un contexto de 4.096 tokens.

Si el contexto cargado es 8.192 y tu declaración es 4.096, Alexandria usa 4.096. Si el contexto cargado es 4.096 y tu declaración es 8.192, Alexandria sigue usando 4.096. Cuando el endpoint no puede exponer su contexto, la declaración aporta el presupuesto que falta. Declara el contexto que realmente admite tu modelo cargado, no su máximo anunciado.

Los objetivos más grandes de tres pasadas para fuente/contexto requieren un contexto detectado o declarado. Sin él, la solicitud de inicio explica qué campo de Setup hay que definir antes de reservar un trabajo en segundo plano. Un modelo de LM Studio gestionado que aún no está cargado cuenta como conocido, porque la ejecución lo carga primero con el contexto previsto. La planificación y las solicitudes HTTP/manuales reales usan el mismo resolvedor de límites. Los prompts sobredimensionados se rechazan; los presupuestos de salida pueden reducirse para ajustarse al contexto restante. Las comprobaciones existentes de fidelidad de la fuente, los reintentos acotados y la admisión de GPU siguen activos.

Los recuentos de tokens usan la estimación conservadora por caracteres que ya existía, no un tokenizador específico del proveedor. La aplicación del contexto en el servidor sigue siendo necesaria. Declarar un presupuesto menor restringe las solicitudes; no libera la VRAM ya asignada por el servidor ni recarga el modelo.

## Configuraciones existentes

Nada cambia para una configuración existente hasta que se define un límite de perfil. Baseline Response Tokens sigue siendo un presupuesto inicial, no un tope: los reintentos por truncamiento aún escalan por encima de él hasta el techo propio de cada tarea (16384, o el valor base si es mayor, para tres pasadas; 32768 para review), de modo que un modelo de razonamiento no se corta en el valor base. Define un Output ceiling de perfil para limitar un modelo; la comprobación de contexto combinado sigue aplicándose.

No se impone ningún techo global de entrada nuevo a las configuraciones existentes: su valor ausente/vacío conserva la comprobación basada en el contexto. Un techo de entrada de perfil vacío hereda el global en tiempo de ejecución sin cambiar los ajustes guardados del perfil. El transporte manual aplica los mismos límites declarados antes de presentar una solicitud para copiarla. La conmutación por error usa los límites del perfil secundario en lugar de ampliarlos al presupuesto del modelo primario.
