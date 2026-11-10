# Setup y servido

[English](Setup-and-Serving.md) · [日本語](Setup-and-Serving-JA.md) · [中文](Setup-and-Serving-CN.md) · Español

La app habla con un modelo de lenguaje a través de cualquier endpoint compatible con OpenAI, en tu propia máquina
o en una remota. La [sección de primer arranque](../../README_ES.md#primer-arranque-qué-esperar) del README
muestra cómo iniciar llama.cpp, LM Studio, Ollama o una API alojada. Esta página cubre los ajustes de servido
que se midió que importan.

## Reglas generales

- **Usa razonamiento bajo, no desactivado, y no prohíbas `<think>`.** El razonamiento bajo con un presupuesto
  del lado del servidor superó al razonamiento desactivado en todos los modelos que razonan.
- **Mantén activo el esquema JSON.** Cada solicitud lleva uno.
- **Elige el contexto para la solicitud y la memoria disponible**, en lugar de copiar un máximo.
  Consulta los pasos prácticos de dimensionamiento de abajo.

## Elegir un tamaño de contexto

**8,192 es un punto de partida local; 32,768 no es un requisito universal.** El contexto debe
contener todo el prompt (instrucciones, reparto y texto circundante), la respuesta generada
y cualquier token de razonamiento. Los contextos más grandes también consumen más memoria de caché KV.

1. Empieza con un texto corto y una ranura de servidor (`--parallel 1`). En llama.cpp, prueba
   `-c 8192` para limitar el uso de memoria en esa primera ejecución, y luego revisa el presupuesto de solicitudes.
2. En Script, ejecuta **Test this book with the LLM** antes de generar el guion completo. Hace
   un muestreo del principio, del medio y de un fragmento con mucho diálogo; pasar es evidencia útil,
   no una garantía para cada solicitud posterior.
3. Si una solicitud excede el contexto, inspecciona el error del servidor y los recuentos de tokens en
   `logs/review_responses.log`. Reduce **Step 1: text per request**, **Step 2: lines per
   request**, el texto circundante o la ventana de revisión según corresponda. Deja espacio para la salida:
   la segmentación vuelve a emitir el texto, así que la longitud de entrada por sí sola subestima sus necesidades.
4. Aumenta el contexto solo si el modelo lo admite y cabe en la VRAM disponible. `-c 32768`
   es un posible ajuste mayor, no una solución automática. Puede hacer falta una cuantización menor o un
   servidor remoto si el modelo y la caché no caben juntos.

**Qué hace realmente la optimización de LM Studio.** En la implementación actual, el respaldo local
es de 8,192 tokens y una ranura. Los perfiles de modelo conocidos pueden seleccionar 16,384 o 32,768
(y a veces dos ranuras), sujetos a una protección de VRAM en vivo con una reserva de 2 GiB. Los modelos
desconocidos o las métricas ilegibles conservan el respaldo. El optimizador remoto tiene un objetivo separado
de 98,304 tokens/dos ranuras; no supongas que ese objetivo conviene a una máquina remota arbitraria.
Revisa sus recursos y ajustes antes de habilitar la optimización remota.

Fuentes: [perfiles de carga y protecciones](../../app/lmstudio_settings.py) y
[preflight del libro y presupuesto de solicitudes](../../app/three_pass_generate.py). Describen el
código actual, no un benchmark de hardware nuevo. La guía anterior de la ficha del modelo informa de una
ventana de atribución de 5,966 tokens, pero ninguna medición enlazada establece eso como máximo
para cada libro, tokenizador, pasada del pipeline o presupuesto de finalización. Trata las
[cifras de memoria de la ficha del modelo](Which-Model-For-Your-Card-ES.md) como mediciones para su
configuración declarada, no como garantía de que todas las solicitudes quepan en 8k.

## Servir Muse-Glimmer-30B

Muse necesita el formato de razonamiento de DeepSeek cuando el razonamiento está activado:

```bash
llama-server \
  -m Muse-Glimmer-30B-UD-Q3_K_XL.gguf \
  --reasoning on \
  --reasoning-format deepseek \
  --chat-template-kwargs '{"reasoning_strength":"low"}' \
  --parallel 1
```

- Envía el esquema JSON con cada solicitud.
- **Nunca uses `--skip-chat-parsing`** al evaluar Muse con razonamiento.

## Antes de una evaluación completa de adaptador

Ejecuta primero el preflight del contrato servido. Debe analizar correctamente una solicitud
`michel2_full` con la escala candidata del adaptador **y** con la escala base 0.0.

Registra la escala candidata en el artefacto. No supongas 1.0 cuando un barrido de escala medido
eligió un valor menor.
