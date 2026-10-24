"""Private native NUT4 transport and normal seekable MP3/WAV mux policy."""
from m4b_mux_profile import get_m4b_mux_profile, require_m4b_mux_runtime


def get_pcm_mux_profile():
    profile = get_m4b_mux_profile()
    return {key:value for key,value in profile.items()
            if key in ('muxer','mux_avformat','mux_avcodec','mux_avutil')} | {
        'mux_transport':'native-nut4-syncpoints-none',
        'mp3_header':'id3v2-4-xing', 'wav_header':'rf64-auto'}


def require_pcm_mux_runtime(av):
    try:
        require_m4b_mux_runtime(av)
    except ValueError:
        raise ValueError('PCM mux runtime differs from its approved profile') from None
