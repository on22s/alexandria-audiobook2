// Existing active-project reset boundary; reusable libraries are retained.
const resetPaths = [
  "annotated_script.json", "voices.json", "voice_config.json",
  "character_aliases.json", "state.json", "app/config.json", "chunks.json",
  "cloned_audiobook.mp3", "voicelines"
]

module.exports = {
  run: [{
    uri: "launcher_lifecycle.js",
    method: "stop_writers"
  }, {
    uri: "launcher_lifecycle.js",
    method: "recover_active_book"
  }, ...resetPaths.map(path => ({
    method: "fs.rm",
    params: { path }
  }))]
}
