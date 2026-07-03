module.exports = {
  daemon: true,
  run: [
    {
      // Resolve the next free port ONCE and store it, so every use below refers
      // to the same value ({{port}} can resolve differently on each expansion).
      method: "local.set",
      params: {
        appport: "{{port}}"
      }
    },
    {
      method: "shell.run",
      params: {
        venv: "env",
        path: "app",
        // Bind to the captured free port instead of the app's hardcoded 4200
        // default, and set CORS_ORIGINS to match so same-origin requests still work.
        env: {
          ALEXANDRIA_PORT: "{{local.appport}}",
          CORS_ORIGINS: "http://127.0.0.1:{{local.appport}},http://localhost:{{local.appport}}"
        },
        message: "python app.py",
        on: [{
          // Capture the URL when the server prints it
          event: "/(http:\\/\\/[0-9.:]+)/",
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
