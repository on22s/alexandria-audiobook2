[English](COLAB.md) | [中文](COLAB_CN.md) | [日本語](COLAB_JA.md) | [Español](COLAB_ES.md)

# Google Colab

想用一次临时的 GPU 会话，请用[主分支的笔记本](https://colab.research.google.com/github/on22s/alexandria-audiobook2/blob/main/alexandria_colab.ipynb)。GPU 是否可用、会话能维持多久都会变化；断开之前先把工作存好。常规的 Web UI 启动会开一个公开的 ngrok 隧道。分享访问权限之前请先看[认证说明](../../README.md#authentication-optional)，并保管好 token 和 API 密钥。

## 开始一次会话

1. 选择 **Runtime → Change runtime type → T4 GPU**，然后运行 **Check GPU Runtime**。
2. 运行 **Install Alexandria**，等它成功结束。它用笔记本内核的 Python 把应用和 Qwen-TTS 依赖一起装好。Git 或装包失败会让该单元格停止；安装失败后不要再往下点 Start。
3. 可选：在**第一次加载模型之前**挂载 Drive。这会改变 Hugging Face 模型缓存的位置。它不会自动备份书、声音分配、生成的音频，也不会备份 Ollama 单独的模型缓存。
4. 配好 ngrok，运行 **Start Alexandria**。等服务端的就绪检查通过，打开显示出来的 URL。在应用的 **Setup** 页配置 LLM。
5. 如果用本地 LLM，运行 **Install Ollama for Local LLM**。否则配置你选用的 API 提供方。托管 API 会产生用量费用。

按各小节的标题去运行单元格。**Stop Ollama** 和 **Stop Server** 是清理动作，不是启动服务后马上要做的配置步骤。

## 让 LLM 和 TTS 共用这块 T4

Ollama 和本地 TTS 抢同一份显存。先把脚本生成、审阅、人设以及其他所有 LLM 任务做完。然后在批量 TTS 之前运行 **Stop Ollama**。这个单元格只回收本笔记本启动的那棵进程树，并显示一份真实的显存快照。其他进程、缓存和上下文仍然可能占着内存；停掉一个进程并不承诺收回固定的量。

配置单元格会拒绝已有监听占用它的端口。它不会接管或杀掉一个不相关的 Ollama 服务。所有权信息缺失、启动失败和清理失败都会抛错。如果所有权无法恢复，请在替换运行时之前把工作下载下来；不要用按进程名大面积 kill 的命令。

把上下文和输出上限保持在推理服务器的实际容量之内。改大应用里的设置并不会扩大 Ollama 已加载的上下文。请保持应用的 GPU/显存余量保护和 worker 逐级降级重试处于启用状态。如果你之后又回来做 LLM 工作，先把 TTS 做完，并在加载另一个模型之前检查可用显存。

## 诊断安装或启动失败

安装单元格会显示子进程的 stdout 和 stderr。请在最后的 pip 汇总**上方**找到**第一个失败的包及其 traceback**。之后出现的缺模块错误可能只是安装不完整的结果；只补装那一个模块，可能仍会留下其他缺失的依赖。

当前依赖用 `romkan2` 取代了旧的 `romkan==0.2.1`——后者的 setup 会 import 在新版 Python 中已被移除的 `imp`。应用也使用了兼容 Python 3.13 的配置注解。如果你手上是旧的已保存副本，请从 main 重新打开笔记本；只更新 checkout 并不会更新浏览器里已经加载的代码单元格。不要拿旧单元格去跑新 checkout。

报告错误时，请记录内核版本和可执行文件路径：

```python
import sys
print(sys.version)
print(sys.executable)
```

请附上失败的包名、它的 traceback 和相关已安装版本。删掉凭据和私有的书籍正文。不要假定每条依赖警告都指向致命的那个错误。运行时变了可能需要重新安装；重启之前先存好工作。

应用启动、模型下载或生成相关的日志，用 **View Server Logs** 看。应用日志是 `/content/Alexandria/colab-server.log`，另有两个轮转备份（合计最多 3 MiB）。查看器从最近 64 KiB 开始。中断查看器不会让服务器停下来。启动失败时也会打印最近的尾部日志；趁运行时还在，把有用的日志下载下来。

## 保存并验证结果

模型在首次使用时下载，每个可能占好几个 GB。没有持久存储时，新的运行时必须重新下载。结束会话之前，请把做好的有声书下载下来，并保存你需要的脚本、声音分配和日志。可选的 Drive 缓存不是完整的项目备份。

一次成功的安装、HTTP 启动检查或短预览，并不能验证整本书、每一种声音模式或听感质量。请检查所有请求的分块都已完成，然后导出并播放结果。当前 Colab 运行时的发现和整本书验证的进展记录在 [issue #1278](https://github.com/on22s/alexandria-audiobook2/issues/1278)。

做完了请用 **Stop Server**。即使隧道清理失败，它也会尝试清理服务器和笔记本自己启动的 Ollama；报告出来的清理错误需要处理。
