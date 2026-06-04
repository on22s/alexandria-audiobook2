#!/usr/bin/env bash

PREP=/home/fakemitch/pinokio/api/alexandria-audiobook2.git/alexandria_preparer_rocm_compatible.py
PY=/home/fakemitch/pinokio/api/alexandria-audiobook.git/app/env/bin/python
export PYTHONPATH=/home/fakemitch/pinokio/api/alexandria-audiobook2.git:${PYTHONPATH}
MODEL=Qwen2.5-14B-Instruct-Q6_K.gguf
NEW_DIR="/home/fakemitch/Desktop/New folder/new new"
OUT_BASE=/home/fakemitch/Desktop/zips2

echo "[$(date)] Starting new batch — 26 narrators"

run_narrator() {
  local stem="$1"
  local audio="$NEW_DIR/$stem.wav"
  local epub="$NEW_DIR/$stem.epub"
  local output="$OUT_BASE/$stem/$stem"
  echo "[$(date)] === $stem ==="
  mkdir -p "$OUT_BASE/$stem"

  # First attempt
  $PY $PREP --audio "$audio" --model "$MODEL" --output "$output" --source "$epub"
  local exit_code=$?

  # Auto-resume loop: retry up to 5 times on crash, using --resume to skip completed work
  local attempt=1
  while [ $exit_code -ne 0 ] && [ $attempt -le 5 ]; do
    echo "[$(date)] WARNING: $stem crashed (exit $exit_code), auto-resuming (attempt $attempt/5)..."
    sleep 5
    $PY $PREP --audio "$audio" --model "$MODEL" --output "$output" --source "$epub" --resume
    exit_code=$?
    attempt=$((attempt + 1))
  done

  if [ $exit_code -ne 0 ]; then
    echo "[$(date)] ERROR: $stem failed after 5 resume attempts, skipping."
  else
    echo "[$(date)] DONE: $stem"
  fi
}

run_narrator "Alyssa Poon, Robert Bradvica Rise of the Weakest Summoner: Volume I [B09NZKLSCB]"
run_narrator "Amy Landon A Memory Called Empire: Teixcalaan, Book 1 [1250318955]"
run_narrator "Brian Nishii Alita: Battle Angel: The Official Movie Novelization [B07HHJPKMJ]"
run_narrator "C.J. Mission Monster Core 2 [1774241641]"
run_narrator "Christian J. Gilliland, Hazel Cohen Elemental Summoner 1 [B09BXYJXYT]"
run_narrator "Cindy Kay Water Moon: A Novel [B0D26L1R1D]"
run_narrator "Dracula [Audible Edition] [B0078PA1OA]"
run_narrator "Emily Woo Zeller Red Winter: The Complete Trilogy [B0D1Z63LZD]"
run_narrator "Gareth Armstrong The Devastation of Baal: Space Marine Conquests: Warhammer 40,000, Book 1 [B07CTT3BGQ]"
run_narrator "Hollie Jackson Seize the Day: A World Conquest Isekai: Empress, Book 1 [B0CXRZXTR9]"
run_narrator "John Keating Half the War [B00SZABKC4]"
run_narrator "John Lee Spellmonger: Spellmonger, Book 1 [B01N264EEK]"
run_narrator "Jon Lindstrom Nightfall and Other Stories [059341635X]"
run_narrator "Jot Davies The Vagrant [B00VUS19W4]"
run_narrator "Kirby Heyborne Out of House and Home: Fred, the Vampire Accountant Series, Book 7 [B09D1HQBVY]"
run_narrator "Mallorie Rodak Frieren: Beyond Journey's End -Prelude-, Vol. 1 [B0GX9T26CV]"
run_narrator "Mark Dacascos Battle Royale [B0093O1ZY4]"
run_narrator "Nick Podehl Super Sales on Super Heroes, Book 2: Super Sales on Super Heroes, Book 2 [B07CJ4C449]"
run_narrator "Qarie Marshall Wolverine: Road of Bones [1662042051]"
run_narrator "Scott Brick Foundation's Edge [B005WL4R7E]"
run_narrator "Shiromi Arserio The Jasmine Throne [154910487X]"
run_narrator "Simon Vance The Blinding Knife [B0096GJ7G2]"
run_narrator "Tara Sands Shorefall: A Novel [0593148053]"
run_narrator "Todd McLaren Altered Carbon [B002V1O6X8]"
run_narrator "Traci Kato-Kiriyama The Phone Booth at the Edge of the World: A Novel [059345894X]"
run_narrator "Various Waking Gods [B01NGUBLBW]"

echo "[$(date)] All 26 narrators complete."
