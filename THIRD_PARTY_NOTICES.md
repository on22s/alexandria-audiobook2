# Third-Party Notices

Alexandria Audiobook2 incorporates material from the projects below. Each entry
records what was taken and reproduces the licence terms that require it.

Adding an entry is not optional. MIT and Apache-2.0 both require the copyright
notice to travel with the code; a comment in the source naming the project is
good practice but does not by itself satisfy either licence. If you copy or
closely adapt third-party code, add it here in the same commit.

Projects with **no licence file grant no rights at all** and must not be copied
from, however permissive they look. Reading them for ideas is fine; reproducing
their code is not.

---

## p0n1/epub_to_audiobook

- Source: https://github.com/p0n1/epub_to_audiobook
- Licence: MIT
- Used in: `app/experiments/quote_aware_chunking.py`

The priority-ordered list of split points in `PUNCT_PRIORITY` is adapted from
that project's `split_long_sentence`. Two properties were the reason to adopt it
rather than write our own: CJK sentence-enders are tried before English ones, so
a language without spaces never falls through to a hard character slice; and
closing brackets and quote marks are themselves split points, so a cut lands
after a quotation closes rather than inside it.

```
MIT License

Copyright (c) 2023 p0n1

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## buddies/alexandria-audiobook (fork of Finrandojin/alexandria-audiobook)

- Source: https://github.com/buddies/alexandria-audiobook — `app/instruct_utils.py`
  and [`VOICE_REFERENCE.md`](docs/guides/VOICE_REFERENCE.md) (Xiao Zhang, 2026-09-16..18)
- Licence: MIT (the fork carries upstream's MIT licence file, © 2026 Finrandojin;
  the ported material is the fork author's)
- Used in: `app/experiments/instruct_lexicon.py` (the vocabulary lists, kept
  verbatim), `app/experiments/instruct_audit.py` and
  `app/experiments/custom_voice_instruct_drift.py` (built on them)

The timbre / register / identity, emotion, delivery and pacing term lists,
and the rule that Section-I (timbre) terms belong in a constant character
style rather than a per-line instruct, are ported as written so that a
finding here means what it meant in their measurement. The finding itself
(an explicit rate instruction lengthens CustomVoice takes by +17.9%) is
theirs; ours are in `instruct_audit_20260918.json` and
`custom_voice_instruct_drift__ryan_arc1_20260918.json`.

```
MIT License

Copyright (c) 2026 Finrandojin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## cjdell/alexandria-audiobook (fork of Finrandojin/alexandria-audiobook)

