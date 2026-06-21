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
    // Pin torch/torchaudio/torchvision/triton to exactly what torch.js just
    // installed for this machine's GPU. Without this, a transitive
    // dependency below (or any later manual pip/uv install) can silently
    // replace the GPU-specific build with a generic PyPI one - the install
    // succeeds, there's no error, GPU acceleration just quietly stops
    // working and everything runs on CPU instead.
    method: "shell.run",
    params: {
      venv: "env",
      path: "app",
      message: "uv pip freeze | grep -iE \"^(torch|torchvision|torchaudio|pytorch-triton)\" > torch-constraints.txt"
    }
  }, {
    method: "shell.run",
    params: {
      venv: "env",
      path: "app",
      env: {
        UV_CONSTRAINT: "torch-constraints.txt",
        PIP_CONSTRAINT: "torch-constraints.txt"
      },
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
