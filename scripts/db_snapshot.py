"""Export and import local Docker Compose Postgres database snapshots.

The snapshots are intended for small personal/dev databases that need to move
between machines through git. They can contain application secrets, credentials,
tokens, and user data, so only commit them to a private repository.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_DIR = REPO_ROOT / "database_exports"


@dataclass(frozen=True)
class DatabaseTarget:
    name: str
    service: str
    user: str
    database: str
    artifact_name: str


DATABASES = {
    "agency": DatabaseTarget(
        name="agency",
        service="postgres",
        user="postgres",
        database="agency",
        artifact_name="agency.dump",
    ),
    "langfuse": DatabaseTarget(
        name="langfuse",
        service="langfuse-postgres",
        user="postgres",
        database="postgres",
        artifact_name="langfuse-postgres.dump",
    ),
}


def run(
    command: list[str],
    *,
    stdin=None,
    stdout=None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    kwargs = {
        "cwd": REPO_ROOT,
        "stdin": stdin,
        "stdout": stdout,
        "stderr": subprocess.PIPE if capture else None,
        "text": capture,
        "check": False,
    }
    if capture:
        kwargs["stdout"] = subprocess.PIPE
    try:
        result = subprocess.run(command, **kwargs)
    except FileNotFoundError as exc:
        raise SystemExit(f"Required command not found: {command[0]}") from exc
    if result.returncode != 0:
        if capture and result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        raise SystemExit(result.returncode)
    return result


def compose_exec(
    target: DatabaseTarget,
    args: list[str],
    *,
    stdin=None,
    stdout=None,
    capture: bool = False,
):
    return run(
        ["docker", "compose", "exec", "-T", target.service, *args],
        stdin=stdin,
        stdout=stdout,
        capture=capture,
    )


def selected_targets(name: str) -> list[DatabaseTarget]:
    if name == "all":
        return list(DATABASES.values())
    return [DATABASES[name]]


def timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def write_manifest(target: DatabaseTarget, dump_path: Path, exported_at: str) -> Path:
    manifest_path = dump_path.with_suffix(".json")
    manifest = {
        "exported_at": exported_at,
        "database": target.database,
        "docker_compose_service": target.service,
        "format": "pg_dump custom",
        "artifact": dump_path.name,
        "warning": "This file can represent sensitive local application data. Commit only to a private repository.",
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def export_database(target: DatabaseTarget, *, timestamped: bool, git_add: bool) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    exported_at = timestamp()
    dump_path = SNAPSHOT_DIR / target.artifact_name
    temp_path = dump_path.with_name(f"{dump_path.name}.tmp")

    print(
        f"Exporting {target.name} from Docker Compose service "
        f"{target.service} to {dump_path}"
    )
    try:
        with temp_path.open("wb") as dump_file:
            compose_exec(
                target,
                [
                    "pg_dump",
                    "-U",
                    target.user,
                    "-d",
                    target.database,
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                ],
                stdout=dump_file,
            )
        temp_path.replace(dump_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise

    paths_to_stage = [dump_path, write_manifest(target, dump_path, exported_at)]

    if timestamped:
        archive_path = SNAPSHOT_DIR / f"{dump_path.stem}-{exported_at}{dump_path.suffix}"
        shutil.copyfile(dump_path, archive_path)
        paths_to_stage.append(archive_path)
        paths_to_stage.append(write_manifest(target, archive_path, exported_at))

    if git_add:
        run(["git", "add", *[str(path.relative_to(REPO_ROOT)) for path in paths_to_stage]])

    print(f"Exported {target.name}.")


def ensure_database_exists(target: DatabaseTarget) -> None:
    check = compose_exec(
        target,
        [
            "psql",
            "-U",
            target.user,
            "-d",
            "postgres",
            "-tAc",
            f"SELECT 1 FROM pg_database WHERE datname = '{target.database}'",
        ],
        capture=True,
    )
    if check.stdout.strip() == "1":
        return
    compose_exec(target, ["createdb", "-U", target.user, target.database])


def import_database(target: DatabaseTarget, *, dump_file: Path | None, yes: bool) -> None:
    dump_path = dump_file or (SNAPSHOT_DIR / target.artifact_name)
    if not dump_path.exists():
        raise SystemExit(f"Snapshot not found: {dump_path}")

    if not yes:
        print(
            f"Refusing to import without --yes because this will overwrite objects in "
            f"{target.service}/{target.database}.",
            file=sys.stderr,
        )
        raise SystemExit(2)

    print(
        f"Importing {dump_path} into Docker Compose service "
        f"{target.service}, database {target.database}"
    )
    ensure_database_exists(target)
    with dump_path.open("rb") as snapshot:
        compose_exec(
            target,
            [
                "pg_restore",
                "-U",
                target.user,
                "-d",
                target.database,
                "--clean",
                "--if-exists",
                "--no-owner",
                "--no-privileges",
                "--exit-on-error",
            ],
            stdin=snapshot,
        )
    print(f"Imported {target.name}.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export/import local Docker Compose Postgres snapshots for git-based machine sync."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser("export", help="Export one or more databases to database_exports/")
    export_parser.add_argument("--database", choices=[*DATABASES.keys(), "all"], default="agency")
    export_parser.add_argument(
        "--timestamped",
        action="store_true",
        help="Also keep an immutable timestamped copy next to the stable snapshot file.",
    )
    export_parser.add_argument(
        "--git-add",
        action="store_true",
        help="Stage the exported snapshot and manifest with git after a successful export.",
    )

    import_parser = subparsers.add_parser("import", help="Import one or more databases from database_exports/")
    import_parser.add_argument("--database", choices=[*DATABASES.keys(), "all"], default="agency")
    import_parser.add_argument(
        "--file",
        type=Path,
        help="Import a specific dump file. Only valid when --database is not all.",
    )
    import_parser.add_argument(
        "--yes",
        action="store_true",
        help="Confirm that existing objects in the target database can be overwritten.",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "export":
        for target in selected_targets(args.database):
            export_database(target, timestamped=args.timestamped, git_add=args.git_add)
        return

    if args.command == "import":
        if args.database == "all" and args.file is not None:
            parser.error("--file can only be used with a single --database value")
        for target in selected_targets(args.database):
            import_database(target, dump_file=args.file, yes=args.yes)
        return


if __name__ == "__main__":
    main()
