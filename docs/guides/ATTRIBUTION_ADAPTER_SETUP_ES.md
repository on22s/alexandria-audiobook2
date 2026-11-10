[English](ATTRIBUTION_ADAPTER_SETUP.md) | [中文](ATTRIBUTION_ADAPTER_SETUP_CN.md) | [日本語](ATTRIBUTION_ADAPTER_SETUP_JA.md) | [Español](ATTRIBUTION_ADAPTER_SETUP_ES.md)

# Activar un adaptador de atribución

Los adaptadores existen ahora por una sola razón: **permitir que se publique un quant más pequeño.** El
prompt (`michel2_full`) hacía el trabajo en los modelos base; un adaptador merece
cargarse solo en el nivel donde se ha medido que ayuda. Todas las filas son el
fixture del producto de cuatro libros (768 filas, base → adaptador emparejados en un servidor con
la escala del adaptador alternada, temperatura 0, esquema JSON, `michel2_full`), de
la colección pública
[Om22s/alexandria-qwen3-attribution](https://huggingface.co/Om22s/alexandria-qwen3-attribution)
(RECIPES §"Attribution adapters", 2026-09-20):

| base | nivel (archivo) | razonamiento | base → adaptador | ¿cargarlo? |
| --- | --- | --- | ---: | --- |
| Qwen3.8-27B | IQ2_XXS (7.3 GB) | activado | 83.1 → **85.7 (+2.6)** | **sí** — el nivel para el que existe este adaptador |
| Qwen3.8-27B | Q3_K_XL (13.1 GB) | activado | 87.5 → **88.8 (+1.3)** | sí |
| Qwen3.8-27B | Q4_K_M (16.5 GB) | activado / desactivado | 89.8 → 89.8 (0) / 87.4 → 88.3 (+0.9) | no con razonamiento; marginal sin él |
| Qwen3.6-35B-A3B | IQ1_M (10.0 GB) | desactivado | 82.9 → **85.9 (+3.0)** | **sí, solo con razonamiento desactivado** |
| Qwen3.6-35B-A3B | IQ1_M | activado | 87.5 → 85.0 (−2.5) | no |
| Qwen3.6-35B-A3B | IQ2_XXS (10.8 GB) | activado | 88.3 → 87.9 (−0.4) | no |
| Qwen3.6-35B-A3B | IQ3_XXS (13.2 GB) | activado | 87.5 → 86.6 (−0.9) | no |
| Qwen3.6-35B-A3B | Q4_K_XL (22.4 GB) | activado | 90.8 → 88.5 (−2.2) | no |
| Qwen3-14B | Q4_K_M (9.0 GB) | bajo, presupuesto 1024 | 66.1 → **74.7 / 74.9 (+8.6 / +8.8)** con `default`; **84.3 → 84.3 (0)** con `michel2_full` en nueve novelas de PDNC | solo con el prompt `default`; con `michel2_full` usa la base |

Hay dos cosas que la tabla no dice por sí sola. El adaptador A3B sobre texto de PDNC
(listas de personajes con alias) copia la línea de la lista — `EMMA (also: EMMA
WOODHOUSE, …)` — en hasta un 12% de las filas; la app lee eso como `EMMA` desde
el #625, pero el harness lo puntúa mal, así que las tarjetas del propio adaptador llevan ambos
números. Y la mejora de Qwen3-14B depende del prompt: se entrenó con la
forma `default` y es nula con el prompt del producto.

## Es un cambio de servicio, no de la aplicación

El adaptador lo aplica `llama-server`. Nada en `app/` calcula con él.

```bash
llama-server \
  -m /path/to/Qwen3.8-27B-UD-IQ2_XXS.gguf \
  --lora /path/to/qwen38-rightsclean-michel2.f16.gguf \
  --host 127.0.0.1 --port 8090 -c 32768 --parallel 1
```

El `.f16.gguf` de cada directorio `adapters/<name>/` de la colección es
el archivo que se midió (`examples/serve_llamacpp.sh` allí hace lo
anterior); el `adapter_mixed.gguf` entrenado con novelas ligeras que describían versiones anteriores
de esta página no es distribuible y se queda en el archivo privado.

Luego apunta la app a él, en `app/config.json`, dentro del bloque del modo
que estés ejecutando:

```json
"llm_local": {
  "base_url": "http://127.0.0.1:8090/v1",
  "model_name": "qwen3.8-27b",
  "attribution_adapter": {
    "path": "models/qwen38-rightsclean-michel2.f16.gguf",
    "scale": 1.0,
    "require": false
  }
}
```

El adaptador vive en el mismo bloque que `base_url` a propósito: pertenece a
ese endpoint, y una segunda ubicación podría contradecirlo sobre qué servidor
se quiere decir.

## Qué hace la comprobación

`generate_script.py` imprime, al arrancar:

```
LLM adapter: attribution_adapter=qwen38-rightsclean-michel2.f16.gguf scale=1.0 require=False
```

y avisa si el servidor en realidad no lo está sirviendo. **Este es el sentido de
todo esto.** Sin ello, un servidor iniciado sin `--lora`, o con la escala
puesta a cero, responde a todas las solicitudes tan feliz con la calidad base.
Nada falla. El libro se genera. Simplemente es el número base y ningún
artefacto registra por qué — la misma forma que el bug de la semilla, que costó seis
comparaciones contaminadas antes de que alguien lo notara de oído.

`require: true` convierte el aviso en un rechazo. El valor por defecto avisa, porque un
libro generado a medias durante la noche no debería morir cuando se reinicia un servidor, pero
la ejecución debe registrar el hecho.

## Tres límites honestos

**El endpoint debe ser llama.cpp.** LM Studio y Ollama no implementan
`/lora-adapters`, así que la comprobación informa "cannot verify" en lugar de fingir
que el adaptador está ausente. Es deliberado: un aviso que salta en cada configuración
es uno que nadie lee.

**La escala no está ajustada.** 1.0 es lo que usó la evaluación. `POST /lora-adapters` puede
ponerla en cualquier valor, y nada aquí ha medido si 0.7 o 1.3 es mejor.

**La mejora está medida en novelas ligeras, más dos novelas de PDNC.** Los cuatro
libros de referencia son novelas ligeras japonesas en traducción al inglés; las celdas
de dos libros de PDNC (Emma, retenida; The Sun Also Rises, una novela de entrenamiento) están en las
tarjetas. Los datos de entrenamiento son prosa de dominio público en inglés, así que la mejora
en novelas ligeras es transferencia, y es pequeña.
