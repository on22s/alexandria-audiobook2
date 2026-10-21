"""Choose the output clock without changing how source PCM is interpreted."""

# Native AAC and LAME supported-rate families, in encoder preference order.
# Keep ordinary supported rates; otherwise choose the closest supported clock.
_SAMPLE_RATES = {
    'mp3': (44100, 48000, 32000, 22050, 24000, 16000, 11025, 12000, 8000),
    'm4b': (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050,
            16000, 12000, 11025, 8000, 7350),
}


def get_audio_encoding_layout(format_name, frame_rate, channels):
    if any(type(value) is not int or value <= 0 for value in (frame_rate, channels)):
        raise ValueError('Encoding requires a positive source rate and channel count.')
    if format_name == 'wav':
        return {'frame_rate': frame_rate, 'channels': channels}
    if format_name not in _SAMPLE_RATES:
        raise ValueError('Unsupported export encoding format.')
    return {
        'frame_rate': min(_SAMPLE_RATES[format_name], key=lambda rate: abs(rate - frame_rate)),
        'channels': min(channels, 2) if format_name == 'mp3' else channels,
    }
