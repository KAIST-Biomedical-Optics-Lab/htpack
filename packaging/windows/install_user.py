"""User-local install of the complete portable bundle; no PATH/registry edits."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


def digest(path):
    value=hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda:f.read(8*2**20),b""):value.update(block)
    return value.hexdigest()


def check(root):
    manifest=json.loads((root/"bundle-manifest.json").read_text(encoding="utf-8"))
    for name,expected in manifest["files"].items():
        path=(root/name).resolve()
        if not path.is_relative_to(root.resolve()) or digest(path)!=expected:
            raise ValueError("Bundle checksum mismatch: "+name)
    return manifest


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--destination",type=Path)
    args=parser.parse_args();source=args.source.resolve();manifest=check(source)
    if args.destination is None:
        base=os.environ.get("LOCALAPPDATA")
        if not base:raise ValueError("LOCALAPPDATA unavailable; provide --destination")
        destination=Path(base)/"Programs"/"HTPack"/manifest["version"]
    else:destination=args.destination.resolve()
    if destination.exists():
        if check(destination)!=manifest:raise ValueError("Existing install differs")
        print(json.dumps(dict(status="already_installed",path=str(destination)),ensure_ascii=False))
        return
    if destination.is_relative_to(source):raise ValueError("Destination cannot be inside source bundle")
    destination.parent.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix=".htpack-install-",dir=destination.parent))
    try:
        shutil.copytree(source,staging,dirs_exist_ok=True)
        check(staging)
        if destination.exists():raise FileExistsError("Destination appeared during install")
        staging.rename(destination)
    finally:
        if staging.exists():shutil.rmtree(staging)
    print(json.dumps(dict(status="installed",path=str(destination),
        command=str(destination/"htpack.cmd"),PATH_changed=False,registry_changed=False),ensure_ascii=False))


if __name__=="__main__":
    try:main()
    except (ValueError,OSError,KeyError) as error:
        print(str(error),file=sys.stderr);sys.exit(1)

