"""Recheck one unchanged saved take using its bound, previously observed API input."""
import copy

from audio_generation_owner import (ensure_audio_generation_active,ensure_audio_generation_book,
    is_audio_generation_cancelled)
from speech_verification import verify_finished_speech
from speech_verification_receipt import (get_current_speech_verification_receipt,
    get_speech_verification_receipt)
from speech_verification_runtime import get_verifier_file_identity
from word_timing import MAX_AUDIO_BYTES


def recheck_editor_speech(manager,index,request,primary_runtime,confirmation_runtime,check_identity):
    """Keep selected audio intact, including when a fresh check finds a defect.

    No authored-text reconstruction, inference under book/owner locks, or new
    report cache. The caller retains its exact CPU task claim through cleanup.
    """
    with ensure_audio_generation_book(manager.root_dir):
        check_identity(index,request.expected_book_token,request.expected_uid)
        rows=manager.load_chunks()
        if not 0 <= index < len(rows):
            raise ValueError('The selected Editor line no longer exists.')
        chunk=copy.deepcopy(rows[index])
        if chunk.get('audio_revision') != request.expected_audio_revision:
            raise ValueError('The selected audio take changed. Refresh Editor.')
        previous=get_current_speech_verification_receipt(chunk,request.expected_book_token)
        version=previous.get('audio_file_version') if previous is not None else None
        if (previous is None or not previous['expected_text'] or not previous['audio']['sha256']
                or not isinstance(version,list) or len(version) not in (4,5)
                or any(type(value) is not int or value < 0 for value in version)):
            raise ValueError('This take has no saved, proven synthesis input. Regenerate it with speech checks enabled.')
        path=manager.get_chunk_audio_path(chunk.get('audio_path'))
    ensure_audio_generation_active()
    try:
        before=get_verifier_file_identity(path,MAX_AUDIO_BYTES,is_audio_generation_cancelled)
    except OSError as error:
        if is_audio_generation_cancelled():
            raise
        raise ValueError('The selected audio file changed or is unavailable. Regenerate the line before checking it.') from error
    binding={key:before[key] for key in ('sha256','size')}
    if binding != previous['audio']:
        raise ValueError('The selected audio bytes changed. Regenerate the line before checking it.')
    report=verify_finished_speech(path,previous['expected_text'],previous['language'],
        primary_runtime,confirmation_runtime,is_audio_generation_cancelled,
        ambiguous_expected_regions=previous['ambiguous_expected_regions'])
    ensure_audio_generation_active()
    current=get_verifier_file_identity(path,MAX_AUDIO_BYTES,is_audio_generation_cancelled)
    if (current != before or report.get('audio') is not None and report['audio'] != binding):
        raise ValueError('The selected audio changed while checking it. Refresh Editor.')
    report={**report,'audio':binding}
    speech_input={'text':previous['expected_text'],'language':previous['language'],
                  'provenance':previous['input_provenance']}
    receipt=get_speech_verification_receipt(report,chunk,request.expected_book_token,
        request.expected_audio_revision,speech_input)
    receipt['audio_file_version']=list(current['file_version'][:5])
    complete=report.get('stage') == 'complete' and report['status'] in (
        'no_content_candidate','review','corroborated_content_difference')
    # No new complete assessment means no contrary content evidence. Keep the
    # completed proof (especially a confirmed defect) and expose this finite
    # latest incomplete recheck separately on the same selected revision.
    fields=({'speech_verification':receipt,'speech_verification_recheck':None} if complete
            else {'speech_verification_recheck':receipt})
    with ensure_audio_generation_book(manager.root_dir):
        check_identity(index,request.expected_book_token,request.expected_uid)
        if manager._get_speech_staged_version(path) != tuple(current['file_version']):
            raise ValueError('The selected audio changed before the check could be saved.')
        updated=manager._update_chunk_fields_by_uid(chunk['uid'],expected_chunk=chunk,
            expected_fields={key:chunk.get(key) for key in ('audio_path','audio_revision',
                'speech_verification','speech_verification_recheck')},**fields)
        if updated is None:
            raise ValueError('The selected line, input or take changed. Refresh Editor.')
    return {'receipt':receipt,'report':report,'book_token':request.expected_book_token,'uid':chunk['uid']}
