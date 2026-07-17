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
        env: {
          ALEXANDRIA_PORT: "{{local.port}}",
          PYTHONUNBUFFERED: "1"
        },
        message: "python app.py",
        on: [{
          // Capture the URL when the server prints it
          event: "/(http:\\/\\/\\S+)/",
          done: true
        }, {
          // Stop visibly instead of leaving "Starting" waiting for a URL
          // after a Python/import/bind/startup failure.
          event: "/(ModuleNotFoundError|ImportError|Address already in use|Application startup failed|Traceback \\(most recent call last\\):)/i",
          break: true
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
