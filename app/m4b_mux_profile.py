"""Versioned mux policy; native AAC encoding remains in FFmpeg."""


def get_m4b_mux_profile():
    return dict(muxer='pyav-17.0.0', mux_avformat='62.3.100',
        mux_avcodec='62.11.100', mux_avutil='60.8.100',
        mux_transport='delayed-moov-250000', movie_clock='lcm(rate,1000)')


def require_m4b_mux_runtime(av):
    expected = get_m4b_mux_profile()
    if av.__version__ != '17.0.0' or any(
            '.'.join(map(str, av.library_versions[name])) != expected[key]
            for name, key in [('libavformat','mux_avformat'), ('libavcodec','mux_avcodec'),
                              ('libavutil','mux_avutil')]):
        raise ValueError('M4B mux runtime differs from its approved profile')
