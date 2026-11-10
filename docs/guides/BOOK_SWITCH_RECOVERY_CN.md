[English](BOOK_SWITCH_RECOVERY.md) | [中文](BOOK_SWITCH_RECOVERY_CN.md) | [日本語](BOOK_SWITCH_RECOVERY_JA.md) | [Español](BOOK_SWITCH_RECOVERY_ES.md)

# 中断的保存和切换

加载一本已保存的书，或保存当前的书，会一次替换多个文件：脚本、声音设置、分块文件等。应用把这些操作作为一个带事务日志的事务来做，因此崩溃、断电或磁盘写满都不会留下半新半旧的书。本指南说明中断之后会发生什么，以及应用自己无法完成时该怎么做。

## 自动完成的处理

在每次读取或修改这本书的操作之前，应用会查找被中断的切换：数据文件夹里名为 `.active_book_transaction.json` 的事务日志，以及旁边的工作文件夹 `.book-switch-<id>/`。（Home 页的状态读取是例外：它不执行恢复，所以可能显示一个 `finish` 仍能完成的切换。）

- **恢复会完成或回滚切换**，前提是每个文件都在事务日志记录的位置，且字节与记录一致。这是常见情况，你什么也看不到。
- **自动隔离**适用于每个书籍文件仍与事务日志一致、但工作文件夹不一致的情况：暂存文件被改动、出现了未知文件，或清理中途停止。工作文件夹和事务日志会被移入 `.book-switch-quarantine-<journal hash>-<nonce>/`，书照常打开。不会删除任何内容。
- **其他情况会拒绝切换**，书不会打开。这发生在书籍文件本身被改动、文件夹被替换，或事务日志损坏时。应用不会猜测哪个版本是对的。

当书因为这个原因无法打开时，Home 页会按尝试顺序列出下面的命令：`inspect`，然后 `finish`，只有在 `finish` 拒绝时才用 `quarantine --accept-current-files`。

## 崩溃留下的残留

崩溃可能留下两样没有任何事务日志认领的东西：

- `.book-switch-<id>/`：在事务日志发布之前就结束的切换的工作文件夹。它可能包含整本书的暂存副本。
- `.active_book_transaction.json.new-<id>`：已经写入但从未成为事务日志的日志发布临时文件。

`inspect` 把它们列为 `orphaned_staging` 和 `journal_temporaries`，并给出条目数、字节数和最新时间戳。恢复摘要只报告数量和总字节数（`leftovers`），绝不报告名称或路径，并且在应用忙碌时报告 `leftovers: null`（因为应用可能正在暂存）。只是看起来相似的名字、链接，或待处理事务日志自己指定的工作文件夹，绝不会被报告为孤儿。

应用从不删除这些残留：事务日志出现之前的文件夹没有证据证明属于我们，所以回收与否由你决定。确认书能打开、且该文件夹不是正在运行的切换之后，你自己删除它，就像处理一个隔离文件夹。`inspect --no-lock` 可能显示一个实际上是运行中切换的暂存目录；删除前请在锁下重新运行 `inspect`。

## 崩溃留下什么

切换按固定顺序修改书籍文件。在 Linux 和 macOS 上（文件系统可以在一步内交换两个名字，即 “v4” 事务日志），它会：

1. 在私有的 `.book-switch-<id>/` 文件夹里暂存每个新文件，
2. 写入事务日志 `.active_book_transaction.json`（阶段 `publishing`），
3. 逐个把暂存文件换入正式位置，
4. 把事务日志标记为 `committed`，
5. 删除暂存文件夹，然后删除事务日志。

每个书籍操作都会先运行恢复（`app/book_state_transaction.py` 中的 `recover_book_state_locked`）。下面是每个崩溃点之后你会看到什么，以及下次打开书时会发生什么。方括号里是处理该情况的函数。

**在事务日志存在之前（第 1 或第 2 步期间）。**
- 磁盘上：一个 `.book-switch-<id>/` 文件夹，可能还有一个写了一半的 `.active_book_transaction.json.new-<id>`。所有书籍文件都未被改动。
- 下次打开：没有需要撤销的东西，书按原样打开。`inspect` 把该文件夹列在 `orphaned_staging` 下，把写了一半的文件列在 `journal_temporaries` 下；Home 页显示它们的数量和大小。不会替你删除任何东西。[`recover_book_state_locked`, `_get_leftover_staging_entries`]
- 需要你做的：确认没有切换在运行后，如果想收回空间，就手动删除它们。

**事务日志存在之后、尚未换入任何文件之前。**
- 磁盘上：事务日志写着 `publishing`；每个书籍文件仍是旧版本。
- 下次打开：逐个文件与事务日志核对，删除暂存文件夹和事务日志，书按原样打开。[`_recover_book_exchange_journal`]
- 需要你做的：无需操作。

