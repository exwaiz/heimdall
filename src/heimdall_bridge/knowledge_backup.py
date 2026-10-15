"""Consistent bridge DB + immutable attachment bundle. Fresh restore paths only."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing

from .operations import _safe_directory, _no_symlinks, _readonly, _check_database, backup_database

ASSET_NAME = re.compile(r"asset-[a-f0-9]{32}\.(?:png|jpg|pdf|zip|doc|rtf|txt|bin)")


def _rows(conn):
    return conn.execute("SELECT asset_id,path,sha256,size_bytes FROM assets WHERE state='stored' ORDER BY asset_id").fetchall()


def _stream(source, output, digest=None, maximum=20000000):
    total, checksum = 0, hashlib.sha256()
    while chunk := source.read(65536):
        total += len(chunk)
        if total > maximum:
            raise ValueError("backup_size_limit")
        checksum.update(chunk)
        output.write(chunk)
    if digest is not None and checksum.hexdigest() != digest:
        raise ValueError("backup_hash_mismatch")
    return total


def backup_bundle(config, destination_dir):
    directory = _safe_directory(config, destination_dir)
    snapshot = backup_database(config, directory)
    target = None
    try:
        # A fresh online copy may inherit WAL journal mode. Make this private
        # snapshot self-contained before hashing/zipping, never the live DB.
        with closing(sqlite3.connect(snapshot)) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=DELETE")
        if snapshot.stat().st_size > 100000000:
            raise ValueError("backup_database_size_limit")
        with closing(_readonly(snapshot)) as conn:
            rows = _rows(conn)
        total = sum(row[3] for row in rows)
        if len(rows) > 1000 or total > 200000000 or shutil.disk_usage(directory).free < total + snapshot.stat().st_size + config.min_disk_bytes:
            raise ValueError("backup_size_limit")
        fd, name = tempfile.mkstemp(prefix="knowledge-backup-", suffix=".zip", dir=directory)
        target = Path(name)
        os.close(fd)
        manifest = {"version": 1, "assets": [], "database_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest()}
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_STORED, allowZip64=False) as archive:
            archive.write(snapshot, "bridge.sqlite3")
            for asset_id, path, digest, size in rows:
                source = _no_symlinks(path)
                if source.parent != config.data_dir / "assets" or not ASSET_NAME.fullmatch(source.name) or type(size) is not int or not 0 < size <= 20000000:
                    raise ValueError("unsafe_backup_asset")
                fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, "rb") as handle:
                    info = os.fstat(handle.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
                        raise ValueError("unsafe_backup_asset")
                    member = "assets/" + source.name
                    with archive.open(member, "w") as output:
                        if _stream(handle, output, digest) != size:
                            raise ValueError("backup_size_mismatch")
                manifest["assets"].append({"asset_id": asset_id, "name": source.name, "sha256": digest, "size": size})
            archive.writestr("manifest.json", json.dumps(manifest))
        with target.open("rb") as handle:
            os.fsync(handle.fileno())
        return target
    except BaseException:
        if target is not None:
            target.unlink(missing_ok=True)
        raise
    finally:
        snapshot.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm", "-journal"):
            Path(str(snapshot) + suffix).unlink(missing_ok=True)


def restore_bundle(config, backup, destination):
    source = _no_symlinks(backup)
    _safe_directory(config, source.parent)
    target = _no_symlinks(destination)
    _safe_directory(config, target.parent)
    data = config.data_dir.resolve()
    if target.exists() or target.is_relative_to(data) or data.is_relative_to(target):
        raise ValueError("restore_destination_must_be_fresh_and_separate")
    # Validation before creating any destination. No extractall or trusted paths.
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        names = [info.filename for info in entries]
        if len(entries) > 1002 or len(set(names)) != len(names) or any(stat.S_ISLNK(info.external_attr >> 16) or info.file_size > 100000000 for info in entries):
            raise ValueError("invalid_backup_archive")
        info = archive.getinfo("manifest.json")
        if info.file_size > 1000000:
            raise ValueError("invalid_backup_manifest")
        manifest = json.loads(archive.read(info))
        if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("assets"), list):
            raise ValueError("invalid_backup_manifest")
        assets = manifest["assets"]
        expected = {"bridge.sqlite3", "manifest.json"}
        ids, total = set(), 0
        for asset in assets:
            if (not isinstance(asset, dict) or set(asset) != {"asset_id", "name", "sha256", "size"}
                or type(asset["asset_id"]) is not int or asset["asset_id"] in ids
                or not isinstance(asset["name"], str) or not ASSET_NAME.fullmatch(asset["name"])
                or not isinstance(asset["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", asset["sha256"])
                or type(asset["size"]) is not int or not 0 < asset["size"] <= 20000000):
                raise ValueError("invalid_backup_manifest")
            member = "assets/" + asset["name"]
            if member in expected or archive.getinfo(member).file_size != asset["size"]:
                raise ValueError("invalid_backup_manifest")
            expected.add(member)
            ids.add(asset["asset_id"])
            total += asset["size"]
        if set(names) != expected or total > 200000000 or shutil.disk_usage(target.parent).free < total + archive.getinfo("bridge.sqlite3").file_size + config.min_disk_bytes:
            raise ValueError("invalid_backup_archive")
        if not isinstance(manifest.get("database_sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", manifest["database_sha256"]):
            raise ValueError("invalid_backup_manifest")
        # A reserved sibling directory keeps failed restoration out of the final path.
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=target.parent) as temporary:
            staging = Path(temporary)
            asset_directory = staging / "assets"
            asset_directory.mkdir(mode=0o700)
            database = staging / "bridge.sqlite3"
            fd = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as output, archive.open("bridge.sqlite3") as input_file:
                _stream(input_file, output, manifest["database_sha256"], maximum=100000000)
            with closing(_readonly(database)) as conn:
                _check_database(conn)
                rows = _rows(conn)
            db_assets = {row[0]: row for row in rows}
            if set(db_assets) != ids:
                raise ValueError("backup_database_manifest_mismatch")
            for asset in assets:
                _, original, digest, size = db_assets[asset["asset_id"]]
                if Path(original).parent.name != "assets" or Path(original).name != asset["name"] or (digest, size) != (asset["sha256"], asset["size"]):
                    raise ValueError("backup_database_manifest_mismatch")
                path = asset_directory / asset["name"]
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as output, archive.open("assets/" + asset["name"]) as input_file:
                    _stream(input_file, output, asset["sha256"])
            with closing(sqlite3.connect(database)) as conn:
                for asset in assets:
                    conn.execute("UPDATE assets SET path=? WHERE asset_id=?", (str(target / "assets" / asset["name"]), asset["asset_id"]))
                conn.commit()
                _check_database(conn)
            # target did not exist, and os.mkdir reserves it without replacement.
            target.mkdir(mode=0o700)
            try:
                (target / "assets").mkdir(mode=0o700)
                os.rename(database, target / "bridge.sqlite3")
                for asset in assets:
                    os.rename(asset_directory / asset["name"], target / "assets" / asset["name"])
            except BaseException:
                # Only this invocation's newly reserved target is removed.
                shutil.rmtree(target)
                raise
    return target
