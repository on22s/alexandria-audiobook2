module.exports = {
  daemon: true,
  run: [
    {
      method: "local.set",
      params: {
        port: "{{port}}"
      }
    },
    {
      // Fail clearly (instead of auto-creating an empty venv and hitting a
      // cryptic ModuleNotFoundError) when the sibling repo's venv — which
      // provides llama-cpp-python — isn't installed.
      method: "log",
      params: {
        raw: "ERROR: LLM venv '../alexandria-audiobook.git/app/env' not found. Install the sibling alexandria-audiobook repo first (it provides llama-cpp-python); the LLM server can't start without it."
      },
      when: "{{!exists('../alexandria-audiobook.git/app/env')}}"
    },
    {
      method: "log",
      params: {
        raw: "ERROR: model file '{{args.model}}' not found in the project root. GGUF models are gitignored and not auto-downloaded — place the file here first."
      },
      when: "{{!exists(args.model)}}"
    },
    {
      method: "script.return",
      params: {
        error: "LLM server prerequisites are missing; see the preceding error."
      },
      when: "{{!exists('../alexandria-audiobook.git/app/env') || !exists(args.model)}}"
    },
    {
      method: "shell.run",
      params: {
        venv: "../alexandria-audiobook.git/app/env",
        path: ".",
        message: "python -m llama_cpp.server --model \"{{args.model}}\" --host 127.0.0.1 --port {{local.port}} --n_gpu_layers -1 --n_ctx 8192",
        on: [{
          event: "/(http:\\/\\/[0-9.:]+)/",
          done: true
        }]
      },
      when: "{{exists('../alexandria-audiobook.git/app/env') && exists(args.model)}}"
    },
    {
      method: "local.set",
      params: {
        llm_url: "{{input.event[1]}}/v1"
      },
      when: "{{exists('../alexandria-audiobook.git/app/env') && exists(args.model)}}"
    },
    {
      method: "json.set",
      params: {
        "app/config.json": {
          "llm.base_url": "{{local.llm_url}}"
        }
      },
      when: "{{exists('../alexandria-audiobook.git/app/env') && exists(args.model)}}"
    }
  ]
}
