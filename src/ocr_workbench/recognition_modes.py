"""Explicit recognition request modes; local text requests are GLM-only."""


def validate_recognition_mode(engine, mode='page'):
    if type(mode) is not str or mode not in {'page', 'local_text'}:
        raise ValueError('Unsupported recognition mode')
    if mode == 'local_text' and engine != 'glm':
        raise ValueError('Local text recognition is supported only by GLM')
    return mode
