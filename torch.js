// The prebuilt optional attention wheels below target CPython 3.10.
const validateOptionalAttentionPython = "{{args && (args.sageattention || args.flashattention) ? 'python -c \"import sys; sys.exit(0 if sys.implementation.name == \\'cpython\\' and sys.version_info[:2] == (3, 10) else \\'Optional SageAttention/FlashAttention cp310 wheels require CPython 3.10; recreate the target venv with Python 3.10 before installing.\\')\"' : ''}}"

module.exports = {
  run: [
    // nvidia windows 
    {
      "when": "{{gpu === 'nvidia' && platform === 'win32'}}",
      "method": "shell.run",
      "params": {
        "venv": "{{args && args.venv ? args.venv : null}}",
        "path": "{{args && args.path ? args.path : '.'}}",
        "message": [
          validateOptionalAttentionPython,
          "uv pip install torch==2.7.0 torchvision==0.22.0 torchaudio==2.7.0 {{args && args.xformers ? 'xformers==0.0.30' : ''}} --index-url https://download.pytorch.org/whl/cu128 --force-reinstall --no-deps",
          "{{args && args.triton ? 'uv pip install triton-windows==3.3.1.post19' : ''}}",
          "{{args && args.sageattention ? 'uv pip install https://huggingface.co/cocktailpeanut/wheels/resolve/9756d71ff0d90075c1dc5c7e3108a97cc4479924/sageattention-2.1.1%2Bcu128torch2.7.1-cp310-cp310-win_amd64.whl#sha256=fde6a193b0a6101f3de8636819fbb0d4fcc53949c4845cbc041e9ccb19c56287' : ''}}",
          "{{args && args.flashattention ? 'uv pip install https://huggingface.co/cocktailpeanut/wheels/resolve/9756d71ff0d90075c1dc5c7e3108a97cc4479924/flash_attn-2.8.2%2Bcu128torch2.7-cp310-cp310-win_amd64.whl#sha256=945ae2b3f140683406f07c8064d474b394063453031602adc5f5773f1d2d10f5' : ''}}"
        ]
      },
      "next": null
    },
    // nvidia linux
    {
      "when": "{{gpu === 'nvidia' && platform === 'linux'}}",
      "method": "shell.run",
      "params": {
        "venv": "{{args && args.venv ? args.venv : null}}",
        "path": "{{args && args.path ? args.path : '.'}}",
        "message": [
          validateOptionalAttentionPython,
          "uv pip install torch==2.7.0 torchvision==0.22.0 torchaudio==2.7.0 {{args && args.xformers ? 'xformers==0.0.30' : ''}} --index-url https://download.pytorch.org/whl/cu128 --force-reinstall",
          "{{args && args.triton ? 'uv pip install triton' : ''}}",
          "{{args && args.sageattention ? 'uv pip install https://huggingface.co/cocktailpeanut/wheels/resolve/9756d71ff0d90075c1dc5c7e3108a97cc4479924/sageattention-2.1.1%2Bcu128torch2.7.1-cp310-cp310-linux_x86_64.whl#sha256=53982ea8e5c4ee0d7dc3ef17319fbc3857e851e67776ced853439af8721851a6' : ''}}",
          "{{args && args.flashattention ? 'uv pip install https://huggingface.co/cocktailpeanut/wheels/resolve/9756d71ff0d90075c1dc5c7e3108a97cc4479924/flash_attn-2.8.3%2Bcu128torch2.7-cp310-cp310-linux_x86_64.whl#sha256=6eb77a0b30963f1164df35dffdb347d299b0096220c6703cdc57a2e49208ba2c' : ''}}"
        ]
      },
      "next": null
    },
    // amd windows
    {
      "when": "{{gpu === 'amd' && platform === 'win32'}}",
      "method": "shell.run",
      "params": {
        "venv": "{{args && args.venv ? args.venv : null}}",
        "path": "{{args && args.path ? args.path : '.'}}",
        "message": "uv pip install torch torch-directml torchaudio torchvision numpy==1.26.4 --force-reinstall"
      },
      "next": null
    },
    // amd linux (rocm) — pinned to the benchmarked RDNA4 combo (RX 9070 XT / gfx1201).
    // Torch 2.10.0+ROCm 7.0 improved real Ryan and LoRA TTS batches by 21% and
    // 18% respectively over the previous 2.7.0+ROCm 6.3 pin (2026-07-14).
    {
      "when": "{{gpu === 'amd' && platform === 'linux'}}",
      "method": "shell.run",
      "params": {
        "venv": "{{args && args.venv ? args.venv : null}}",
        "path": "{{args && args.path ? args.path : '.'}}",
        "message": [
          "echo 'Installing benchmarked ROCm torch 2.10.0+rocm7.0 (RDNA4 / RX 9070 XT tested)'",
          "uv pip install torch==2.10.0 torchaudio==2.10.0 --index-url https://download.pytorch.org/whl/rocm7.0 --force-reinstall --no-deps",
          "uv pip install triton-rocm==3.6.0 --index-url https://download.pytorch.org/whl/rocm7.0"
        ]
      },
      "next": null
    },
    // apple silicon mac
    {
      "when": "{{platform === 'darwin' && arch === 'arm64'}}",
      "method": "shell.run",
      "params": {
        "venv": "{{args && args.venv ? args.venv : null}}",
        "path": "{{args && args.path ? args.path : '.'}}",
        "message": "uv pip install torch==2.7.0 torchvision==0.22.0 torchaudio==2.7.0 --index-url https://download.pytorch.org/whl/cpu --force-reinstall --no-deps"
      },
      "next": null
    },
    // intel mac
    {
      "when": "{{platform === 'darwin' && arch !== 'arm64'}}",
      "method": "shell.run",
      "params": {
        "venv": "{{args && args.venv ? args.venv : null}}",
        "path": "{{args && args.path ? args.path : '.'}}",
        "message": "uv pip install torch==2.2.2 torchvision==0.17.2 torchaudio==2.2.2 --index-url https://download.pytorch.org/whl/cpu --force-reinstall --no-deps"
      },
      "next": null
    },
    // cpu
    {
      "method": "shell.run",
      "params": {
        "venv": "{{args && args.venv ? args.venv : null}}",
        "path": "{{args && args.path ? args.path : '.'}}",
        "message": "uv pip install torch==2.7.0 torchvision==0.22.0 torchaudio==2.7.0  --index-url https://download.pytorch.org/whl/cpu --force-reinstall --no-deps"
      }
    }
  ]
}
