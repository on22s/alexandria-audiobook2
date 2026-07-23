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
      message: "python -c \"import importlib.metadata as m, pathlib; wanted={'torch','torchvision','torchaudio','pytorch-triton','pytorch-triton-rocm','triton-rocm'}; rows=[f'{d.metadata[\\\"Name\\\"]}=={d.version}' for d in m.distributions() if d.metadata[\\\"Name\\\"].lower() in wanted]; pathlib.Path('torch-constraints.txt').write_text('\\n'.join(rows)+'\\n')\""
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
    // The preparer has a separate ML environment so adding pyannote cannot
    // replace Voice Lab's known-working torch/ROCm stack.
    when: "{{platform === 'linux' && gpu === 'amd'}}",
    method: "script.start",
    params: {
      uri: "torch.js",
      params: {
        path: ".",
        venv: "preparer_env"
      }
    }
  }, {
    when: "{{platform === 'linux' && gpu === 'amd'}}",
    method: "shell.run",
    params: {
      venv: "preparer_env",
      message: "python -c \"import importlib.metadata as m, pathlib; wanted={'torch','torchvision','torchaudio','pytorch-triton','pytorch-triton-rocm','triton-rocm'}; rows=[f'{d.metadata[\\\"Name\\\"]}=={d.version}' for d in m.distributions() if d.metadata[\\\"Name\\\"].lower() in wanted]; pathlib.Path('preparer_env/torch-constraints.txt').write_text('\\n'.join(rows)+'\\n')\""
    }
  }, {
    when: "{{platform === 'linux' && gpu === 'amd'}}",
    method: "shell.run",
    params: {
      venv: "preparer_env",
      env: {
        UV_CONSTRAINT: "preparer_env/torch-constraints.txt",
        PIP_CONSTRAINT: "preparer_env/torch-constraints.txt"
      },
      message: [
        "uv pip install -r requirements-preparer.txt",
        "uv pip install -r requirements-diarization.txt"
      ]
    }
  }, {
    when: "{{platform === 'linux' && gpu === 'amd'}}",
    method: "shell.run",
    params: {
      venv: "preparer_env",
      env: {
        UV_CONSTRAINT: "preparer_env/torch-constraints.txt",
        PIP_CONSTRAINT: "preparer_env/torch-constraints.txt"
      },
      message: "CMAKE_ARGS=\"-DGGML_HIP=ON -DAMDGPU_TARGETS=$(rocminfo | awk '/Name: *gfx/{print $2; exit}')\" uv pip install llama-cpp-python==0.3.23 --no-binary llama-cpp-python"
    }
  }, {
    when: "{{!exists('whisper.cpp')}}",
    method: "shell.run",
    params: {
      message: "git clone --depth 1 --branch v1.9.1 https://github.com/ggml-org/whisper.cpp whisper.cpp"
    }
  }, {
    when: "{{!exists('models/whisper.cpp/ggml-small.en.bin')}}",
    method: "fs.download",
    params: {
      url: "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin",
      dir: "models/whisper.cpp"
    }
  }, {
    when: "{{platform === 'linux' && gpu === 'amd'}}",
    method: "shell.run",
    params: {
      path: "whisper.cpp",
      message: "cmake -S . -B build -DGGML_HIP=ON -DAMDGPU_TARGETS=\"$(rocminfo | awk '/Name: *gfx/{print $2; exit}')\""
    }
  }, {
    when: "{{gpu === 'nvidia'}}",
    method: "shell.run",
    params: {
      path: "whisper.cpp",
      message: "cmake -S . -B build -DGGML_CUDA=ON"
    }
  }, {
    when: "{{platform === 'darwin'}}",
    method: "shell.run",
    params: {
      path: "whisper.cpp",
      message: "cmake -S . -B build -DGGML_METAL=ON"
    }
  }, {
    when: "{{!(platform === 'linux' && gpu === 'amd') && gpu !== 'nvidia' && platform !== 'darwin'}}",
    method: "shell.run",
    params: {
      path: "whisper.cpp",
      message: "cmake -S . -B build"
    }
  }, {
    method: "shell.run",
    params: {
      path: "whisper.cpp",
      message: "cmake --build build --config Release -j"
    }
  }, {
    method: "notify",
    params: {
      html: "Installation Complete! Click 'Start' to launch the application."
    }
  }]
}
