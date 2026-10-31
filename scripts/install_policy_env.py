"""Create the optional model environment without replacing ROS's torch build."""
import json
import os
from pathlib import Path
import re
import shutil
import site
import subprocess
import sys
import tempfile
import tomllib
import venv

LEROBOT_REPOSITORY = 'https://github.com/huggingface/lerobot.git'
LEROBOT_REVISION = '22bd7a2f489b367d8df42de803b1e8c4ca63a3f9'
PROJECT = Path(__file__).resolve().parents[1]


def prepare_source(project=PROJECT, repository=LEROBOT_REPOSITORY, revision=LEROBOT_REVISION):
    """Fetch a pinned source into this project; never depend on a sibling checkout."""
    if not re.fullmatch(r'[0-9a-f]{40}', revision):
        raise ValueError('LeRobot revision must be a full commit hash')
    parent = Path(project)/'data/envs'
    target = parent/('lerobot-source-'+revision[:12])

    def verify(folder):
        if not (folder/'.git').exists():
            raise ValueError('Cached LeRobot source is not a Git checkout: '+str(folder))
        commit = subprocess.check_output(['git', '-C', str(folder), 'rev-parse', 'HEAD'], text=True).strip()
        if commit != revision:
            raise ValueError('Cached LeRobot source differs from the pinned revision')
        if subprocess.run(['git', '-C', str(folder), 'diff', '--quiet', 'HEAD', '--'], check=False).returncode:
            raise ValueError('Cached LeRobot tracked files were modified; preserve your changes and use a clean cache')
        project_info = tomllib.loads((folder/'pyproject.toml').read_text())
        if project_info.get('project', {}).get('name') != 'lerobot':
            raise ValueError('Expected a LeRobot project')

    if target.exists():
        verify(target)
        return target
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.lerobot-source.partial-', dir=parent))
    try:
        print('Preparing pinned LeRobot source: '+revision, flush=True)
        subprocess.run(['git', 'init', '--quiet', str(temporary)], check=True)
        subprocess.run(['git', '-C', str(temporary), 'remote', 'add', 'origin', repository], check=True)
        subprocess.run(['git', '-C', str(temporary), 'fetch', '--quiet', '--depth', '1', 'origin', revision], check=True)
        subprocess.run(['git', '-C', str(temporary), 'checkout', '--quiet', '--detach', 'FETCH_HEAD'], check=True)
        verify(temporary)
        temporary.rename(target)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return target


def main():
    source = prepare_source()
    root = PROJECT/'data/envs/policies'
    if not (root/'bin/python').exists():
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(root)
    python = str(root/'bin/python')
    target = subprocess.check_output([python, '-c', 'import sysconfig; print(sysconfig.get_path("purelib"))'], text=True).strip()
    # Parent venv torch/safetensors/ROS packages remain available to this process.
    Path(target, 'so101-base.pth').write_text('\n'.join(site.getsitepackages())+'\n')
    import torch
    constraint = root/'constraints.txt'
    constraint.write_text('torch=='+torch.__version__.split('+')[0]+'\n')
    # PyPI's aarch64 torchvision wheel is CPU-only. Match the existing torch
    # CUDA flavor, otherwise torchvision::nms fails even before model import.
    major, minor = map(int, torch.__version__.split('+')[0].split('.')[:2])
    if major != 2:
        raise RuntimeError('Choose a matching torchvision build for torch '+torch.__version__)
    vision = f'0.{minor+15}.0'
    flavor = ('cu'+torch.version.cuda.replace('.', '')) if torch.version.cuda else 'cpu'
    index = os.environ.get('SO101_TORCH_INDEX', f'https://download.pytorch.org/whl/{flavor}')
    subprocess.run([python, '-m', 'pip', 'install', '--no-deps', '--force-reinstall',
                    'torchvision=='+vision, '--index-url', index], check=True)
    extra = sys.argv[1]
    package = str(source)+('['+extra+']' if extra else '')
    subprocess.run([python, '-m', 'pip', 'install', '--constraint', str(constraint), package], check=True)
    subprocess.run([python, '-c', 'from lerobot.policies.factory import get_policy_class; import torch; print(torch.__version__)'], check=True)
    packages = subprocess.check_output([python, '-m', 'pip', 'freeze'], text=True)
    (root/'requirements.installed.txt').write_text(packages)
    (root/'so101-install.json').write_text(json.dumps({'extras': extra, 'torch': torch.__version__,
        'source': str(source), 'source_repository': LEROBOT_REPOSITORY,
        'source_revision': LEROBOT_REVISION, 'requirements': 'requirements.installed.txt'}, indent=2))


if __name__ == '__main__':
    main()
