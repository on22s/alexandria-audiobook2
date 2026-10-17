# Google Colab

Use [the main-branch notebook](https://colab.research.google.com/github/on22s/alexandria-audiobook2/blob/main/alexandria_colab.ipynb)
for a temporary GPU session. GPU availability and session lifetime vary; save
your work before disconnecting. The normal web-UI startup opens a public ngrok
tunnel. See [authentication](../../README.md#authentication-optional) before
sharing access, and keep tokens and API keys private.

## Start a session

1. Select **Runtime → Change runtime type → T4 GPU**, then run **Check GPU Runtime**.
2. Run **Install Alexandria** and wait for it to finish successfully. It installs
   the application and Qwen-TTS dependencies together using the notebook kernel's
   Python. Git or package failures stop the cell; do not continue to Start after
   a failed installation.
3. Optionally mount Drive **before the first model load**. This changes the
   Hugging Face model cache location. It does not automatically back up books,
   voice assignments, generated audio or Ollama's separate model cache.
4. Configure ngrok and run **Start Alexandria**. Wait for the server's readiness
   check and open the displayed URL. Configure the LLM in the app's **Setup** tab.
5. If using a local LLM, run **Install Ollama for Local LLM**. Otherwise configure
   your chosen API provider. A hosted API can incur usage charges.

Run cells by their section headings. **Stop Ollama** and **Stop Server** are
cleanup actions, not setup steps to run immediately after starting services.

## Share the T4 between the LLM and TTS

Ollama and local TTS compete for the same VRAM. Complete script generation,
review, personas and any other LLM tasks first. Then run **Stop Ollama** before
batch TTS. That cell retires only the tree started by this notebook and displays
an actual device-memory snapshot. Other processes, caches and contexts can
still hold memory; stopping a process does not promise a fixed amount reclaimed.

The setup cell refuses an existing listener on its port. It does not adopt or
kill an unrelated Ollama service. Missing ownership, startup failures and failed
cleanup raise errors. If ownership cannot be recovered, download your work
before replacing the runtime; do not use a broad process-name kill command.

Keep context and output limits within the inference server's actual capacity.
Increasing an app setting does not enlarge Ollama's loaded context. Keep the
app's GPU/headroom protections and worker step-down retries enabled. If you
return to LLM work later, finish TTS first and check available VRAM before
loading another model.

## Diagnose installation or startup failures

The install cell displays child stdout and stderr. Find the **first failed
package and its traceback**, above the final pip summary. A later missing-module
error can be a consequence of an incomplete installation; installing only that
one module may leave other requirements missing.

Current requirements use `romkan2` instead of legacy `romkan==0.2.1`, whose setup
imports `imp` removed in newer Python. The app also uses Python 3.13-compatible
configuration annotations. Reopen the notebook from main when using an older
saved copy; updating the checkout alone does not update code cells already
loaded in your browser. Do not continue running old cells against a new checkout.

Record the kernel version and executable when reporting an error:

```python
import sys
print(sys.version)
print(sys.executable)
```

Include the failed package name, its traceback and relevant installed versions.
Remove credentials and private book text. Do not assume every dependency warning
identifies the fatal error. A changed runtime can require a new installation;
save your work before restarting it.

For application startup, model downloads or generation, use **View Server Logs**.
The app log is `/content/Alexandria/colab-server.log`, with two rotated backups
(at most 3 MiB total). The viewer starts from the recent 64 KiB. Interrupting the
viewer leaves the server running. Startup failure also prints the recent tail;
download useful logs before the runtime disappears.

## Save and validate the result

Models download on first use and can occupy several GB each. Without persistent
storage they must be downloaded again in a new runtime. Download the finished
audiobook and preserve any scripts, voice assignments and logs you need before
ending the session. The optional Drive cache is not a complete project backup.

A successful install, HTTP startup check or short preview does not validate a
whole book, every voice mode or listening quality. Check that all requested
chunks finish, then export and play the result. Current Colab runtime findings
and full-book validation are tracked in [issue #1278](https://github.com/on22s/alexandria-audiobook2/issues/1278).

Use **Stop Server** when finished. It attempts server and notebook-owned Ollama
cleanup even if tunnel cleanup fails; reported cleanup errors need attention.
