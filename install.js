module.exports = {
  requires: {
    bundle: "ai"
  },
  run: [{
    method: "shell.run",
    params: {
      message: "uv cache clean"
    }
  }, {
    method: "shell.run",
    params: {
      path: "app",
      message: "python -m venv env"
    }
  }, {
    // Install the platform-correct torch FIRST so transitive deps below
    // (peft, qwen-tts) see it already satisfied and don't pull a CUDA build.
    method: "script.start",
    params: {
      uri: "torch.js",
      params: {
        path: "app",
        venv: "env",
        flashattention: true
      }
    }
  }, {
    method: "shell.run",
    params: {
      venv: "env",
      path: "app",
      message: [
        "uv pip uninstall google-genai",
        "uv pip install -r requirements.txt",
        "uv pip install qwen-tts==0.1.1"
      ]
    }
  }, {
    method: "notify",
    params: {
      html: "Installation Complete! Click 'Start' to launch the application."
    }
  }]
}
