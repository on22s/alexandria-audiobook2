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
      method: "shell.run",
      params: {
        venv: "../alexandria-audiobook.git/app/env",
        path: ".",
        message: "python -m llama_cpp.server --model {{args.model}} --host 127.0.0.1 --port {{local.port}} --n_gpu_layers -1 --n_ctx 8192",
        on: [{
          event: "/(http:\\/\\/[0-9.:]+)/",
          done: true
        }]
      }
    },
    {
      method: "local.set",
      params: {
        llm_url: "{{input.event[1]}}/v1"
      }
    },
    {
      method: "json.set",
      params: {
        "app/config.json": {
          "llm.base_url": "{{local.llm_url}}"
        }
      }
    }
  ]
}
