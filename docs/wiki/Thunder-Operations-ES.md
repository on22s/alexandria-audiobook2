# Operaciones de Thunder

[English](Thunder-Operations.md) · [日本語](Thunder-Operations-JA.md) · [中文](Thunder-Operations-CN.md) · Español

Reglas para ejecutar trabajos en las máquinas GPU remotas de Thunder.

## Antes de cambiar nada

- Cada máquina ejecuta **un trabajo de GPU a la vez**.
- Comprueba primero la lista de procesos y el log del trabajo.

Comprobaciones útiles:

```bash
date '+%I:%M %p %Z'
ps -eo pid,ppid,etime,cmd
tail -f /home/ubuntu/<job>/eval.log
```

## Cambiar un trabajo en ejecución

- **Nunca edites un script de shell mientras se está ejecutando.** Bash lee los scripts sobre la marcha, así que una
  edición puede hacer que el trabajo en ejecución salte al lugar equivocado. Escribe una nueva versión con un nombre nuevo
  y lanza esa.
- **Detén los trabajos por PID**, y solo cuando se sabe que una ejecución no es válida.
- **Nunca uses `pkill -f`** con un patrón de trabajo: puede coincidir con el shell que lo ejecuta y matarlo.

## Informar del progreso

Para un trabajo que sigue en ejecución, informa del tiempo medido por ventana, y mantenlo separado
de la estimación del trabajo que queda.
