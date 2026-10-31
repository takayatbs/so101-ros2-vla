#!/usr/bin/env python3
"""Copy public settings to ignored local profiles without overwriting existing files."""
import argparse
from pathlib import Path


def initialize(root):
    sources = [root/'hardware/robot.yaml', root/'control/teleop.yaml', root/'collection/record.yaml',
               *sorted((root/'cameras').glob('*.yaml'))]
    created = []
    for source in sources:
        if source.name.endswith('.local.yaml'):
            continue
        destination = source.with_name(source.stem+'.local.yaml')
        content = source.read_text()
        try:
            with destination.open('x') as output:
                output.write(content)
        except FileExistsError:
            continue
        created.append(destination)
    return created


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config-root', type=Path, default=Path(__file__).resolve().parents[1]/'config')
    args = parser.parse_args()
    for path in initialize(args.config_root):
        print('Created '+str(path))
    print('Existing local files were preserved. Edit local device IDs and task settings before real use.')
