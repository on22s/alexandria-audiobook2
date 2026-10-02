module.exports = {
  run: [{
    uri: "launcher_lifecycle.js",
    method: "stop_writers"
  }, {
    method: "shell.run",
    params: {
      message: "git pull"
    }
  }, {
    method: "script.start",
    params: {
      uri: "install.js"
    }
  }]
}
