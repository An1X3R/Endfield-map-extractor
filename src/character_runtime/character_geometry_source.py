"""Select a versioned geometry payload or an existing legacy FBX cache."""
from pathlib import Path


def geometry_source(directory: Path) -> Path:
    """Prefer an explicit portable payload without hiding its validation failures."""
    manifest = directory / 'model.geometry.json'
    if manifest.is_file():
        return manifest
    legacy = directory / 'model.fbx'
    if legacy.is_file():
        return legacy
    raise FileNotFoundError(f'Character geometry payload is missing: {directory}')
