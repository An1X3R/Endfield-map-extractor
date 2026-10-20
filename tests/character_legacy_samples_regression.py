"""Exercise real mixed-format auxiliary samples and reject altered legacy evidence."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src/character_runtime'))
from animator_auxiliary_layers import object_row, read_json, rows
from animator_selected_avatar_bindings import load_selected_avatar_clip


def verify(samples: Path, avatar_path: Path, hierarchy_path: Path, output: Path) -> None:
    avatar = object_row(read_json(avatar_path), 'avatar')
    hierarchy = rows(read_json(hierarchy_path), 'hierarchy')
    results: list[dict[str, object]] = []
    output.parent.mkdir(parents=True, exist_ok=True)
    for directory in sorted(path for path in samples.iterdir() if path.is_dir()):
        clip = load_selected_avatar_clip(directory, avatar, hierarchy)
        legacy_path = directory / 'legacy_dense_clip.json'
        legacy = object_row(read_json(legacy_path), 'legacy') if legacy_path.is_file() else None
        results.append({'index': clip.index, 'frames': len(clip.times), 'mapped_tracks': len(clip.paths),
            'encoding': 'legacy_dense_streamed_constant' if legacy is not None else 'new_acl',
            'sample_differs_from_reference': any(pose != reference for frame in clip.poses
                for pose, reference in zip(frame, clip.references, strict=True))})
        if legacy is None:
            continue
        with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
            copied = Path(temporary) / directory.name
            shutil.copytree(directory, copied)
            for field, altered in (('rawDataSha256', '0' * 64), ('m_CurveCount', int(legacy['m_CurveCount']) + 1)):
                damaged = legacy | {field: altered}
                (copied / legacy_path.name).write_text(json.dumps(damaged), encoding='utf-8')
                try:
                    load_selected_avatar_clip(copied, avatar, hierarchy)
                except ValueError:
                    pass
                else:
                    raise AssertionError(f'Altered legacy source evidence was accepted: index={clip.index}, field={field}')
    if not any(row['encoding'] == 'new_acl' for row in results) or not any(
            row['encoding'] == 'legacy_dense_streamed_constant' and row['sample_differs_from_reference'] for row in results):
        raise AssertionError('Real regression needs both formats and non-reference legacy samples')
    output.write_text(json.dumps({'status': 'passed', 'clips': results,
        'legacy_identity_and_dimensions_tamper_rejected': True}, indent=2), encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ('samples', 'avatar', 'hierarchy', 'output'):
        parser.add_argument('--' + field, type=Path, required=True)
    args = parser.parse_args()
    verify(args.samples, args.avatar, args.hierarchy, args.output)


if __name__ == '__main__':
    main()
