# Publicaciones en Hugging Face

[English](Hugging-Face-Releases.md) · [日本語](Hugging-Face-Releases-JA.md) · [中文](Hugging-Face-Releases-CN.md) · Español

Los adaptadores públicos están en
[Om22s/alexandria-qwen3-attribution](https://huggingface.co/Om22s/alexandria-qwen3-attribution).
La lista de comprobación completa de publicación es [HF_MODEL_GUIDE.md](../../HF_MODEL_GUIDE.md).

## Qué se puede publicar

Solo archivos cuyos derechos de redistribución se han verificado.

## Qué debe incluir toda publicación pública

- el hash del archivo exacto que se probó;
- el modelo base y su licencia;
- las fuentes de datos de entrenamiento y sus derechos;
- el prompt con el que se entrenó y el comando de servido;
- resultados de evaluación completos, incluidas las limitaciones.

## Nombres de adaptadores

Cada nombre de adaptador termina con el prompt con el que se entrenó, porque un adaptador solo funciona
con ese prompt:

| etiqueta | prompt |
|---|---|
| `attrv1` | el prompt de atribución anterior al 2026-09-14 |
| `attrv2` | el prompt de atribución desde el 2026-09-14 |
| `michel2v1` | `MICHEL2_SYSTEM`, 1,825 caracteres (antes de #619/#623) |
| `michel2v2` | `MICHEL2_SYSTEM`, 2,071 caracteres (después de #619/#623) |

Los textos de los prompts se publican en la carpeta `prompts/` de la colección.

## Resultados negativos

- Una evaluación negativa se puede publicar sin subir pesos de adaptador restringidos ni
  texto de origen.
- Los adaptadores fallidos, o los adaptadores con un defecto de configuración, deben etiquetarse claramente y nunca
  promocionarse como modelos de calidad.
- Cuando un resultado publicado se corrige después, la ficha lo dice y dice qué cambió; por
  ejemplo, la repuntuación del 2026-09-28, publicada como colección v1.6.1.
