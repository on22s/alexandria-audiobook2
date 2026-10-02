const path = require("path")

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

  async stop_writers(req, ondata, kernel) {
    const parentPath = req.parent && req.parent.path
    if (!parentPath || path.dirname(path.resolve(parentPath)) !== __dirname) {
      throw new Error("Launcher shutdown must run from this checkout")
    }
    const caller = path.basename(parentPath)
    for (const name of this.get_writer_scripts(caller)) {
      // script.stop currently starts this promise without awaiting it. Await
      // the kernel completion so a shutdown failure prevents file mutation.
      await kernel.api.stop({ params: { uri: path.join(__dirname, name) } })
      ondata({ raw: `Stopped ${name}\r\n` })
    }
  }
}

module.exports = LauncherLifecycle
