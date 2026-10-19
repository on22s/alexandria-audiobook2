# Choose reference-audio channels

A stereo recording can hold different speakers on its two channels. Mixing can also cancel opposite-phase audio. In the clone import dialog or Dataset Preparer, choose **Mixed**, **Left** or **Right**, then **Preview first 10 seconds**. Playback shows the selected channel as mono. A missing right channel is refused; it never silently becomes mixed audio.

Preview uploads the original once and retains it privately for the later import/start. The default remains Mixed. Preview does not import a voice, start a dataset job or load a model. An accepted preview does not certify the rest of a long recording.

Clone import still requires an exact transcript, rights confirmation and a clean 3–30 second reference. Its existing silence/clipping checks apply to the selected channel. Each accepted reference receives a new immutable ID, with source channel count and chosen channel recorded in its manifest.

Single and batch preparation apply channel selection before downmixing/resampling, using that same signal for ASR and training clips. Changing the channel invalidates saved run identity; use a fresh run instead of resuming a different selection. Existing Mixed checkpoints retain their identity. The CLI option is `--audio-channel mix|left|right`.

Previews are capped at ten seconds, two simultaneous decoders and eight pending uploads. Original upload limits remain 512 MiB for clone references and 20 GiB for preparation. Temporary sources expire after 30 minutes: expired handles are refused, and expired files are reclaimed when another upload is staged. Closing a picker requests immediate cleanup; if the connection is lost, expiry cleanup still applies. Decoder children have a finite timeout and are retired on preview cancellation. Select the file again if its preview has expired or was consumed by preparation.

This feature does not edit existing voice references or change existing datasets. All processing stays in the configured AA2 host; remote access to that host has the same privacy implications as other AA2 uploads.
