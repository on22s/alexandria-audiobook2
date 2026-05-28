#!/bin/bash
# Rename Kizu zips based on voice clustering:
#   Character_1 = vol01, vol02  (same voice, keep one as primary)
#   Character_2 = vol03-09 (same voice, keep best 2)
#   Character_3 = vol10  (unique)
#
# Creates copies with new names, preserves originals.

SRC=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
DST=/home/fakemitch/pinokio/api/alexandria-audiobook2.git/renamed_zips

mkdir -p "$DST"

# ─── Character 1 (vol01 ↔ vol02, nearly identical. Keep vol01) ────
cp "$SRC/kizu_test_output_vol01.zip" "$DST/kizu_character_1_vol01.zip"

# ─── Character 2 (vol03-09, keep 2 most representative) ────────
# vol09 has highest avg similarity to others in this cluster
cp "$SRC/kizu_test_output_vol09.zip" "$DST/kizu_character_2_vol01.zip"
cp "$SRC/kizu_test_output_vol04.zip" "$DST/kizu_character_2_vol02.zip"

# ─── Character 3 (vol10, unique) ────────────────────────────────
cp "$SRC/kizu_test_output_vol10.zip" "$DST/kizu_character_3_vol01.zip"

# ─── Also label the source/original narrators ──────────────────
# Narrator datasets are all unique, just copy with clean names
cp "$SRC/test_corpus_output/dataset_Luci Christian Full Metal Panic-converted.zip" \
   "$DST/narrator_luci_christian_fmp.zip"
cp "$SRC/test_corpus_output_pre_wavfix/dataset_Cherami Leigh Cyberpunk 2077-converted.zip" \
   "$DST/narrator_cherami_leigh_cyberpunk.zip"
cp "$SRC/test_corpus_output_pre_wavfix/dataset_Cliff Kurt Mushoku Tensei-converted.zip" \
   "$DST/narrator_cliff_kurt_mushoku.zip"
cp "$SRC/test_corpus_output_pre_wavfix/dataset_J Michael Tatum Spice and Wolf, Vol. 10-converted.zip" \
   "$DST/narrator_j_michael_tatum_spicewolf.zip"
cp "$SRC/test_corpus_output_pre_wavfix/dataset_Jay Snyder Ex-Heroes-converted.zip" \
   "$DST/narrator_jay_snyder_exheroes.zip"

# ─── Source audiobooks ──────────────────────────────────────────
cp "$SRC/test_corpus_output/Full_Metal_Panic_Volume_1_Shouji_Gatou.zip" \
   "$DST/source_audiobook_fmp_shouji_gatou.zip"
cp "$SRC/test_corpus_output/Cyberpunk_2077_No_Coincidence_Rafal_Kosik.zip" \
   "$DST/source_audiobook_cyberpunk_rafal_kosik.zip"

echo "Done. Renamed zips in: $DST"
ls -1 "$DST"