"""Private pre-upgrade snapshot; credentials never appear in process arguments."""
from __future__ import annotations
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from datetime import datetime
import yaml


class BackupError(RuntimeError):
    """Operator-facing messages written here, never raw credential-bearing errors."""


def backup(destination: Path, executable="mysqldump", root: Path | None = None):
    root = (root or Path(__file__).resolve().parents[1]).resolve()
    destination = destination.resolve()
    if destination.is_relative_to(root):
        raise BackupError("Choose a private backup directory outside the project")
    if destination.exists() and any(destination.iterdir()):
        raise BackupError("Backup destination must be empty")
    binary = shutil.which(executable)
    if binary is None:
        raise BackupError("mysqldump not found. Install the MySQL client or pass --mysqldump with its path")
    cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))["database"]
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    def option(value):
        return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('\n','\\n').replace('\r','\\r')+'"'
    database = str(cfg["database"])
    if database.startswith("-") or "\x00" in database:
        raise BackupError("Invalid database name")
    with tempfile.TemporaryDirectory(prefix="rinbot-mysql-") as temporary:
        auth_file = Path(temporary) / "client.cnf"
        auth_file.write_text("[client]\n" + "\n".join(f"{name}={option(cfg[name])}" for name in ("host","port","user","password")), encoding="utf-8")
        os.chmod(auth_file, 0o600)
        partial = destination / "database.sql.gz.partial"
        with tempfile.TemporaryFile() as errors:
            process = subprocess.Popen([binary, f"--defaults-extra-file={auth_file}",
                "--single-transaction", "--quick", "--routines", "--triggers", "--hex-blob", database],
                stdout=subprocess.PIPE, stderr=errors)
            try:
                with gzip.open(partial,"wb") as output:
                    shutil.copyfileobj(process.stdout,output)
                if process.wait() != 0:
                    raise BackupError("Database backup failed; check client compatibility and database permissions")
                partial.replace(destination / "database.sql.gz")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
    for name in ("config.yaml", ".env", ".env.prod", ".env.dev"):
        source = root / name
        if source.is_file():
            shutil.copy2(source,destination / name)
            os.chmod(destination / name,0o600)
    hashes = {}
    for file in destination.iterdir():
        with file.open("rb") as content:
            hashes[file.name] = hashlib.file_digest(content,"sha256").hexdigest()
    (destination/"manifest.json").write_text(json.dumps({"created_at":datetime.now().isoformat(),
        "private":True,"sha256":hashes},indent=2),encoding="utf-8")
    return destination


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination",required=True,type=Path)
    parser.add_argument("--mysqldump",default="mysqldump")
    args=parser.parse_args()
    try:
        print(f"Private backup complete: {backup(args.destination,args.mysqldump)}")
    except BackupError as error:
        print(f"Backup incomplete: {error}. Do not migrate until a verified backup exists.")
        raise SystemExit(1)
    except Exception as error:
        print(f"Backup incomplete ({type(error).__name__}); do not migrate until a verified backup exists.")
        raise SystemExit(1)
