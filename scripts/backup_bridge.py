"""Offline backup/restore CLI; does not load a secret environment file or start Store."""

import argparse
from pathlib import Path
import sqlite3
import zipfile

from heimdall_bridge.config import Config
from heimdall_bridge.operations import backup_database, restore_database
from heimdall_bridge.knowledge_backup import backup_bundle, restore_bundle


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).resolve().parents[1] / "config.toml")
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--destination-dir", type=Path, required=True)
    backup.add_argument("--include-assets", action="store_true", help="Consistent DB and original attachment ZIP bundle")
    restore = commands.add_parser("restore")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    restore.add_argument("--safe-directory", type=Path)
    bundle = commands.add_parser("restore-bundle")
    bundle.add_argument("--backup", type=Path, required=True)
    bundle.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.config, live=False)
        if args.command == "backup":
            path = (backup_bundle if args.include_assets else backup_database)(config, args.destination_dir)
        elif args.command == "restore":
            path = restore_database(config, args.backup, args.destination, safe_directory=args.safe_directory)
        else:
            path = restore_bundle(config, args.backup, args.destination)
    except (ValueError, OSError, sqlite3.Error, KeyError, zipfile.BadZipFile) as exc:
        # Exception strings can contain config/environment values; report only type.
        print(f"{args.command} failed ({type(exc).__name__}); check paths, permissions, disk and database integrity")
        return 1
    print(f"{args.command} verified: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
