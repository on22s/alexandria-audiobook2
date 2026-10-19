# Optional export mastering

In Result, expand **Optional export mastering**. It is off by default. Enable it before the next merged MP3, chapter MP3/WAV or M4B export. The starting targets are -18 LUFS integrated loudness and -2 dBTP true peak; permitted targets are -24 to -14 LUFS and -9 to -1 dBTP. These are processing choices, not a distribution compliance certificate.

Mastering analyzes each assembled deliverable, applies measured FFmpeg loudnorm processing, then measures the final encoded file. MP3/AAC encoding can change the achieved loudness or peak. Check the actual results and listen. FFmpeg may use linear or dynamic processing; the report states which. Source TTS clips and Audacity tracks are unchanged.

After completion, choose **Show verified export measurements**. Refresh Editor first if the current book identity is unavailable. Reports show integrated loudness, loudness range and true peak before/after, targets, mode and the output SHA-256. The application verifies current file bytes and book identity. Reports for merged MP3/M4B are kept for the app session; chapter manifests retain per-file processing information. Changed-only chapter reuse includes mastering settings and verifies measured output identity. Switching books or starting another export clears displayed results.

The extra analysis/encoding passes are CPU work and use temporary disk space. They retain existing owned-task/cancellation behavior. Silence, unreadable audio, non-finite measurements or missing FFmpeg support fail visibly; a prior good output remains when processing fails or is cancelled. Each new FFmpeg analysis/processing phase has a finite 600-second deadline; long files can reach that limit. No whole-book PCM allocation is added by the mastering helper, though existing assembly paths retain their current memory behavior.

FFmpeg reference: https://ffmpeg.org/ffmpeg-filters.html#loudnorm
