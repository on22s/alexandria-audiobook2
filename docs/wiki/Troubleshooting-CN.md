# 故障排查（中文）

[English](Troubleshooting.md) · [日本語](Troubleshooting-JA.md)

## 书与保存

### 崩溃后保存的书打不开

加载或保存一本书会一次替换多个文件，应用会把这次切换记入事务日志。被中断的切换会在下次打开书时自动完成或回滚。如果文件与事务日志不一致，应用不会猜测，而是拒绝打开，并在 Home 页按顺序给出要尝试的命令。在 Alexandria 文件夹中用应用的 Python 运行：

```bash
python app/book_state_recovery.py inspect --hash
python app/book_state_recovery.py finish
python app/book_state_recovery.py quarantine --accept-current-files
```

- `inspect` 只读：显示待处理的事务日志、每个文件的预期状态与当前状态、工作文件夹、退避文件夹和崩溃残留。它不会回收任何内容。
- `finish` 立即执行应用打开书之前使用的恢复。
- `quarantine --accept-current-files` 是最后手段：保留当前文件不动，把事务日志和工作文件夹移到 `.book-switch-quarantine-*`，不删除任何内容。
- 旧版本应用写的事务日志需要 `migrate-legacy --trust-journal-hashes`。

在确认没有切换正在运行之前，不要删除 `.book-switch-*` 文件夹；崩溃在事务日志出现之前留下的文件夹不会被自动回收。各种崩溃状态、恢复摘要和退出码见[中文指南](../../docs/guides/BOOK_SWITCH_RECOVERY_CN.md)。
