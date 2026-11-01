# Provenance — built-in listening fixtures

All three embedded WAV fixtures were constructed from one public LibriVox recording.

- Source: https://www.archive.org/download/short_poetry_001_librivox/abou_hunt_py_64kb.mp3
- Source MP3 SHA-256: `ad060a5cbd1ea90024967af226f6bc8e5d0d9905660a5a506720604f13a1d759`
- License scope: the official LibriVox catalog records public-domain status in the USA. No worldwide-rights claim is made. The recording is not private voice material.

Fixture WAV hashes (the bytes embedded in `listening-lab.html`, which `verify-lab.cjs` re-checks):

| Fixture | WAV bytes | WAV SHA-256 |
| --- | ---: | --- |
| `pd01-pristine` | 1440044 | `820f268f6823ff400705d2fe808a1efbb2331566db3728f7b467da008f113912` |
| `pd01-intentional` | 1620044 | `09b46fcbf6c8c438fcac0defbdb9b9be76dd61fd30359bbb785d3c29f8a2c527` |
| `pd01-defect` | 2088044 | `0c3ec1d9ed84c2e17e184fdeaa54aed4416b4738ee1f0b5de5e9628226aa460e` |

In each fixture's `construction` record, `wavSha256` and `wavBytes` describe the embedded WAV, while `sourceMp3Sha256` names the public source audio. The source MP3 hash was recorded when the fixtures were built; it is metadata and is not re-derived from the embedded WAV bytes.

Construction annotations are editorial labels frozen before this tool was packaged. They are not human listening results, authorial intent, transcripts or quotations. No acceptance, pilot or publication claim is made.
