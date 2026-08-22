# Speech-quality metric audit — 2026-08-22

## Decision

Do not use NISQA's overall MOS or NISQA-TTS score as an automatic release
gate for this project. On the 12 Satella clips with owner ratings, neither
score tracked the listening judgments. NISQA's noisiness dimension is useful
as a candidate triage signal, but the sample is too small to promote it to a
gate.

## Local validation

The official NISQA repository was evaluated at commit
`fe84f0f252abec382b24367d5b22498a7ce34dbb` on CPU, without changing the
project environment. Model hashes:

- `nisqa.tar`: `7ec4cf937514dd3f8860b21e66fabd8ca87a168572675ef8d979c4c4ad2e805c`
- `nisqa_tts.tar`: `c556a954cd9536360b4fe6f524ad432816509cd22f436f345b281a7851360656`

Spearman correlation against the 12 blinded owner quality ratings:

| Prediction | rho | p |
| --- | ---: | ---: |
| NISQA multidimensional MOS | 0.296 | 0.350 |
| Noisiness | 0.644 | 0.024 |
| Discontinuity | 0.363 | 0.246 |
| Coloration | 0.407 | 0.189 |
| Loudness | 0.237 | 0.458 |
| NISQA-TTS MOS | -0.178 | 0.580 |

The one clip explicitly described as cracking and rated 2/5 was ranked worst
by multidimensional MOS and noisiness. NISQA-TTS instead scored it relatively
well and scored a human-rated 5/5 clip poorly. This is why the aggregate
TTS score is rejected for the current use case.

## Web evidence and fit

- Microsoft's DNSMOS predicts P.835 speech, background, and overall quality
  for noise-suppression systems. That task is not a close match for synthetic
  voice cracking or character-voice naturalness.
- NISQA exposes noisiness, coloration, discontinuity, and loudness dimensions,
  making it the most relevant inexpensive diagnostic tested here.
- TTSDS2 reports stronger cross-domain agreement with subjective TTS ratings,
  but it is a distribution-level evaluator requiring reference/noise pools;
  it is not a per-clip crack locator.
- Published work demonstrates score-preserving attacks against UTMOS. This
  reinforces the requirement that any learned metric be validated against the
  owner's listening judgments before it can gate releases.

## Next validation threshold

Keep noisiness as a ranking/triage column only. Recompute correlation after
the new 30-clip Satella test is rated. Do not automate acceptance unless the
relationship replicates on that independent set and obvious defects are not
assigned high scores.

## Independent replication result

The preregistered 30-clip Satella test was rated later on 2026-08-22. The
ratings matched the frozen source hash and had mean quality 4.633/5. NISQA
noisiness correlated only `rho = 0.219` (`p = 0.245`) with those ratings.
None of the other dimensions was significant either; their correlations
ranged from 0.206 to 0.308. The earlier noisiness result therefore did not
replicate. Reject NISQA, including its noisiness dimension, as a project gate
or dependable triage ranker on the evidence currently available.

## Primary sources

- https://github.com/microsoft/DNS-Challenge
- https://github.com/microsoft/DNS-Challenge/blob/master/DNSMOS/README.md
- https://github.com/gabrielmittag/NISQA
- https://arxiv.org/abs/2104.09494
- https://github.com/ttsds/ttsds
- https://arxiv.org/abs/2506.19441
- https://arxiv.org/abs/2606.31105
