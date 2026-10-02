const whisperAssets = require("./whisper_assets.json")

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
    when: "{{!exists('app/env')}}",
    method: "shell.run",
    params: {
      path: "app",
      message: "uv venv --python 3.10 env"
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
    method: "script.start",
    params: {
      uri: "llama_cpp.js",
      params: { path: "app", venv: "env", constraints: "torch-constraints.txt" }
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
    method: "script.start",
    params: {
      uri: "llama_cpp.js",
      params: { path: ".", venv: "preparer_env", constraints: "preparer_env/torch-constraints.txt" }
    }
  }, {
    when: "{{!exists('whisper.cpp')}}",
    method: "shell.run",
    params: {
      message: [
        "git init whisper.cpp",
        `git -C whisper.cpp remote add origin ${whisperAssets.source_repo}`,
        `git -C whisper.cpp fetch --depth 1 origin ${whisperAssets.source_commit}`,
        "git -C whisper.cpp checkout --detach FETCH_HEAD"
      ]
    }
  }, {
    when: "{{!exists('models/whisper.cpp/ggml-small.en.bin')}}",
    method: "fs.download",
    params: {
      url: `https://huggingface.co/${whisperAssets.model_repo}/resolve/${whisperAssets.model_revision}/${whisperAssets.model_filename}`,
      dir: "models/whisper.cpp"
    }
  }, {
    method: "shell.run",
    params: {
      venv: "app/env",
      message: "python tools/verify_whisper_assets.py"
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
