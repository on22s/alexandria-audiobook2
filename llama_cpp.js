// Backend selection is shared by the app and the isolated AMD preparer.
const command = "uv pip install llama-cpp-python[server]==0.3.23 --no-binary llama-cpp-python"
const step = (when, env, message = command) => ({
  when,
  method: "shell.run",
  params: {
    path: "{{args.path}}",
    venv: "{{args.venv}}",
    env: {
      UV_CONSTRAINT: "{{args.constraints}}",
      PIP_CONSTRAINT: "{{args.constraints}}",
      ...env
    },
    message
  },
  next: null
})

module.exports = {
  run: [
    step("{{platform === 'linux' && gpu === 'amd'}}", {},
      `CMAKE_ARGS="-DGGML_HIP=ON -DAMDGPU_TARGETS=$(rocminfo | awk '/Name: *gfx/{print $2; exit}')" ${command}`),
    step("{{gpu === 'nvidia'}}", { CMAKE_ARGS: "-DGGML_CUDA=ON" }),
    step("{{platform === 'darwin' && gpu !== 'nvidia'}}", { CMAKE_ARGS: "-DGGML_METAL=ON" }),
    step("{{!(platform === 'linux' && gpu === 'amd') && gpu !== 'nvidia' && platform !== 'darwin'}}",
      { CMAKE_ARGS: "-DGGML_HIP=OFF -DGGML_CUDA=OFF -DGGML_METAL=OFF" })
  ]
}
