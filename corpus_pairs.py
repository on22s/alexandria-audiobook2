"""One pair validator shared by the corpus dry run and the corpus runner."""


class CorpusPairError(ValueError):
    """A pair row the corpus tools cannot use, carrying its audio for reporting."""

    def __init__(self, message, audio=None):
        super().__init__(message)
        self.audio = audio


def validate_corpus_pair(pair):
    """Return (audio, source) for a usable pair or raise CorpusPairError.

    The dry-run pre-scan and the corpus runner both classify pairs here so
    their reports cannot disagree about which pairs are usable.
    """
    if not isinstance(pair, dict):
        raise CorpusPairError('pair must be an object')
    audio = pair.get('audio')
    row_audio = audio if isinstance(audio, str) and audio else None
    if row_audio is None:
        raise CorpusPairError('pair audio must be a nonempty path string', row_audio)
    try:
        source = pair['source']
    except KeyError:
        raise CorpusPairError("'source'", row_audio) from None
    if source is not None and not isinstance(source, str):
        raise CorpusPairError('pair source must be a path string or null', row_audio)
    return row_audio, source
