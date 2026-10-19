"""Shared channel mapping applied before resampling or mono conversion."""
import json
import subprocess

AUDIO_CHANNELS = ('mix', 'left', 'right')


def get_audio_channel_filter(channel):
    if channel not in AUDIO_CHANNELS:
        raise ValueError('Choose mixed, left or right audio.')
    return [] if channel == 'mix' else ['-af', 'pan=mono|c0=c' + ('0' if channel == 'left' else '1')]


def get_audio_channel_count(path, capture=None):
    capture = capture or subprocess.run
    result = capture(['ffprobe', '-v', 'error', '-select_streams', 'a:0',
                             '-show_entries', 'stream=channels', '-of', 'json', str(path)],
                            **({'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE} if capture is subprocess.run else {}), timeout=15)
    result.check_returncode()
    streams = json.loads(result.stdout)['streams']
    count = streams[0]['channels'] if streams else None
    if type(count) is not int or not 1 <= count <= 65535:
        raise ValueError('Could not determine the source audio channels.')
    return count


def get_validated_audio_channel_filter(path, channel, capture=None):
    arguments = get_audio_channel_filter(channel)
    if channel != 'mix':
        count = get_audio_channel_count(path, capture)
        if count < (2 if channel == 'right' else 1):
            raise ValueError('The selected audio channel is unavailable in this recording.')
    return arguments
