// Existing active-project reset boundary; the canonical book artifact set and
// its guarded removal live in app/reset_artifact_roots.py. Shared libraries,
// uploads, adapters and take history are deliberately outside that set.
module.exports = {
  run: [{
    uri: "launcher_lifecycle.js",
    method: "stop_writers"
  }, {
    uri: "launcher_lifecycle.js",
    method: "recover_active_book"
  }, {
    uri: "launcher_lifecycle.js",
    method: "clear_active_book"
  }]
}
