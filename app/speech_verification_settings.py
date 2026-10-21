"""Off-default speech-verification execution policy, outside acoustic identity."""


def get_speech_verification_settings(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or set(value) - {'enabled'} or type(value.get('enabled', False)) is not bool:
        raise ValueError('Speech verification settings require a boolean enabled field.')
    return {'enabled': value.get('enabled', False)}


def get_enabled_speech_verification_settings(value=None):
    settings = get_speech_verification_settings(value)
    return settings if settings['enabled'] else None
