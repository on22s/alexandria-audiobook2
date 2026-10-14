const path = require("path")
const fs = require("fs")
const { execFile } = require("child_process")
const { promisify } = require("util")
const runFile = promisify(execFile)

// Include child installers too: their shells have independent script groups.
const writerScripts = [
  "start.js", "start_llm.js", "run_stage4_checkpoint.js",
  "install.js", "torch.js", "llama_cpp.js", "reset.js", "update.js"
]

class LauncherLifecycle {
  get_writer_scripts(caller) {
    if (!["reset.js", "update.js"].includes(caller)) {
      throw new Error("Launcher shutdown requires a reset or update caller")
    }
    return writerScripts.filter(name => name !== caller)
  }

  get_caller(req) {
    const parentPath = req.parent && req.parent.path
    if (!parentPath || path.dirname(path.resolve(parentPath)) !== __dirname) {
      throw new Error("Launcher shutdown must run from this checkout")
    }
    return path.basename(parentPath)
  }

  async stop_writers(req, ondata, kernel) {
    const caller = this.get_caller(req)
    for (const name of this.get_writer_scripts(caller)) {
      // script.stop currently starts this promise without awaiting it. Await
      // the kernel completion so a shutdown failure prevents file mutation.
      await kernel.api.stop({ params: { uri: path.join(__dirname, name) } })
      ondata({ raw: `Stopped ${name}\r\n` })
    }
  }

  async recover_active_book(req, ondata, kernel) {
    if (this.get_caller(req) !== "reset.js") {
      throw new Error("Active-book recovery requires a reset caller")
    }
    const environmentPython = path.join(__dirname, "app", "env",
      process.platform === "win32" ? "Scripts/python.exe" : "bin/python")
    const python = fs.existsSync(environmentPython) ? environmentPython : kernel.which("python")
    if (!python) {
      throw new Error("Python is required to settle active-book recovery before Reset")
    }
    const code = [
      "import sys",
      "from book_state_transaction import ensure_book_state",
      "with ensure_book_state(sys.argv[1]):",
      "    pass"
    ].join("\n")
    // Await successful recovery; a corrupt journal or failed child must stop
    // Reset before it deletes active files or their recovery evidence.
    await runFile(python, ["-c", code, __dirname], {
      cwd: path.join(__dirname, "app"), timeout: 15000, windowsHide: true
    })
    ondata({ raw: "Active-book recovery settled.\r\n" })
  }
}

module.exports = LauncherLifecycle
