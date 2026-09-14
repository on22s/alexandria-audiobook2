# Corpora used by the non-English attribution experiments

These files are **not** in version control — they are downloadable from the
sources below, and 11 MB of public-domain text does not belong in a repository
whose history is already several gigabytes. What is committed is this file, so
the data can be reconstructed.

They previously lived in a Claude session scratchpad whose path embedded a
session UUID, which meant `chinese_attribution.py`, `quote_aware_chunking.py`
and `japanese_quote_robustness.py` referenced a directory that would disappear
when that session was cleaned. Moved here on 2026-08-04.

Override the location with `ALEXANDRIA_CHINESE_CORPUS` or
`ALEXANDRIA_AOZORA_CORPUS`; otherwise the scripts look here.

## aozora/ — Japanese, public domain

Aozora Bunko (https://www.aozora.gr.jp/). Public domain in Japan; these authors
died more than 70 years ago.

| file | work | author |
| --- | --- | --- |
| `kokoro.txt` / `.html` | Kokoro | Natsume Sōseki |
| `ningen.txt` / `.html` | Ningen Shikkaku | Dazai Osamu |
| `rashomon.txt` / `.html` | Rashōmon | Akutagawa Ryūnosuke |

Used to test quote-aware chunking against 「」 and 『』 delimiters, which behave
differently from Western quotation marks.

## chinese/ — Chinese quotation attribution

| file | contents |
| --- | --- |
| `wp_train_instances.json`, `wp_dev_instances.json`, `wp_test.json` | World of Plainness (WP) splits |
| `wp_names.txt` | speaker roster |
| `jy_test.json` | Jin Yong (JY) test split |

From the Chinese speaker-identification datasets released under Apache-2.0 and
recorded in `THIRD_PARTY_NOTICES.md`. These are the corpora whose existence was
wrongly denied on 2026-08-03 before being found — see the note in that file.

## Reconstructing

Place the directories as `ab_test_runtime/corpora/aozora` and
`ab_test_runtime/corpora/chinese`. The experiments fail loudly with a missing
path rather than silently scoring nothing.

## hifitts/<reader>/ — English, a second public human-read reference set

Hi-Fi Multi-Speaker English TTS Dataset (Bakhturina, Lavrukhin, Ginsburg,
Zhang; Interspeech 2021; OpenSLR 109). LibriVox audio, Gutenberg text,
**CC BY 4.0**, 44.1 kHz, human transcripts (raw and normalised). Fetched per
reader from the parquet mirror `MikhailT/hifi-tts` by
`app/experiments/hifitts_fetch.py`, which reads each shard's `speaker`
column before deciding to download it, so one reader costs a few gigabytes
of transfer and never the 41 GB tarball. Written LJSpeech-shaped
(`wavs/<id>.wav`, `metadata.csv`, plus `corpus.json` naming the corpus,
licence and native rate) so `ljspeech_prepare.py` and everything after it
run unchanged; `id = <book_slug>-<chapter>_<seq>` so the split-by-source-work
rule holds.

| reader | name | sex | clean hours | works fetched (2026-09-13, ≤400 clips each) |
| --- | --- | --- | --- | --- |
| 9017 | John Van Stan | M | 58.0 | dartagnan01, dartagnan03part1, dartagnan03part3, zarathustra, antoinetteromances4, celebratedcrimesv1, historyofforestry |

Used by goal 2.9 (`run_chains/hifitts_9017_20260913.sh`) as the public
counterpart to the eight private narrators of `second_english_eval_20260820`.

## dracor/<corpus>/tei/ — play scripts, speaker-labelled by the text itself

DraCor (Drama Corpora Project, https://dracor.org/), TEI P5. Every speech is
`<sp who="#id">` against a cast list of canonical names, so the speaker label
is part of the source and needs no annotator. Fetched by
`app/experiments/dracor_trainset.py` as the GitHub archive of each corpus
repository (`https://github.com/dracor-org/<corpus>dracor`), not through the
dracor.org API, which does not serve every corpus (`lacy` is absent from it).
`archive.json` beside each `tei/` records the archive URL and sha256.

Licences, checked 2026-09-13 in the DraCor registry
(`dracor-org/dracor-registry`, `corpora.json`) and confirmed per file: every
TEI file fetched below carries `<licence target=".../publicdomain/zero/1.0/">`
in its header; the builder refuses any play whose licence element names
anything else. The underlying plays are public domain (authors dead 70+
years). Corpora under CC BY-NC (`eng`, `shake`) and CC BY-NC-SA (`fre`) are
not used.

| corpus | language | plays | encoding licence | used by |
| --- | --- | --- | --- | --- |
| `lacy` | en, Victorian (Lacy's Acting Edition) | 103 | CC0 (per-file statement) | English arm |
| `am` | en, American | 40 | CC0 (per-file) | English arm |
| `ger` | de | ~780 | CC0 | mixed arm |
| `rus` | ru | ~210 | CC0 | mixed arm |
| `dutch` | nl | — | CC0 | mixed arm |
| `pol` | pl | — | CC0 | mixed arm |
| `ibs` | no (Ibsen) | — | CC0 | mixed arm |
| `ar` | es (Argentine) | — | CC0 | mixed arm |

Reconstruct with:

```
python app/experiments/dracor_trainset.py --corpora lacy am --out-dir ab_test_runtime/distill/dracor_en_20260913
python app/experiments/dracor_trainset.py --corpora lacy am ger rus dutch pol ibs ar --out-dir ab_test_runtime/distill/dracor_mixed_20260913
```

Both are seeded (`--seed 20260913`) and write a `manifest.json` naming every
play, its rejected-speech counts and the per-language row and token totals.

## riqua/

RiQuA (Papay & Padó, LREC 2020): 5,963 quotations with speaker, addressee
and cue spans over 15 brat documents from 11 19th-century works. Fetched
2026-09-14 from https://www.ims.uni-stuttgart.de/documents/ressourcen/korpora/riqua/riqua.tar.gz
(sha256 6c3bb5361650e1007819f50f9763a05cee85b5340cf2ecb629fa7ab2970c64ed).
No licence file in the archive; the paper says "publicly available for
use, modification, and experimentation"; source texts public domain.
Recorded as availability wording, not a named licence. `austen_emma_*` is
excluded from every training set because Emma is an evaluation fixture.
Rebuild: `app/experiments/riqua_trainset.py --riqua ab_test_runtime/corpora/riqua/riqua/merged --out-dir ab_test_runtime/distill/riqua_20260914`.
