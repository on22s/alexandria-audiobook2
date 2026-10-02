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
      // cryptic ModuleNotFoundError) when this app's venv isn't installed.
      method: "log",
      params: {
        raw: "ERROR: LLM venv 'app/env' not found. Run this project's Install action first; it provides llama-cpp-python for the LLM server."
      },
      when: "{{!exists('app/env')}}"
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
      when: "{{!exists('app/env') || !exists(args.model)}}"
    },
    {
      method: "shell.run",
      params: {
        venv: "app/env",
        path: ".",
        message: {
          _: ["python", "-m", "llama_cpp.server", "--model={{args.model}}",
              "--host", "127.0.0.1", "--port", "{{local.port}}",
              "--n_gpu_layers", "-1", "--n_ctx", "8192"]
        },
        on: [{
          event: "/(http:\\/\\/[0-9.:]+)/",
          done: true
        }]
      },
      when: "{{exists('app/env') && exists(args.model)}}"
    },
    {
      method: "local.set",
      params: {
        llm_url: "{{input.event[1]}}/v1"
      },
      when: "{{exists('app/env') && exists(args.model)}}"
    },
    {
      method: "json.set",
      params: {
        "app/config.json": {
          "llm_mode": "local",
          "llm.base_url": "{{local.llm_url}}",
          "llm.model_name": "{{args.model}}",
          "llm_local.base_url": "{{local.llm_url}}",
          "llm_local.model_name": "{{args.model}}"
        }
      },
      when: "{{exists('app/env') && exists(args.model)}}"
    }
  ]
}