- Source: https://github.com/cjdell/alexandria-audiobook — commit `adb3a66`
- Licence: MIT (the fork carries upstream's licence file, © 2026 Finrandojin)
- Used in: `app/device_utils.py` (`compute_dtype`), reached from every model-load
  site in `app/tts.py` (#573, 2026-09-14)

The finding and the fix are theirs: bf16 HIP GEMMs segfault inside
`torch.Linear` on AMD APU iGPUs (gfx1035 — Radeon 660M/680M — on ROCm 6.4
wheels), so those parts load in fp32. Rewritten around `gcnArchName` with our
own tests, but the change is a port of theirs and is recorded as such.

```
MIT License

Copyright (c) 2026 Finrandojin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## XinchaoGou/alexandria-audiobook (fork of Finrandojin/alexandria-audiobook)

- Source: https://github.com/XinchaoGou/alexandria-audiobook — `llm_utils.py`
- Licence: MIT (the fork carries upstream's licence file, © 2026 Finrandojin)
- Used in: `app/llm_provider.py` — `adapt_request_for_reasoning_model` (OpenAI
  reasoning models want `max_completion_tokens` and reject sampling keys with
  reasoning on) and API keys by environment reference (`env:NAME`, `${NAME}`)
  in `make_llm_client` (#574, 2026-09-14). Done at our single dispatch point
  rather than by monkey-patching the client, but both behaviours are ports.

```
MIT License

Copyright (c) 2026 Finrandojin

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

---

## Ideas adopted without code

These projects shaped a design but no code was copied from them. No licence
obligation attaches to an idea; they are recorded because the provenance is
worth keeping, and because if we later lift code from one of them, the entry
above is where the obligation gets written down.

| Project | Licence | What it prompted |
| --- | --- | --- |
| [zeropointnine/tts-audiobook-tool](https://github.com/zeropointnine/tts-audiobook-tool) | MIT | Transcribe-back validation of generated audio: word-error alignment against the source with a length-scaled fail threshold, plus truncation detection. Implemented independently in `app/experiments/tts_output_validation.py`. |
| [DrewThomasson/VoxNovel](https://github.com/DrewThomasson/VoxNovel) | MIT | That BookNLP is the field-standard baseline for quotation speaker attribution, and that our ledger had never measured against it. See `app/experiments/booknlp_baseline.py`. |
| [nazdridoy/kokoro-tts](https://github.com/nazdridoy/kokoro-tts) | MIT | Voice blending by weighted interpolation as a way to widen a cast's voice pool. See `app/experiments/voice_blending.py`. |
| [WhiskeyCoder/Qwen3-Audiobook-Converter](https://github.com/WhiskeyCoder/Qwen3-Audiobook-Converter) | MIT | An independent choice of a 200-character chunk cap for Qwen3-TTS voice cloning, matching the cap our own external path enforces and our local path does not. |
| [XinchaoGou/alexandria-audiobook](https://github.com/XinchaoGou/alexandria-audiobook) | MIT | The external-TTS endpoint pool (#532): one client per server, round-robin, concurrent batches. We kept the Gradio per-line API rather than their batch server. Code ports from the same fork are the entry above. |
| [Darkkingwill/alexandria-audiobook](https://github.com/Darkkingwill/alexandria-audiobook) | MIT | Five UX fixes rebuilt on our own patterns, not merged (#534, 2026-09-11): cancellable merge/export, and the Result-tab affordances around it. |
| [aayushnaphade/alexandria-audiobook](https://github.com/aayushnaphade/alexandria-audiobook) | MIT | Reviewed 2026-09-18 alongside the buddies fork; nothing copied. |
| Michel, Epure, Cerisara — the quotation-attribution prompt formulation behind `michel`, `michel2`, `michel2_full`, `michel2_shot` (`app/attribution_prompt_variants.py`) | paper | The prompt shape that won on every base measured (RECIPES §"Prompt variants × bases"); our variants are re-writings of the idea, no text copied. |
| [CosyVoice](https://github.com/FunAudioLLM/CosyVoice) | Apache-2.0 | Design notes only — `docs/LEARN_FROM_COSYVOICE.md`. |

### Consulted and rejected

Recorded so the same repositories are not re-reviewed from scratch.

| Project | Reason |
| --- | --- |
| sudonitin/Audio-book-generator | No licence file — no grant of rights. |
| aedocw/epub2tts-chatterbox | No licence file — no grant of rights. |
| devsapp/ai-audiobook-flow | No licence file — no grant of rights. |
| OmniVoice-Studio, QuickPiperAudiobook | AGPL-3.0; copyleft reach is a poor fit for a shipped application. |
| transitive-bullshit/kindle-ai-export | Solves acquisition, not narration; drives the Kindle web reader with Playwright, which raises terms-of-service questions for no technical gain. |
| ReadAlongs/Studio | Licence is unresolved (`NOASSERTION`), and our preparer alignment already works. |
| aedocw/epub2tts, DrewThomasson/ebook2audiobookpiper-tts, pravinyo/AudioBook | Nothing structurally absent from this project. |

---

## Research and industry practice consulted

No code or text is taken from these; they are recorded because they changed
design decisions, and a decision whose reasoning cannot be traced is one that
gets re-argued from scratch. Where a finding contradicted something already
built, that is noted - those are the entries worth keeping.

### Audiobook casting

- [How Professional Audiobook Narrators Voice Characters — Jay Myers](https://www.jaymyersvoiceover.com/blog-ideas/how-audiobook-narrators-make-characters-sound-distinct-believable-and-consistent)
- [How to Give Every Character a Different Voice in Your Audiobook — Audie](https://www.audie.ai/how-to-give-every-character-a-different-voice-in-your-audiobook)
- [Narrator Voice vs Character Voices — Audie](https://www.audie.ai/narrator-voice-vs-character-voices-getting-dialogue-right)
- [Character Voices in Audiobooks: 7 Key Considerations — Malk Williams](https://www.linkedin.com/pulse/character-voices-audiobooks-7-key-considerations-malk-williams)
- [Creating Distinct Character Voices for Audiobooks — Vois](https://vois.so/blog/character-voices-audiobooks)

Two findings changed the design:

**4-8 distinct character voices plus a narrator is the working maximum; more
risk listener confusion.** This CONTRADICTS the direction the voice-blending
experiment was heading. `voice_blending.py` measured that blending an 8-voice
pool reaches 148 identities and framed that as the prize. By professional
practice 148 is far past the point where a listener stops tracking who is
speaking. The arithmetic in that experiment stands; the goal it implied does
not.

**Contrast matters between characters who SHARE SCENES, not globally.**
Narrators pick voices so that characters appearing together are
distinguishable. Measured on the live book: 69% of character pairs never
co-occur within a 20-line window, and a greedy colouring of the co-occurrence
graph needs only 9 voices for 32 characters. So `audible_errors.py`'s
assumption - that any two characters sharing a voice is a confusion - is
stricter than practice. Sharing is fine when the characters never meet.

### Anime and dub casting

- [Answerman — Why Do Dubs Cast Men As Boy Characters, while Japan Casts Women? (ANN)](https://www.animenewsnetwork.com/answerman/2018-08-22/.135762)
- [Cross-Dressing Voices / Anime (TV Tropes)](https://tvtropes.org/pmwiki/pmwiki.php/CrossDressingVoices/Anime)
- [How Are English Dub Voice Actors Cast? (ANN)](https://www.animenewsnetwork.com/answerman/2015-09-11/.92806)
- [Anime Voice Acting: From Start to Finish — Bunny Studio](https://bunnystudio.com/blog/voice-acting-anime-from-start-to-finish/)

Adult women routinely voice boys and young men in Japanese originals - Naruto,
Edward Elric, Luffy, Ash - because few adult men sound convincingly young and
child actors bring scheduling and content limits. This matters for a corpus of
translated light novels: an `_f_` adapter on a teenage boy is CONVENTION, not
an error. It corrected a conclusion about Subaru, where the real mismatch was
the `50s` age band rather than the `f` tag.

### Voice and perceived sexual orientation

- [The effect of sexual orientation on voice acoustic properties (Frontiers in Psychology, 2024)](https://www.frontiersin.org/journals/psychology/articles/10.3389/fpsyg.2024.1412372/full)
- [Speech Acoustic Features: Gay Men, Heterosexual Men, and Heterosexual Women (PMC)](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7497419/)
- [Do gay-sounding men speak like women? — Smyth & Rogers (TWPL)](https://twpl.library.utoronto.ca/index.php/twpl/article/download/6168/3157/0)
- ["Do I Sound Straight?" — Acoustic Correlates (JSLHR)](https://pubs.asha.org/doi/abs/10.1044/2018_JSLHR-S-17-0125)

Checked because the folk model - "sounds more feminine, so higher pitch" - was
about to be encoded. The literature does not support it. Findings on mean pitch
are mixed and sometimes reversed; the reliable correlates are ARTICULATORY,
chiefly /s/ with higher spectral peaks and longer duration, longer voice-onset
times, and more alveolar /l/. One study reports sibilant variation as socially
rather than anatomically determined. Nothing here is representable by an
`_f_`/`_m_` tag, which is further evidence that the sex tag is the wrong
primary key for casting.

---

## Speech corpora for ground-truth voice evaluation

Added 2026-08-05, before use rather than after. These provide something no
measurement in this repo previously had: a human reading the *same line* a
model was asked to generate, so voice similarity can be scored against ground
truth instead of against the reference clip that was also the prompt.

### LJSpeech (The LJ Speech Dataset)

- <https://keithito.com/LJ-Speech-Dataset/>
- 13,100 clips, ~24 hours, a single speaker, 22.05 kHz.
- **Public domain.** The recordings come from LibriVox and the texts from
  seven non-fiction works in the public domain via Project Gutenberg.
- Used as the primary single-narrator corpus: one speaker across many hours is
  exactly the shape a voice LoRA is trained for.
- Note for anyone reusing our numbers: at 22.05 kHz it needs resampling to the
  24 kHz this project generates at. We resample the HUMAN audio once, up
  front, so both sides of every comparison share a rate.

### LibriTTS-R

- <https://www.openslr.org/141/> — Koizumi et al., 2023.
- 585 hours, 2,456 speakers, natively 24 kHz; a speech-restored version of
  LibriTTS with identical content.
- **CC BY 4.0.**
- Used for multi-speaker work and for the "different narrator" floor anchor.
  Native 24 kHz matches what `tts.py` writes, so no resampling confound.

### LibriTTS

- <https://www.openslr.org/60/> — Zen et al., Interspeech 2019.
- **CC BY 4.0.** Derived from LibriSpeech's original LibriVox mp3 audio and
  Project Gutenberg texts.
- Superseded by LibriTTS-R for audio quality; recorded here because -R is
  derived from it and the alignment work is theirs.

### LibriVox

- <https://librivox.org> — **public domain.**
- The upstream source of all of the above, and the fallback if a specific
  narrator, book or language is wanted that the derived corpora do not cover.

### Project Gutenberg

- <https://www.gutenberg.org> — the text side of every corpus above.

None of this audio is redistributed by us. The corpora are downloaded to
`ab_test_runtime/corpora/`, which is gitignored; `PROVENANCE.md` there records
what to fetch and from where.

### Non-English speech corpora (candidates, not yet used)

Recorded 2026-08-05 while answering "is there anything open source, not
necessarily English". Listed with licences so the choice is auditable before
any of it is downloaded.

**[Kokoro Speech Dataset](https://mozilladatacollective.com/datasets/cmmknsho4014wmf087kvq5rc6)**
— the closest match to what this project actually generates.

- 43,253 clips, a single speaker, reading 14 novels.
- **Public domain**: Aozora Bunko texts + LibriVox recordings, both PD.
- Japanese, single-speaker, audiobook register - the same shape as LJSpeech,
  so `ljspeech_prepare.py` / `ljspeech_build.py` apply with a different root.
- **We already hold the text side.** `ab_test_runtime/corpora/aozora/kokoro.txt`
  is Natsume Sōseki's こころ, downloaded for the Japanese quote-robustness work.
  This corpus is LibriVox audio of that same text.

**[Multilingual LibriSpeech (MLS)](https://arxiv.org/pdf/2012.03411)** — **CC0**,
public domain. English, German, Dutch, French, Spanish, Italian, Portuguese,
Polish, from LibriVox + Gutenberg. The multi-language equivalent of the
English work.

**[CSS10](https://github.com/Kyubyong/css10)** — single speaker in ten
languages including **Japanese**, Chinese, Russian, Greek, Finnish, Hungarian,
from LibriVox. Single-speaker-per-language is exactly the voice-LoRA shape.

**[Common Voice](https://commonvoice.mozilla.org)** — **CC0**, ~9,283 hours
across 60 languages. Read Wikipedia sentences rather than audiobook
performance, so useful for speaker variety and a floor anchor, less so for
narration register.

**[CML-TTS](https://github.com/freds0/CML-TTS-Dataset)** — **CC BY 4.0**,
MLS adapted for TTS: Dutch, French, German, Italian, Polish, Portuguese,
Spanish.

**[JSUT / JVS](https://www.jstage.jst.go.jp/article/ast/41/5/41_E1950/_pdf)** —
Japanese, 10 h single speaker and 30 h across 100 speakers. Check the current
licence terms before use; they are free for research but not obviously the same
blanket public domain as the LibriVox-derived sets.

**J-MAC** — Japanese multi-speaker audiobook corpus, ~150 audiobooks. Built
from commercial Audiobook.jp recordings with Aozora reference text, so the
AUDIO is not redistributable the way the above are. Noted for completeness and
deliberately not a candidate.

---

## Corpora and annotations used for attribution training and evaluation (added 2026-09-19)

Recorded here because the notices file is where a rights question gets
written down, and `ab_test_runtime/corpora/PROVENANCE.md` has pointed at this
file for the Chinese sets since August without the entry existing.

| corpus | licence | how it is used | shipped? |
| --- | --- | --- | --- |
| [Project Dialogism Novel Corpus (PDNC)](https://github.com/Priya22/project-dialogism-novel-corpus) — Vishnubhotla, Hammond, Hirst | **none stated.** The 28 novels are public domain; the repository declares no licence for the annotations (checked 2026-09-19: no LICENSE file, no wording in the ReadMe). | Quotation-to-speaker labels: 20 novels train the rights-clean adapters, 8 are evaluation fixtures (`pdnc_fixture.py`). | **Adapters trained on it are public on the Hub** (`Om22s/alexandria-qwen3-attribution`). Permission to train and redistribute has been requested from the authors; every card states the licence is unstated until they answer. The corpus itself is not redistributed. |
| [RiQuA](https://www.ims.uni-stuttgart.de/en/research/resources/corpora/riqua/) — Papay & Padó, LREC 2020 | no licence file; the paper says "publicly available for use, modification, and experimentation" (availability wording, not a named licence). Texts public domain. | Training rows (`riqua_trainset.py`); Emma excluded because it is a fixture. | adapters public; corpus not redistributed |
| [DraCor](https://dracor.org) play corpora | per-play TEI `<licence>`; the builder (`dracor_trainset.py`) keeps CC0 / public-domain / CC BY(-SA) plays and refuses BY-NC / ND. `ibs` is BY-NC despite the registry and is excluded. | Prose renderings of speaker-labelled plays as training rows. | adapters public; corpus not redistributed |
| Chinese quotation attribution sets — WP (World of Plainness, 平凡的世界) and JY (Jin Yong) | **Apache-2.0** (the released speaker-identification datasets; characters anonymised as `[C0]`…) | Evaluation only (`chinese_attribution.py`). | no |
| JY-QuotePlus | **no licence**, and it annotates a novel still in copyright. | Evaluation of the Chinese frame only (`chinese_attribution_frame.py`), which records the same caveat in its artifact. | no; nothing trained on it |
| WP2021 — 1,835 Chinese quotations whose gold is a character rather than a mention (#397) | no stated licence | Read locally to test the Elson-style frame in Chinese; fetched and converted locally, fixtures gitignored, only the script and derived counts committed. | no |
| [LibriQuote](https://huggingface.co/datasets/gasmichel/LibriQuote) — Michel, Epure, Cerisara, Findings of ACL 2026 | **CC BY-NC 4.0** | Evidence only: quotes-trained vs narration-trained voice adapters, cross-scored. | **never** — nothing trained on it ships |
| [LitBank](https://github.com/dbamman/litbank) / BookNLP — Bamman et al. | CC BY 4.0 (LitBank) | The published attribution baseline our end-to-end PDNC number is compared against (`external_comparability.py`); no data copied. | no |

## Speech corpora used since 2026-08-05

The 2026-08-05 section above listed Kokoro as a candidate. It and three more
are now in use; all four are fetched to the gitignored `ab_test_runtime/corpora/`
by the scripts named, none is redistributed.

| corpus | licence | used for |
| --- | --- | --- |
| [Kokoro Speech Dataset](https://mozilladatacollective.com/datasets/cmmknsho4014wmf087kvq5rc6) | public domain (Aozora texts, LibriVox recordings) | the Japanese human ceiling for goals 2.1, 2.5, 2.6, 2.9 (`kokoro_fetch.py`) |
| [AISHELL-3](https://www.openslr.org/93/) | Apache-2.0 | the Chinese human ceiling (`aishell3_prepare.py`) |
| [Hi-Fi TTS](https://www.openslr.org/109/) | CC BY 4.0 (LibriVox audio, Gutenberg text) | a second English reader for goal 2.1 (`hifitts_fetch.py`) |
| LJSpeech | public domain | as above |

## Models used for measurement (not shipped)

| model | licence | used in |
| --- | --- | --- |
| `torchaudio.pipelines.MMS_FA` (Meta MMS forced aligner) | **CC-BY-NC 4.0** | `aligned_japanese_accent.py` — mora alignment for goal 2.9. Evaluation only; no shipped feature calls it. |
| `jonatasgrosman/wav2vec2-large-xlsr-53-chinese-zh-cn` | Apache-2.0 | `contextual_tone_model.py` — Mandarin syllable alignment for 2.9 |
| `speechbrain/spkrec-ecapa-voxceleb` | Apache-2.0 | every ECAPA speaker-similarity number (`_ecapa_batch.py`, `voice_reference.py`) — this one is also used by the shipped drift check |
| [pyopenjtalk](https://github.com/r9y9/pyopenjtalk) / OpenJTalk | MIT / modified BSD | Japanese accent labels (`expected_prosody.py`, `aligned_japanese_accent.py`) |
| [pypinyin](https://github.com/mozillazg/python-pinyin) | MIT | Mandarin tone per syllable (`expected_prosody.py`, `contextual_tone_model.py`) |
| [SudachiPy](https://github.com/WorksApplications/SudachiPy) + SudachiDict | Apache-2.0 (chosen over JMdict's CC BY-SA, whose share-alike reaches a shipped app) | Japanese tokenisation for the Latinized-word discovery behind goal 5.5 (#325) |
| [Muse-Glimmer-30B](https://huggingface.co/meta-models/Muse-Glimmer-30B), Qwen3 / Qwen3.6 / Qwen3.8 | Apache-2.0 | attribution bases; adapters for the Qwen models are published, trained as recorded in RECIPES |
| DeepSeek v4-pro (API) | vendor terms | the cloud ceiling in RECIPES; nothing trained on its outputs except the distillation rows RECIPES names, none of which is in a public adapter |

## Qwen3 read-aloud timing (#1844)

| Source | Licence | Used in |
| --- | --- | --- |
| [ReadAloudMaker](https://github.com/REFLEXGAMING007/ReadAloudMaker) commit 8ba70a83383e8a5bddd8b859016ffbbcefccba18, Copyright (c) 2026 ReadAloudMaker contributors | MIT | Sentence boundary splitting, sentence-span redistribution over inline markup, punctuation-free token mapping and alignment-window scheduling, re-derived over AA2's own normalization. Its standalone server/UI, standalone packager/validator and growth/clamp/stretch choices are not used. |
| [QwenLM/Qwen3-ASR](https://github.com/QwenLM/Qwen3-ASR) commit 7c6daf77a2421100f5fb066495372c00129d39ff (qwen_asr==0.0.6) | Apache-2.0 | `Qwen3ForcedAligner` backend used at runtime in `app/qwen_align_worker.py`; not bundled, imported only in the isolated qwen-env interpreter. |
| `Qwen/Qwen3-ForcedAligner-0.6B` (Hugging Face, pin c7cbfc2048c462b0d63a45797104fc9db3ad62b7) | Apache-2.0 | Model weights loaded locally at runtime in the qwen-env interpreter via `local_files_only=True`; never downloaded by a build, never shipped. |

### MIT License (ReadAloudMaker, complete terms for the adapted code)

Adapted substantial code (the sentence boundary algorithm, sentence-span
redistribution, punctuation-free token mapping and alignment-window
scheduling re-derived in `app/qwen_align.py` and `app/epub_readaloud.py`)
obeys the donor's license, reproduced verbatim below per its own
conditions:

```text
MIT License

Copyright (c) 2026 ReadAloudMaker contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The Qwen3-ASR package and weights are Apache-2.0 **dependencies** used at
runtime in the isolated `qwen-env` interpreter. The sentence-level idea
originated with ReadAloudMaker; the full build, EPUB source preservation,
receipts, and process ownership are this project's own design.

### hangry-labs/Qwen3-ASR-STT decoder (Apache-2.0, copied code)

- Source: https://github.com/hangry-labs/Qwen3-ASR-STT — commit
  `97ad227a7ec2fb1ae6f601dbe0206b308158252e`,
  `qwen_asr/inference/qwen3_forced_aligner.py`
- Licence: Apache-2.0 (Copyright 2026 The Alibaba Qwen team, per the
  source file header)
- Used in: `app/qwen_score_decoder.py` (worker-only, in the isolated
  qwen-env interpreter). Only the constrained maximum-score
  monotonic-path decoder invariant is borrowed: an already-valid argmax
  path is kept, otherwise the highest-scoring strictly-positive
  monotonic path over real model scores is decoded. Deliberate
  divergences from the donor: its `_repair_timestamps` interpolation
  and valid-LIS fast path are not ported, and impossible/non-finite
  inputs raise instead of returning a repaired path. The donor's fork
  server and Transformers-5 migration are not used. The license terms
  for the borrowed code are reproduced in full below per their own
  conditions:

```text
                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

   TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION

   1. Definitions.

      "License" shall mean the terms and conditions for use,
      reproduction, and distribution as defined by Sections 1 through
      9 of this document.

      "Licensor" shall mean the copyright owner or entity authorized by
      the copyright owner that is granting the License.

      "Legal Entity" shall mean the union of the acting entity and all
      other entities that control, are controlled by, or are under
      common control with that entity. For the purposes of this
      definition, "control" means (i) the power, direct or indirect,
      to cause the direction or management of such entity, whether by
      contract or otherwise, or (ii) ownership of fifty percent (50%)
      or more of the outstanding shares, or (iii) beneficial ownership
      of such entity.

      "You" (or "Your") shall mean the individual or Legal Entity
      exercising permissions granted by this License.

      "Source" form shall mean the preferred form for making
      modifications, including but not limited to software source code,
      documentation source, and configuration files.

      "Object" form shall mean any form resulting from mechanical
      transformation or translation of a Source form, including but
      not limited to compiled object code, generated documentation,
      and conversions to other media types.

      "Work" shall mean the work of authorship, whether in Source or
      Object form, made available under the License, as indicated by a
      copyright notice that is included in or attached to the work
      (an example is provided in the Appendix below).

      "Derivative Works" shall mean any work, whether in Source or Object
      form, that is based on (or derived from) the Work and for which
      the editorial revisions, annotations, elaborations, or other
      modifications represent, as a whole, an original work of
      authorship. For the purposes of this License, Derivative Works
      shall not include works that remain separable from, or merely
      link (or bind by name) to the interfaces of, the Work and
      Derivative Works thereof.

      "Contribution" shall mean any work of authorship of any kind,
      commercial, political, publication or otherwise, that is based
      on (or derived from) the Work and for which the editorial
      revisions, annotations, elaborations, or other modifications
      represent, as a whole, an original work of authorship; but in
      all cases, Contribution shall mean a work subject to the
      License.

      "Contributor" shall mean Licensor and any individual or Legal
      Entity on behalf of whom a Contribution has been received by
      Licensor and subsequently incorporated within the Work.

   2. Grant of Copyright License. Subject to the terms and
      conditions of this License, each Contributor hereby grants to
      You a perpetual, worldwide, non-exclusive, no-charge,
      royalty-free, irrevocable copyright license to reproduce,
      prepare Derivative Works of, publicly display, publicly
      perform, sublicense, and distribute the Work and such Derivative
      Works in Source or Object form.

   3. Grant of Patent License. Subject to the terms and conditions
      of this License, each Contributor hereby grants to You a
      perpetual, worldwide, non-exclusive, no-charge, royalty-free,
      irrevocable (except as stated in this section) patent license
      to make, have made, use, offer to sell, sell, import, and
      otherwise transfer the Work, where such license applies only to
      those patent claims licensable by such Contributor that are
      necessarily infringed by their Contribution(s) alone or by
      combination of their Contribution(s) with the Work to which
      such Contribution(s) was submitted. If You institute patent
      litigation against any entity (including a cross-claim or
      counterclaim in a lawsuit) alleging that the Work itself
      constitutes direct or contributory patent infringement, then
      any patent licenses granted to You under this License for that
      Work shall terminate as of the date such litigation is filed.

   4. Redistribution. You may reproduce and distribute copies of
      the Work or Derivative Works thereof in any medium, with
      or without modifications, and in Source or Object form,
      provided that You meet the following conditions:

      (a) You must give any other recipients of the Work or
          Derivative Works a copy of this License; and

      (b) You must cause any modified files to carry prominent
          notices stating that You changed the files; and

      (c) You must retain, in the Source form of any Derivative
          Works that You distribute, all copyright, patent,
          trademark, and attribution notices from the Source form
          of the Work, excluding those notices that do not pertain
          to any part of the Derivative Works; and

      (d) If the Work includes a "NOTICE" text file as part of
          its distribution, then any Derivative Works that You
          distribute must include a readable copy of the
          attribution notices contained within such NOTICE
          file, excluding those notices that do not pertain to
          any part of the Derivative Works, in at least one
          of the following places: within a NOTICE text file
          distributed as part of the Derivative Works; within
          the Source form and documentation, if provided
          along with the Derivative Works; or, if not, then
          within a display generated by the Derivative Works,
          if and wherever such third-party notices normally
          appear. The contents of the NOTICE file are for
          informational purposes only and do not modify the
          License. You may add Your own attribution notices
          within Derivative Works that You distribute, alongside
          or as an addendum to the NOTICE text from the Work,
          provided that such additional attribution notices
          cannot be construed as modifying the License.

      You may add Your own copyright statement to Your
      modifications and may provide additional or different
      license terms and conditions for use, reproduction, or
      distribution of Your modifications, or for any such
      Derivative Works as a whole, provided Your use,
      reproduction, and distribution of the Work otherwise
      complies with the conditions stated in this License.

   5. Submission of Contributions. Unless You explicitly state
      otherwise, any Contribution intentionally submitted for
      inclusion in the Work by You to the Licensor shall be
      under the terms and conditions of this License, without
      any additional terms or conditions. Notwithstanding the
      above, nothing herein shall supersede or modify the
      terms of any separate License Agreement you may have
      executed with Licensor regarding such Contributions.

   6. Trademarks. This License does not grant permission to use
      the trade names, trademarks, service marks, or product names
      of the Licensor, except as required for reasonable and
      customary use in describing the origin of the Work and
      reproducing the content of the NOTICE file.

   7. Disclaimer of Warranty. Unless required by applicable law
      or agreed to in writing, Licensor provides the Work (and
      each Contributor provides its Contributions) on an "AS
      IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
      either express or implied, including, without limitation,
      any warranties or conditions of TITLE, NON-INFRINGEMENT,
      MERCHANTABILITY, or FITNESS FOR A PARTICULAR PURPOSE. You
      are solely responsible for determining the appropriateness
      of using or redistributing the Work and assume any risks
      associated with Your exercise of permissions under this
      License.

   8. Limitation of Liability. In no event and under no legal
      theory, whether in tort (including negligence), contract, or
      otherwise, unless required by applicable law (such as
      deliberate and grossly negligent acts) or agreed to in
      writing, shall any Contributor be liable to You for damages,
      including any direct, indirect, special, incidental or
      consequential damages of any character arising as a result of
      this License or out of the use or inability to use the
      Work (including but not limited to damages for loss of
      goodwill, work stoppage, computer failure or malfunction, or
      any and all other commercial damages or losses), even if
      such Contributor has been advised of the possibility of
      such damages.

   9. Accepting Warranty or Additional Liability. While
      redistributing the Work and Derivative Works thereof You
      may choose to offer, and charge a fee for, acceptance of
      support, warranty, indemnity, or other liability
      obligations and/or rights consistent with this License.
      However, in accepting such obligations, You act solely
      on Your own behalf and on Your sole responsibility, not
      on behalf of any other Contributor, and only if You
      agree to indemnify, defend, and hold each Contributor
      harmless for any liability incurred by or claims asserted
      against such Contributor by reason of your accepting
      any such warranty or additional liability.

   END OF TERMS AND CONDITIONS

   APPENDIX: How to apply the Apache License to your work.

      To apply the Apache License to your work, add the
      following boilerplate notice, with the fields enclosed
      by brackets "[]" replaced with your own identifying
      information. (Don't include the brackets!)  The text
      should be enclosed in the appropriate comment syntax
      for the file format. We also recommend that a
      file or class name and description of purpose be included
      on the same "printed page" as the copyright notice for
      files contributed by optional authors.

      Copyright [yyyy] [name of copyright owner]

      Licensed under the Apache License, Version 2.0 (the
      "License"); you may not use this file except in compliance
      with the License. You may obtain a copy of the License at

          http://www.apache.org/licenses/LICENSE-2.0

      Unless required by applicable law or agreed to in writing,
      software distributed under the License is distributed on an
      "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
      either express or implied. See the License for the specific
      language governing permissions and limitations under the
      License.
```

## Research consulted since 2026-08-05

- Michel, Epure, Cerisara — the Michel et al. prompt formulation and LibriQuote (above).
- The nine papers read 2026-09-13 (usual-suspect prior, hard-case selection, prosodic pruning, quotes-vs-narration, chapter cuts) — findings and which held are in `docs/` and RECIPES; no code or text taken.
- Vishnubhotla, Hammond, Hirst — PDNC and the alias-oracle caveat on every published PDNC number (GOALS 1.3).
- arXiv 2608.02359 — the "field considers this solved" framing that prompted the context-window sweep (#351), and the source of the alias-oracle caveat: systems on the PDNC benchmark "access the gold-labelled character list both at training and evaluation time" (#498).
- arXiv 2307.03734 (BookNLP-OG, the one end-to-end PDNC number, 0.40) and arXiv 2406.11380 (#498).
- arXiv 2408.09452 (NLPCC 2024) and arXiv 2312.14590 (SIG) — the zero-shot GPT-3.5 numbers that GOALS 1.3 says must not be quoted as targets (#389).
- EmergentTTS-Eval, arXiv 2505.23009 — homographs and syntactic complexity as TTS challenge categories, confirming the "only a listener can see it" finding (#413).
- Elson & McKeown's attribution categories, re-implemented for Chinese word order in `chinese_attribution_frame.py` (#390); no code taken.
