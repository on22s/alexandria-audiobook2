module.exports = {
  daemon: true,
  run: [
    {
      method: "local.set",
      params: { port: "{{port}}" }
    },
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        env: { ALEXANDRIA_PORT: "{{local.port}}" },
        message: "python app.py",
        on: [{
          // Capture the URL when the server prints it
          event: "/(http:\\/\\/\\S+)/",
          done: true
        }]
      }
    },
    {
      // Set the local variable 'url' for pinokio.js to display "Open Web UI"
      method: "local.set",
      params: {
        url: "{{input.event[1]}}"
      }
    }
  ]
}
