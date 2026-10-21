"""Independent chapter geometry and admission of previously recorded plans."""
from audio_streaming import (get_combined_source_plan, get_pcm_shape,
                             save_trimmed_pcm_sources)
from export_quality import (get_export_quality_plan_from_record,
                            get_pcm_export_encoding_plan)


def get_chapter_pcm_plan(pairs, pause_defaults, cancel_check, *, already_trimmed=False):
    segments = [segment for _, segment in pairs]
    if not already_trimmed:
        segments = save_trimmed_pcm_sources(segments, cancel_check, None)
    return get_combined_source_plan(segments, [row['speaker'] for row, _ in pairs],
        *pause_defaults, [row.get('pause_after') for row, _ in pairs], cancel_check)


def get_chapter_quality_plan(pcm_plan, format_name, options):
    if pcm_plan is None:
        raise ValueError('Chapter sources have no independent PCM frame plan; rebuild the chapter.')
    return get_pcm_export_encoding_plan(pcm_plan, format_name=format_name, options=options)


def ensure_chapter_piece_matches_plan(piece, pcm_plan):
    if pcm_plan is None or get_pcm_shape(piece) != ((pcm_plan['channels'], pcm_plan['frame_rate'],
            pcm_plan['sample_width']), pcm_plan['frames']):
        raise ValueError('Rendered chapter PCM differs from its independent source-frame plan.')


def get_recorded_chapter_quality_plan(row, options):
    """Keep the old render clock, applying only the new validation policy."""
    try:
        record = row['export_quality_plan']
        plan = get_export_quality_plan_from_record({**record, 'options': options})
        if plan['format'] not in ('wav', 'mp3') or plan['source_shape'] is None:
            return None
        return plan
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def get_chapter_plan_duration_ms(plan):
    source = plan['source_shape']
    return 1000 * source['frames'] / source['sample_rate']


def get_chapter_durations_ms(plans, trimmed_pairs, pause_defaults, cancel_check):
    """Use each chapter's own promotion clock, including unselected groups."""
    result = []
    for plan in plans:
        first, last = plan['chunks']
        pcm = get_chapter_pcm_plan(trimmed_pairs[first:last + 1], pause_defaults,
                                   cancel_check, already_trimmed=True)
        if pcm is None:
            raise ValueError('Chapter sources have no independent PCM frame plan; rebuild the chapter.')
        result.append(1000 * pcm['frames'] / pcm['frame_rate'])
    return result
