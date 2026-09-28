"""Restore and publish datasets without deleting history or hiding failures."""
import argparse
import gzip
import json
import os
from pathlib import Path
import subprocess


def gh(*args):
    return subprocess.check_output(['gh', *args], text=True, encoding='utf-8')


def newest(releases, prefix):
    candidates = [r for r in releases if r['tag_name'].startswith(prefix)
                  and not r.get('draft') and not r.get('prerelease')]
    return max(candidates, key=lambda r: (r.get('published_at') or '', r['tag_name']), default=None)


def validate_files(paths):
    for path in paths:
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError(f'Missing or empty data: {path}')
        if path.suffix == '.gz':
            with gzip.open(path, 'rb') as stream:
                while stream.read(1024 * 1024):
                    pass


def restore(prefix, out):
    repo = os.environ['GITHUB_REPOSITORY']
    pages = json.loads(gh('api', f'repos/{repo}/releases', '--paginate', '--slurp'))
    release = newest([r for page in pages for r in page], prefix)
    if release is None or not release.get('assets'):
        raise RuntimeError(f'No complete baseline for {prefix}')
    out.mkdir(parents=True, exist_ok=True)
    gh('release', 'download', release['tag_name'], '--repo', repo, '--dir', str(out), '--clobber')
    paths = [out / a['name'] for a in release['assets']]
    for path, asset in zip(paths, release['assets']):
        if path.stat().st_size != asset['size']:
            raise RuntimeError(f'Size mismatch: {path}')
    validate_files(paths)
    print(f'Restored {release["tag_name"]}: {len(paths)} assets')


def publish(tag, out, pattern):
    repo = os.environ['GITHUB_REPOSITORY']
    paths = sorted(out.glob(pattern))
    if not paths:
        raise RuntimeError('No files to publish')
    validate_files(paths)
    # Unique tags preserve prior snapshots. Incomplete uploads remain drafts.
    gh('release', 'create', tag, '--repo', repo, '--title', tag,
       '--notes', 'Validated data snapshot', '--draft')
    gh('release', 'upload', tag, '--repo', repo, *map(str, paths))
    gh('release', 'edit', tag, '--repo', repo, '--draft=false', '--latest=false')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('action', choices=['restore', 'publish'])
    ap.add_argument('name')
    ap.add_argument('--out', type=Path, default=Path('out'))
    ap.add_argument('--pattern', default='*')
    a = ap.parse_args()
    if a.action == 'restore':
        restore(a.name, a.out)
    else:
        publish(a.name, a.out, a.pattern)
