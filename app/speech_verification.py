"""Bounded finished-take speech evidence, with distinct confirmation of suspicions."""
import json
import copy
from pathlib import Path
import time

from cpu_asr_scratch import CPUASRScratch
from speech_verification_acoustic import get_short_acoustic_evidence, MAX_SHORT_SECONDS
from speech_verification_compare import (get_comparison_tokens, get_speech_comparison,
    get_material_regions, get_corroborated_regions)
from speech_verification_runtime import (get_verifier_identity, get_verifier_file_identity,
    ensure_distinct_verifier_confirmation)
from word_timing import (MAX_AUDIO_BYTES, MAX_RESULT_BYTES, decode_asr_audio,
                         transcribe_asr_audio, get_asr_transcript)


class SpeechVerificationCancelled(ValueError):
    """Cancellation remains cancellation, never optional verifier unavailability."""


def verify_finished_speech(audio_path, actual_text, language, primary_runtime,
                           confirmation_runtime, cancel_check=lambda: False,
                           *, ambiguous_expected_tokens=(), ambiguous_expected_regions=()):
    """Check a finished artifact against the caller's proven exact API speech input.

    This does not reconstruct expectation from current author/pronunciation
    settings. Publication must validate that input proof and bind the returned
    byte identity to the exact selected take before attaching evidence.
    """
    start = time.monotonic()
    report = dict(version=1, status='unavailable', stage='expectation',
        ambiguous_expected_regions=copy.deepcopy(ambiguous_expected_regions),
        notice='ASR and acoustic evidence require listening review; no pronunciation certification.')

    def cancelled():
        if cancel_check():
            raise SpeechVerificationCancelled('Speech verification cancelled.')
        return False

    try:
        cancelled()
        tokens = get_comparison_tokens(actual_text)
        if not tokens:
            raise ValueError('Exact nonempty synthesis input is required.')
        if not isinstance(language, str) or language.casefold() not in ('en', 'english'):
            raise ValueError('Speech verification is currently qualified for English input only.')
        report['expected_text'] = actual_text
        report['stage'] = 'provider_admission'
        with CPUASRScratch() as scratch:
            snapshot, audio = scratch.capture(audio_path, cancelled)
            report['audio'] = audio
            # A mono decode could hide cancellation between stereo channels.
            # Refuse that ambiguity until a channel policy is qualified.
            import soundfile as sf
            try:
                channels = sf.info(str(snapshot)).channels
            except RuntimeError as error:
                raise ValueError('Finished audio metadata is unavailable to the verifier.') from error
            if channels != 1:
                raise ValueError('Speech verification requires qualified mono generated audio.')
            report['stage'] = 'primary_identity'
            primary = get_verifier_identity(primary_runtime, 'primary', cancelled)
            report['stage'] = 'decode'
            decoded, duration = decode_asr_audio(snapshot, cancelled, scratch,
                command=scratch.run, decoder=primary['decoder']['resolved_path'])
            report['duration'] = duration
            if len(tokens) < 6:
                if duration <= MAX_SHORT_SECONDS:
                    report['short_acoustic'] = get_short_acoustic_evidence(decoded, cancelled)
                else:
                    report['short_acoustic'] = dict(status='unavailable_duration_limit')
            report['stage'] = 'primary'
            pass_start = time.monotonic()
            payload = transcribe_asr_audio(decoded, primary_runtime, cancelled, scratch, command=scratch.run)
            transcript = get_asr_transcript(payload)
            if get_verifier_identity(primary_runtime, 'primary', cancelled) != primary:
                raise ValueError('Primary verifier changed during execution.')
            comparison = get_speech_comparison(actual_text, transcript,
                ambiguous_expected_tokens=ambiguous_expected_tokens,
                ambiguous_expected_regions=ambiguous_expected_regions)
            report['primary'] = dict(identity=primary, transcript=transcript,
                comparison=comparison, seconds=time.monotonic() - pass_start)
            acoustic = report.get('short_acoustic', {})
            acoustic_unavailable = len(tokens) < 6 and acoustic.get('status', '').startswith('unavailable')
            suspicious_short = len(tokens) < 6 and (comparison['differences'] or
                acoustic.get('repetition_candidates') or acoustic.get('voiced_seconds') == 0)
            needs_confirmation = bool(get_material_regions(comparison) or suspicious_short)
            if needs_confirmation:
                report['stage'] = 'confirmation_identity'
                confirmation = get_verifier_identity(confirmation_runtime, 'confirmation', cancelled)
                ensure_distinct_verifier_confirmation(primary, confirmation)
                report['stage'] = 'confirmation'
                pass_start = time.monotonic()
                payload = transcribe_asr_audio(decoded, confirmation_runtime, cancelled, scratch, command=scratch.run)
                transcript = get_asr_transcript(payload)
                if get_verifier_identity(confirmation_runtime, 'confirmation', cancelled) != confirmation:
                    raise ValueError('Confirmation verifier changed during execution.')
                comparison2 = get_speech_comparison(actual_text, transcript,
                    ambiguous_expected_tokens=ambiguous_expected_tokens,
                    ambiguous_expected_regions=ambiguous_expected_regions)
                report['confirmation'] = dict(identity=confirmation, transcript=transcript,
                    comparison=comparison2, seconds=time.monotonic() - pass_start)
                report['corroborated_regions'] = get_corroborated_regions(comparison, comparison2)
            report['stage'] = 'audio_readmission'
            captured = get_verifier_file_identity(snapshot, MAX_AUDIO_BYTES, cancelled)
            current = get_verifier_file_identity(audio_path, MAX_AUDIO_BYTES, cancelled)
            if any(row['sha256'] != audio['sha256'] or row['size'] != audio['size']
                   for row in (captured, current)):
                raise ValueError('Finished audio changed during speech verification.')
            report['status'] = ('review_short_acoustic_unavailable' if acoustic_unavailable else
                'corroborated_content_difference' if report.get('corroborated_regions') else
                'review' if comparison['differences'] or suspicious_short else 'no_content_candidate')
            report['stage'] = 'complete'
    except SpeechVerificationCancelled:
        raise
    except (ValueError, OSError, KeyError, TypeError) as error:
        cancelled()
        report.update(status='review_confirmation_unavailable' if report['stage'].startswith('confirmation') else 'unavailable',
                      reason=str(error)[:240])
    report['seconds'] = time.monotonic() - start
    if len(json.dumps(report, ensure_ascii=False,allow_nan=False).encode('utf-8')) > MAX_RESULT_BYTES:
        # Discard the oversized diagnostics, rather than returning the same
        # oversized report with an error label or a misleading partial verdict.
        report={key:report[key] for key in ('version','expected_text','audio','duration','seconds',
            'ambiguous_expected_regions') if key in report}
        report.update(status='unavailable',stage='evidence_limit',
                      reason='Speech verification evidence exceeds 1 MiB.')
    return report


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_verification_code
_LOADED_SPEECH_VERIFICATION_CODE_SHA256 = _get_loaded_verification_code()
