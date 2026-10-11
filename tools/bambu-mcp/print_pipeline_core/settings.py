"""Canonical Bambu setting strings; validation remains with each catalog (MIT)."""


def encode_setting(key, value):
    text = str(int(value)) if isinstance(value, bool) else str(value)
    return text.rstrip('%') + '%' if key == 'sparse_infill_density' else text


def encode_settings(settings):
    return {key: encode_setting(key, value) for key, value in settings.items()}


def normalize_settings(settings):
    """Preserve reported numeric types while normalizing boolean settings."""
    return {key: encode_setting(key, value) if isinstance(value, bool) else value
            for key, value in settings.items()}
