"""Backups: one tar.gz holding every vault *with* its .git history.

Restore validates the archive, moves the current vault contents aside to
STATE_DIR/pre-restore/<timestamp>/ (nothing is deleted), then unpacks.
The same archive works for recreating a server or running offline on a laptop.
"""

from __future__ import annotations

import io
import json
import shutil
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from . import gitrepo
from .config import STATE_DIR
from .store import VAULTS, Vault, init_vaults


def create_backup(fileobj, vaults: Optional[Iterable[Vault]] = None) -> None:
    """Write a tar.gz of the given vaults (default: all) to fileobj."""
    vaults = list(vaults or VAULTS.values())
    manifest = {
        "format": "forest-backup/1",
        "created": datetime.now().isoformat(timespec="seconds"),
        "vaults": [v.name for v in vaults],
    }
    with tarfile.open(fileobj=fileobj, mode="w:gz") as tar:
        data = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        for v in vaults:
            with v.lock:
                tar.add(str(v.root), arcname=f"vaults/{v.name}",
                        filter=lambda ti: None if ti.issym() or ti.islnk() else ti)


def backup_filename() -> str:
    return f"forest-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.tar.gz"


def _safe_members(tar: tarfile.TarFile):
    for m in tar.getmembers():
        p = Path(m.name)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(f"Unsafe path in archive: {m.name}")
        if not (m.isfile() or m.isdir()):
            continue  # skip links, devices
        yield m


def restore_backup(archive: Path, only: Optional[list[str]] = None) -> dict:
    """Restore vaults from a backup archive. Returns {restored: [...], moved_aside_to: path}."""
    if not VAULTS:
        init_vaults()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    aside = STATE_DIR / "pre-restore" / stamp
    restored = []
    with tempfile.TemporaryDirectory(dir=STATE_DIR if STATE_DIR.exists() else None) as tmp:
        tmpdir = Path(tmp)
        with tarfile.open(archive, mode="r:gz") as tar:
            members = list(_safe_members(tar))
            names = {m.name for m in members}
            if "manifest.json" not in names:
                raise ValueError("Not a forest backup (manifest.json missing)")
            tar.extractall(tmpdir, members=members, filter="data")
        manifest = json.loads((tmpdir / "manifest.json").read_text())
        for name in manifest.get("vaults", []):
            if only and name not in only:
                continue
            if name not in VAULTS:
                continue  # vault not configured on this server
            src = tmpdir / "vaults" / name
            if not src.is_dir():
                continue
            v = VAULTS[name]
            with v.lock:
                # move current contents aside (works even when root is a mount point)
                (aside / name).mkdir(parents=True, exist_ok=True)
                for child in list(v.root.iterdir()):
                    shutil.move(str(child), str(aside / name / child.name))
                for child in list(src.iterdir()):
                    shutil.move(str(child), str(v.root / child.name))
                gitrepo.ensure_repo(v.root)
            restored.append(name)
    return {"restored": restored, "moved_aside_to": str(aside)}
