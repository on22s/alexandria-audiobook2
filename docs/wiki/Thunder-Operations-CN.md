# Thunder 运维

[中文主页](Home-CN.md) · [English](Thunder-Operations.md)

在远程 Thunder GPU 机器上运行任务的规则。

## 改动任何东西之前

- 每台机器**一次只跑一个 GPU 任务**。
- 先检查进程列表和任务的日志。

常用检查：

```bash
date '+%I:%M %p %Z'
ps -eo pid,ppid,etime,cmd
tail -f /home/ubuntu/<job>/eval.log
```

## 修改正在运行的任务

- **绝不要在 shell 脚本运行时编辑它。** Bash 是边读边执行的，编辑可能让正在运行的任务跳到错误的位置。请用新名字写一个新版本再启动它。
- **按 PID 停止任务**，而且只在确认一次运行无效时才停。
- **绝不要**用任务匹配模式执行 `pkill -f`——它可能匹配并杀掉运行它的那个 shell。

## 汇报进度

对于仍在运行的任务，汇报每个窗口的实测耗时，并把它与剩余工作量的估算分开。
