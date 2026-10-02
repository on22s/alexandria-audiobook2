"""Enrichment selection rule shared by preparer HTTP requests and CLI arguments."""

ENRICHMENT_CATEGORIES = ('enrich_speaker_attribution', 'enrich_narration_style', 'enrich_emotional_tone')


def validate_preparer_enrichment_settings(settings):
    """Refuse enrichment with nothing selected; it would otherwise run as a silent no-op."""
    if settings.get('enrich_with_llm') and not any(settings.get(name) for name in ENRICHMENT_CATEGORIES):
        raise ValueError('Select at least one enrichment category.')
