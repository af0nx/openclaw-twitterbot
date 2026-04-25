#!/usr/bin/env python3
"""Authoritative deploy helper for the CS2 bot runtime.

Keeps one command responsible for:
1. syncing the canonical repo into an optional mirror/runtime tree
2. restarting selected PM2 services with --update-env
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SYNC_PATHS = [
    'scripts',
    'config',
    'schema.sql',
    'ecosystem.config.js',
    'ecosystem.config.cjs',
    'requirements.txt',
]


def _autodetect_mirror_root() -> Path | None:
    env_root = os.getenv('BOT_MIRROR_DIR')
    if env_root:
        return Path(env_root).expanduser().resolve()
    candidate = Path('/home/ubuntu/skinbethub_twitter')
    if candidate.exists() and candidate.resolve() != REPO_ROOT:
        return candidate.resolve()
    return None


def _copy_file(src: Path, dst: Path) -> int:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        src_stat = src.stat()
        dst_stat = dst.stat()
        if src_stat.st_size == dst_stat.st_size and int(src_stat.st_mtime) == int(dst_stat.st_mtime):
            return 0
    shutil.copy2(src, dst)
    return 1


def _sync_path(src_root: Path, dst_root: Path, relative_path: str) -> int:
    src = src_root / relative_path
    dst = dst_root / relative_path
    changed = 0
    if not src.exists():
        return 0
    if src.is_file():
        return _copy_file(src, dst)
    for path in src.rglob('*'):
        rel = path.relative_to(src)
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        changed += _copy_file(path, target)
    return changed


def sync_repo(src_root: Path, dst_root: Path, relative_paths: list[str]) -> int:
    if src_root.resolve() == dst_root.resolve():
        return 0
    changed = 0
    for relative_path in relative_paths:
        changed += _sync_path(src_root, dst_root, relative_path)
    return changed


def restart_services(services: list[str], cwd: Path, dry_run: bool) -> None:
    for service in services:
        cmd = ['pm2', 'restart', service, '--update-env']
        print(f"restart: {' '.join(cmd)}")
        if not dry_run:
            subprocess.run(cmd, cwd=str(cwd), check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description='Sync canonical bot code and restart PM2 services')
    parser.add_argument('--runtime-root', default=os.getenv('BOT_RUNTIME_DIR', str(REPO_ROOT)))
    parser.add_argument('--mirror-root', default=None)
    parser.add_argument('--paths', nargs='*', default=DEFAULT_SYNC_PATHS)
    parser.add_argument('--services', nargs='*', default=[])
    parser.add_argument('--skip-sync', action='store_true')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    runtime_root = Path(args.runtime_root).expanduser().resolve()
    mirror_root = Path(args.mirror_root).expanduser().resolve() if args.mirror_root else _autodetect_mirror_root()

    print(f"canonical: {REPO_ROOT}")
    print(f"runtime:   {runtime_root}")
    if mirror_root:
        print(f"mirror:    {mirror_root}")

    if not args.skip_sync:
        changed = sync_repo(REPO_ROOT, runtime_root, args.paths)
        print(f"synced -> runtime changed_files={changed}")
        if mirror_root:
            mirror_changed = sync_repo(REPO_ROOT, mirror_root, args.paths)
            print(f"synced -> mirror changed_files={mirror_changed}")

    if args.services:
        restart_services(args.services, runtime_root, args.dry_run)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())