**换入过程中（第 3 步）。**
- 磁盘上：一些文件是新的，一些是旧的。每个已换入文件的旧版本（如果是新建文件，则是 64 字节的占位符）在暂存文件夹里。正在创建的文件可能短暂地在其正式位置显示该占位符。
- 下次打开：再次核对两侧之后，每个已换入的文件都会被换回，书精确恢复到切换之前的样子。切换正在创建的文件会再次消失。[`_recover_book_exchange_journal`, `_undo_book_exchange`]
- 需要你做的：无需操作。

**所有文件都已换入，但事务日志尚未 `committed`。**
- 磁盘上：每个书籍文件都是新的；暂存文件夹里是旧版本。
- 下次打开：按上一种情况处理。切换会回滚，书打开旧文件，因为在写入 `committed` 之前没有任何东西承诺切换已经完成。唯一的例外是：事务日志里的每一行都是替换，每个文件都证明了自己的新内容，且没有旧副本可供恢复；这种情况会直接按新文件完成。已完成和未完成文件混在一起、又无法恢复时，会拒绝打开，而不是打开一本半新半旧的书。[`_recover_book_exchange_journal`]
- 需要你做的：无需操作，除非它拒绝（“cannot be completed consistently”）。那就运行 `inspect`，`quarantine --accept-current-files` 会保留文件当前的样子。

**`committed` 之后、清理期间（第 5 步）。**
- 磁盘上：事务日志写着 `committed`；每个书籍文件都是新的；暂存文件夹可能还有一部分。
- 下次打开：逐个核对每个书籍文件的新内容，然后只删除切换自己创建的东西并移除事务日志。暂存文件夹里任何无法证明属于我们的内容，会和事务日志一起被移入 `.book-switch-quarantine-<hash>-<nonce>/`，书仍然用新文件打开。[`_recover_book_exchange_journal`, `_quarantine_book_transaction`]
- 需要你做的：无需操作。不再需要证据时，删除隔离文件夹。

**隔离期间（其 `receipt.json` 仍写着 `prepared`）。**
- 磁盘上：隔离文件夹，暂存文件夹和事务日志已被移入其中，或仍在书籍根目录。
- 下次打开：如果事务日志仍在根目录，隔离会被完成，但只有一切仍与回执记录一致才行；否则恢复会拒绝并保留一切。如果事务日志已经移走，书会打开，并在再次核对已移动事务日志的大小和哈希、以及已移动暂存文件夹的身份之后，把回执标记为 `completed`。未通过该核对回执会保持 `prepared`，什么也不会移动。[`_quarantine_book_transaction`, `_complete_interrupted_quarantines`]
- 需要你做的：通常无需操作。回执停留在 `prepared` 说明有东西变了；`inspect` 会显示它。

**另一个程序在切换运行期间修改了书籍文件。**
在事务日志之后的任何一步，如果某个书籍文件既不是切换的旧版本也不是新版本，恢复就会拒绝：它不会交换、删除或移走一个不是它写入的文件。书会保持关闭，直到你用 `inspect` 然后 `quarantine --accept-current-files` 做出选择。（一个例外：Windows 风格的切换如果在移动任何文件之前失败，并在放弃时发现另一个程序的改动，仍会移除自己的事务日志。切换没有应用任何东西，另一个程序的改动保留，应用日志会出现一条以 “Book switch ... was retired without recording recovered” 开头的警告。）

**Windows 以及无法交换名字的文件系统（“v3” 事务日志）。**
这些系统用两次重命名发布每个文件：先把旧文件重命名为暂存文件夹里的 `old-<i>`，再把新文件移入正式位置。没有占位符，所以两次重命名之间崩溃会让书籍文件短暂缺失。恢复遵循上面的相同规则：核对保存的旧文件，把已安装的新文件移到 `discard-<i>`，再把旧文件放回；如果事务日志已经写着 `committed`，则向前完成。[`_recover_book_journal`；当交换不可用时 `apply_book_state_locked` 选择 v3，这包括所有 Windows 切换。]

所有这些都遵循一条规则，来自 #1816：恢复绝不会重命名到一个它不再能证明属于自己的名字上。当无法判断哪个文件是我们的时，它会保留事务日志、暂存文件夹和两个文件，并拒绝打开书，而不是猜测。

## 切换期间的读取方

在 Linux 或 macOS 上创建或删除文件时，该名字可能短暂地持有一个 64 字节的占位符：文本 `alexandria-book-placeholder-v1 `、一个随机 id 和一个换行（`app/book_placeholder.py`）。它绝不是真正的内容。应用的大多数读取方只在取得书籍锁（`ensure_book_state`）之后读取书，这会先完成或拒绝任何待处理的切换，因此它们永远不会看到占位符。少数不持锁运行的读取方会检查它：

