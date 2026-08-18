"""An experiment that writes an artifact must record how it was produced.

138 artifacts in this repository carry no provenance block at all: no commit,
no host, not even a dirty flag. They cannot be replayed, and replaying the ones
that CAN be is a smaller job than this - so the number that matters is not how
many old artifacts are broken but whether new ones join them.

`experiments/provenance.py::provenance` already exists and 54 scripts use it.
This is about the rest. Rather than rewrite 64 scripts blind - several are
historical one-offs whose reruns nobody wants - the list below freezes today's
debt, and any experiment written or converted from now on must stamp its
artifacts. Removing a name from the list (by wiring the helper) is always
welcome; adding one is not.
"""
import os
import unittest

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPERIMENTS = os.path.join(APP, "experiments")

# Scripts that wrote artifacts without provenance as of 2026-08-18. Frozen
# debt, not permission.
LEGACY_UNPROVENANCED = {
    "adaptive_split_floor.py",
    "audible_errors.py",
    "booknlp_baseline.py",
    "build_validation_manifest.py",
    "chinese_attribution.py",
    "chunk_completion.py",
    "chunk_retry_probe.py",
    "chunker_attribution.py",
    "cluster_vs_name.py",
    "corpus_hnr_baseline.py",
    "discover_foreign_terms.py",
    "distill_collect.py",
    "duration_probe.py",
    "fallback_policy.py",
    "finalise_fixture.py",
    "generation_state_probe.py",
    "hnr_length_probe.py",
    "ingest_judgements.py",
    "japanese_quote_robustness.py",
    "judge_agreement.py",
    "judge_prompts.py",
    "kokoro_ja_asr_set.py",
    "kokoro_same_speaker_build.py",
    "lexicon_corpus_scan.py",
    "listener_impact.py",
    "lora_scale_sweep.py",
    "make_fixture.py",
    "manifest.py",
    "moss_vs_lora.py",
    "name_consistency.py",
    "nonprose_gate.py",
    "offbyone_turns.py",
    "pair_e_row.py",
    "pdnc_context_evidence.py",
    "pdnc_eval.py",
    "pdnc_failure_telemetry.py",
    "pdnc_fixture.py",
    "pdnc_generalisation.py",
    "pdnc_narrator_prior.py",
    "pitch_quality_probe.py",
    "profile_vram.py",
    "quote_fallthrough.py",
    "realizable_router.py",
    "residual_errors.py",
    "robotic_proxy.py",
    "run_lengths.py",
    "scale_vs_register.py",
    "segmentation_classifier.py",
    "selection_gap_recheck.py",
    "shipping_readiness.py",
    "source_span_coverage.py",
    "stack_overlap.py",
    "symbolization.py",
    "targeted_missing_repair.py",
    "three_pass_chunk_probe.py",
    "training_composition.py",
    "trim_silence_build.py",
    "trivial_baselines.py",
    "tts_boundary_audit.py",
    "tts_output_validation.py",
    "tuned_disagreement.py",
    "voice_adapter_health.py",
    "voice_blending.py",
    "weak_supervision.py",
}


class ProducerProvenanceTest(unittest.TestCase):
    def _producers(self):
        for name in sorted(os.listdir(EXPERIMENTS)):
            if not name.endswith(".py") or name.startswith("test_"):
                continue
            with open(os.path.join(EXPERIMENTS, name), encoding="utf-8") as fh:
                source = fh.read()
            if "atomic_json_write(" in source or "json.dump(" in source:
                yield name, source

    def test_new_producers_stamp_their_artifacts(self):
        offenders = [name for name, src in self._producers()
                     if "provenance(" not in src
                     and name not in LEGACY_UNPROVENANCED]
        self.assertEqual([], offenders,
                         "these scripts write artifacts with no provenance; "
                         "call experiments.provenance.provenance(__file__, args)")

    def test_the_debt_list_does_not_name_scripts_that_are_gone(self):
        present = {name for name, _ in self._producers()}
        missing = LEGACY_UNPROVENANCED - present
        self.assertEqual(set(), missing,
                         "listed scripts no longer write artifacts; drop them")

    def test_the_debt_list_does_not_name_scripts_already_fixed(self):
        """A paid debt left on the list hides the progress and inflates it."""
        unfixed = {name for name, src in self._producers()
                   if "provenance(" not in src}
        self.assertEqual(set(), LEGACY_UNPROVENANCED - unfixed,
                         "these are stamped now - remove them from "
                         "LEGACY_UNPROVENANCED")


if __name__ == "__main__":
    unittest.main()