- **下载和导出**会返回 409 `SWITCH_IN_PROGRESS`（“A book switch is in progress. Wait a moment and download again.”），而不是提供占位符。路由会检查名字（`_require_export_download`，`app/routers/editor.py`），响应会检查它实际打开的文件（`ExportFileResponse`，`app/export_download.py`），所以中途开始的切换仍会被抓住。ZIP 下载会跳过占位符文件，如果所有文件都是占位符则返回 409（`build_zip_download`）。
- **分块读取**（`ProjectManager._read_chunks`，`app/project.py`）会以 “A book switch is in progress; chunks are not available yet” 拒绝。它们从不解析占位符、把它重命名为损坏文件或在其上重新生成，并且检查读取到的字节而不是名字，所以中途的交换也会被抓住。
- **范围导出**把占位符 `state.json` 当作“还没有清单”（`is_scope_export_historical`，`app/project_scope_exports.py`）。

## 恢复命令

在 Alexandria 文件夹的终端里，使用应用的 Python 运行（Pinokio 下是 `app/env` 中的环境）：

```
python app/book_state_recovery.py inspect --hash
python app/book_state_recovery.py finish
python app/book_state_recovery.py quarantine --accept-current-files
python app/book_state_recovery.py migrate-legacy --trust-journal-hashes
```

| 命令 | 作用 |
|---|---|
| `inspect` | 只读报告：待处理的事务日志、每个文件的预期状态与当前状态、工作文件夹、每个隔离文件夹及其回执，以及任何崩溃残留（`orphaned_staging`、`journal_temporaries`）。加 `--hash` 会哈希每个文件，并报告它持有事务日志的 `old` 字节、`new` 字节，还是 `neither`。仅凭时间戳可能漏掉同样大小的重写：FAT/exFAT 驱动器保留 2 秒精度的时间。`--no-lock` 在应用忙碌时读取，作为尽力而为的快照；它显示的条目可能是正在运行的切换的暂存。 |
| `finish` | 立即运行应用所运行的同一套恢复。 |
| `quarantine --accept-current-files` | 即使书籍文件与事务日志不一致，也按它们当前的样子保留。工作文件夹和事务日志被移走，不删除任何内容，书会打开。在 `inspect` 显示当前文件正是你想要的文件之后使用。 |
| `migrate-legacy --trust-journal-hashes` | 用于旧版本应用的事务日志（v1），它记录每个文件的哈希但不记录身份。加上该标志后，持有其阶段所记录哈希的文件会被信任。当每个文件都匹配时，工作文件夹和旧日志会被移入 `.book-switch-migration-<hash>-<nonce>/`，书会打开。发布中途被中断的保存两种状态都不匹配：每个不匹配的文件都会被列出，什么也不会移动。 |

常用选项：`--json` 打印一个 JSON 对象，`--data-dir DIR` 指定数据文件夹（默认是应用自己的：`ALEXANDRIA_DATA_DIR`，否则是 Alexandria 文件夹），`--wait SECONDS` 等待另一个书籍操作结束。不加 `--wait` 时，忙碌的应用会立即被报告，而不是挂起。

| 退出码 | 含义 |
|---|---|
| 0 | 已完成，或没有待处理的事务。 |
| 1 | 系统错误，包括不确定的事务日志写入（证据保留；重试）。 |
| 2 | 用法错误，例如缺少确认标志。 |
| 3 | 拒绝；消息说明原因，什么也没有移动。当 `inspect` 无法读取事务日志时也会返回。 |
| 4 | 忙碌：另一个书籍操作持有锁。 |
| 5 | 有待处理的旧版 v1 事务日志；使用 `migrate-legacy`。 |

## 隔离文件夹

每个 `.book-switch-quarantine-*` 或 `.book-switch-migration-*` 文件夹包含：

- `receipt.json`：移动进行中为 `prepared`，之后为 `completed`。它还记录事务日志的哈希和阶段以及移动原因：操作员隔离时 `forced` 为 true，迁移会加上 `kind: legacy-migration` 和 `trust: journal-digests`。
- `journal.json`：事务日志本身，是移动而不是复制，所以字节完全一致。
- `workspace/`：存在过的工作文件夹，被移动过来。

应用从不删除这些文件夹。不再需要证据时自己删除；`inspect` 会列出它们全部。如果移动在事务日志仍位于数据文件夹时中断，下一次恢复（隔离）或同一命令（操作员隔离、迁移）会完成它。如果它在事务日志移走之后停止，下一次书籍操作（或 `finish`）会完成回执，但前提是重新核对 `journal.json` 仍具有回执记录的精确大小和哈希，且 `workspace/` 仍是回执所指的文件夹。如果无法证明，回执保持 `prepared`，什么也不会移动，`inspect` 会为它显示 `journal_held: true`。

## 信任边界

回执没有签名。任何能写数据文件夹的人本来就能改书籍文件，所以这些工具信任该文件夹。只有在工作文件夹确实被移走之后，回执的 `forced` 标志才会被认可，因此手写的回执无法把普通恢复变成强制隔离。
