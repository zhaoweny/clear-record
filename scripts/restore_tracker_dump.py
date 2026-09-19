#!/usr/bin/env python3
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Take the tracker's dump where it runs, restore it here, and audit the copy.

The other half of ``scripts/audit_tracker_backup.py``. That one proves a dump
holds the tracker; this one proves the dump can be *put back* — which is the
claim a backup is worth nothing without.

The route is the owner's: **take the dump where the instance runs; reproduce it
here.** The instance runs in a container on a Docker daemon that is not this
machine's (an operator-named endpoint: ``--context``, ``--docker-host``, or
whatever ``docker`` already selects). So this script

1. reads the source's version — the image the container runs, or the instance's
   own ``/api/v1/version`` — and the local ``gitea`` binary's, and **refuses to
   restore when they differ**: Gitea migrates its schema forward on start, so a
   restore across versions returns the rows into a different schema than they
   were dumped from. That is a different experiment, not this drill;
2. takes a fresh dump with ``gitea dump`` inside that container, copies the zip
   out, and removes the in-container temp file (the one file this script puts on
   the daemon's host, and it takes it away again);
3. restores the zip **here**, with the local ``gitea`` binary, into a scratch
   root under ``.local/`` — data at ``<scratch>/data`` and the repository root at
   ``<scratch>/repos``, which is the layout ``gitea dump`` writes — behind a
   **generated** ``app.ini`` that binds ``127.0.0.1`` only, serves on a free
   port, and runs offline: no mail, no federation, no actions, no cron, and no
   external service the live instance's own configuration might name;
4. audits that local instance with ``scripts/audit_tracker_backup.py`` — the same
   comparison, in one file — where the verdict must read ``MATCH``, and, when the
   source is named, audits the dump against the source as well;
5. stops the local instance and reports the verdict.

The instance's own ``app.ini`` is in the dump and is deliberately **not** used,
because it points at the *container's* paths (``/data/gitea``,
``/data/git/repositories``, and a dozen more) which do not exist here: started
against it, the local instance would come up on no data, and the audit would
compare the live counts against nothing — a green light for a restore that never
happened. The generated file names every path the live one names, each under the
scratch root, and two checks refuse rather than warn: the extracted database must
be the dump's member byte for byte, and the paths the *instance itself* reports
in its log (``WorkPath``/``CustomPath``/``ConfigFile``, every ``Creating new
Local Storage at …``, and the address it ``Listen``s on) must all be inside the
scratch root, on ``127.0.0.1:<port>``.

There is no Docker-*run* path, deliberately. A ``docker run -p 127.0.0.1:PORT``
against a **remote** daemon publishes on *that host's* loopback, so the audit
here could not reach it, would compare its counts against nothing, and would
leave an orphaned container and directory on a machine nobody was looking at.
The daemon is used for exactly what it is needed for — reading the instance's
version and taking the dump — and the instance restored for the audit is a local
process on this machine's loopback.

What the restored instance must not do is reach back into the live one, and
nothing here writes to it: the reads are ``docker exec gitea dump`` and ``docker
cp``, and the only write anywhere outside this machine is the dump's own temp
file inside the container, which the script removes again. Every other write is
under ``.local/``.

Two limits worth stating plainly, because a backup's worth is exactly its
limits. Gitea's own documentation is explicit that *"to ensure the consistency
of the Gitea instance, it must be shutdown during backup"* — a dump taken from a
serving instance is a point-in-time read of a database and of repositories that
were still moving, so it is **not a consistency-guaranteed backup**, and a
migration in flight can leave a repository and its rows disagreeing. What this
drill can do about that is notice: the audit compares counts and a deterministic
sample, so a torn read shows up as DRIFT (or FAIL) rather than passing silently.
If the tracker can ever be paused, take the dump with it stopped and the
question goes away. The other limit is LFS: Gitea's default puts the object
store at ``data/lfs``, inside the AppDataPath the dump already carries, and the
tracker's issues and wiki do not use it, so there is nothing this drill needs
to copy separately.

Requirements, and the honest limits: ``docker`` on ``PATH`` only when the dump is
*this* script's job (``--take-dump``, or ``--container`` to read its image) — a
reader with a dump file needs no Docker at all, just the matching local binary
*and the release that dump came from* (``--source-version``, or a ``--source-url``
that answers); ``git``, for the wiki page count the audit makes (the wiki is a
git repository inside the dump); and a token the instance accepts, for the
comparisons — ``$GITEA_TOKEN`` or the ``tea`` login store, as
``scripts/migrate_tracker.py`` resolves it. The restored copy carries the dump's
own database, so the operator's existing token authenticates against it exactly
as it does against the source.

Usage (normally via ``just tracker-restore …``)::

    # the drill, end to end, off the instance's container
    uv run --no-project scripts/restore_tracker_dump.py --take-dump \
        --context NAME --container NAME --source-url URL

    # a reader with a dump in hand and no Docker at all
    uv run --no-project scripts/restore_tracker_dump.py --dump FILE \
        --source-version X.Y.Z

    # plan only, touching nothing (--container is required with --take-dump)
    uv run --no-project scripts/restore_tracker_dump.py --take-dump \
        --container NAME --dry-run

Exit status: 0 when the restored copy matches the dump — the source comparison,
when the source was named, may read ``MATCH`` or ``DRIFT``, the latter being the
live tracker worked on after the dump was taken; 1 when the drill did not pass —
a ``FAIL`` verdict, which is something the dump holds and the compared instance
does not (a ticket, a comment, a page, a label or an account), an audit that
wrote no record, or a named source that could not be read; 2 when the run was
refused before or during the restore — a missing prerequisite or bad input (no
Docker, a version mismatch, an unreadable dump, a scratch directory this drill
did not write), an instance that would not come up, or a path or listen address
outside the scratch root. A restore that started always leaves ``drill.json``
saying what happened, including what failed.
``docs/tracker-backup.md`` carries the procedure, the evidence and who runs it.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn

DEFAULT_REPO = "zhaow/clear-record"

# The base URL of the instance to compare against, when the operator names it.
# Same variable, and same reason, as ``scripts/audit_tracker_backup.py`` and
# ``scripts/migrate_tracker.py``: the instance is private, the committed-file
# guard rejects its hostname, and nothing here ever defaults to it.
URL_ENV = "CLEAR_RECORD_GITEA_URL"

# Where ``gitea dump`` writes, as the container's own environment names it: the
# AppDataPath holds the database and the files it points at, the repository root
# holds the mirrored code and the wiki.
APP_DATA_PATH = "data"
REPO_ROOT_PATH = "repos"

# A dump's top-level members, which map one-to-one onto those two paths, plus the
# SQL-text copy of the database and the configuration copy at the archive root.
DUMP_PAYLOAD = ("data", "repos", "app.ini", "gitea-db.sql")

# The generated configuration's home, under the scratch root: ``GITEA_CUSTOM``
# points here too, so the instance's own ``data/conf/app.ini`` — which is in the
# dump — is never read.
CUSTOM_DIR = "custom"
CONFIG_NAME = "app.ini"

# The secrets a fresh instance needs, which the local binary generates. They are
# this drill's own, not the instance's: the dump's configuration is not restored,
# so the copy carries the dump's data and none of the instance's keys.
DEFAULT_PORT = 0  # 0 = pick a free one, so two drills cannot collide
DEFAULT_SCRATCH = ".local/restore-drill"
DEFAULT_DUMP_DIR = ".local/backups"
DEFAULT_TIMEOUT = 180
STOP_TIMEOUT = 30
HEALTH_PATH = "/api/healthz"
VERSION_PATH = "/api/v1/version"
CONTAINER_TMPDIR = "/tmp"
CONTAINER_USER = "git"

# Written into the scratch root, and required before this script removes it: a
# --scratch path that is not empty and has no marker is somebody else's
# directory, and is refused rather than deleted.
DRILL_MARKER = ".restore-drill"

# Every absolute path the live instance's own configuration names — the
# container's ``/data/gitea`` and ``/data/git/repositories`` among them — and
# therefore every one the generated configuration has to re-point at the scratch
# root, or the copy would come up as an *empty* instance on a path that does not
# exist here, and the audit would compare the live counts against nothing.
#
# The sections and keys are the live config's, read from a dump of it.
CONFIG_PATH_KEYS = (
    ("", "WORK_PATH"),
    ("repository", "ROOT"),
    ("repository.local", "LOCAL_COPY_PATH"),
    ("repository.upload", "TEMP_PATH"),
    ("server", "APP_DATA_PATH"),
    ("database", "PATH"),
    ("session", "PROVIDER_CONFIG"),
    ("picture", "AVATAR_UPLOAD_PATH"),
    ("picture", "REPOSITORY_AVATAR_UPLOAD_PATH"),
    ("attachment", "PATH"),
    ("indexer", "ISSUE_INDEXER_PATH"),
    ("lfs", "PATH"),
    ("log", "ROOT_PATH"),
)

# What the running instance says about itself, in its own log: the paths it will
# use, the storage it creates, and the address it listens on. The check is made
# here rather than only against the generated file because the log is what the
# instance *did*, and a config the operator edited cannot fake it.
LOG_PATH_RE = re.compile(
    r"^.*\* (WorkPath|CustomPath|ConfigFile): (\S+)$", re.MULTILINE
)
LOG_STORAGE_RE = re.compile(r"Creating new Local Storage at (\S+)")
LOG_LISTEN_RE = re.compile(r"Listen: http://(\S+)")

# The versions this drill will restore across: exact and equal, or nothing. A
# tag like ``latest`` or ``1.27`` says nothing about which release runs, so the
# image tag only counts when it names one; otherwise the instance's version
# endpoint, or ``--source-version``, has to.
VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")
CONCRETE_TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+)(?:[-+][0-9A-Za-z.-]+)?$")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def die(message: str) -> NoReturn:
    """Report a missing prerequisite or a bad input: stderr, exit status 2."""
    print(message, file=sys.stderr)
    sys.exit(2)


def human_bytes(size: int) -> str:
    return f"{size / (1 << 20):.1f} MiB" if size >= (1 << 20) else f"{size} bytes"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(
    argv: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
    quiet: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run one command, printing it first — the sequence is the evidence."""
    if not quiet:
        print(f"  $ {' '.join(argv)}", flush=True)
    result = subprocess.run(argv, capture_output=True, text=True, env=env)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        die(
            f"tracker-restore: the command failed: {' '.join(argv)}\n"
            f"  {detail[-1] if detail else 'no output'}"
        )
    return result


# --------------------------------------------------------------------------- #
# The operator's Docker endpoint
# --------------------------------------------------------------------------- #


def docker_argv(args: argparse.Namespace) -> list[str]:
    """``docker``, aimed at the endpoint the operator named.

    ``--context`` is the CLI's own way of naming a remote daemon, and names no
    host in a committed file; ``--docker-host`` sets ``$DOCKER_HOST`` for this
    process's docker calls. Neither flag means: whatever ``docker`` already
    selects (its selected context, or ``$DOCKER_CONTEXT``/``$DOCKER_HOST``).
    """
    return ["docker", "--context", args.context] if args.context else ["docker"]


def docker_env(args: argparse.Namespace) -> dict[str, str] | None:
    if not args.docker_host:
        return None
    return {**os.environ, "DOCKER_HOST": args.docker_host}


def require_docker(args: argparse.Namespace) -> None:
    if shutil.which("docker") is None:
        die(
            "tracker-restore: `docker` is not on PATH, and this run needs it "
            "(the dump is taken through it).\n"
            "  A reader with a dump file and no Docker does not: pass --dump FILE\n"
            "  --source-version X.Y.Z, naming the release the dump came from.\n"
            "  `--dry-run` prints the whole sequence without touching Docker."
        )
    probe = run(
        docker_argv(args) + ["info", "--format", "{{.ServerVersion}}"],
        check=False,
        env=docker_env(args),
    )
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout).strip().splitlines()
        die(
            "tracker-restore: the Docker daemon is not reachable.\n"
            f"  docker info said: {detail[-1] if detail else 'no output'}\n"
            "  Name it with --context NAME or --docker-host URL (or select it in\n"
            "  the docker CLI); `--dry-run` prints the sequence without needing it."
        )
    print(f"docker    daemon {probe.stdout.strip()}")


def image_of(args: argparse.Namespace) -> str:
    """The image the container runs — the source's own release, as it runs it."""
    result = run(
        docker_argv(args)
        + ["inspect", "--format", "{{.Config.Image}}", args.container],
        env=docker_env(args),
    )
    return result.stdout.strip()


# --------------------------------------------------------------------------- #
# Versions: the source's, and the local binary's
# --------------------------------------------------------------------------- #


def version_from_api(url: str) -> str | None:
    """The instance's own answer to ``/api/v1/version`` — public, no token."""
    endpoint = f"{url.rstrip('/')}{VERSION_PATH}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(endpoint, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    version = str(payload.get("version", "")).strip()
    return version or None


def read_source_version(
    args: argparse.Namespace,
) -> tuple[str | None, str, str | None]:
    """The source's version, how it was read, and its image when it has one.

    The description never carries the source's address — only ``--source-url``
    ever holds that, and it is not printed here.
    """
    if args.source_version:
        return args.source_version.strip().lstrip("v"), "--source-version", None
    image = None
    if args.container:
        image = image_of(args)
        match = CONCRETE_TAG_RE.match(image.rpartition(":")[2])
        if match:
            return match.group(1), f"the image the container runs ({image})", image
        print(
            f"image     {image} — its tag names no release, so it cannot say "
            "which version runs"
        )
    url = resolve_source_url(args)
    if url:
        version = version_from_api(url)
        if version:
            return version, "the instance's version endpoint", image
    return None, "unknown", image


def local_version(binary: str) -> str:
    """The version of the ``gitea`` binary the restore would run."""
    resolved = shutil.which(binary)
    if resolved is None and not Path(binary).is_file():
        die(
            f"tracker-restore: no `{binary}` binary.\n"
            "  The restore runs the instance's own Gitea locally; install the\n"
            "  release the source runs and put it on PATH, or pass --gitea PATH."
        )
    result = subprocess.run([binary, "--version"], capture_output=True, text=True)
    text = f"{result.stdout}\n{result.stderr}"
    if result.returncode != 0:
        die(
            f"tracker-restore: `{binary} --version` failed (status "
            f"{result.returncode}):\n  {text.strip().splitlines()[-1] if text.strip() else 'no output'}"
        )
    match = VERSION_RE.search(text)
    if match is None:
        die(
            f"tracker-restore: `{binary} --version` named no version:\n  {text.strip()}"
        )
    return match.group(1)


def check_versions(
    source: str | None, source_from: str, local: str, binary: str
) -> None:
    """Refuse the restore unless both sides run the same release.

    A forward migration is a different experiment: the rows come back, but into
    the newer schema, so a drill run across versions would be testing the
    migration rather than the backup.
    """
    if source is None:
        die(
            "tracker-restore: the source's version is unknown, and the drill will "
            "not restore across versions it cannot compare.\n"
            "  Name the instance (--source-url URL), or the container it runs in\n"
            "  (--container NAME with --context/--docker-host), or say it outright\n"
            "  (--source-version 1.27.3)."
        )
    if source != local:
        die(
            f"tracker-restore: version mismatch — the source runs Gitea "
            f"{source} ({source_from}),\n"
            f"  but the local `{binary}` is {local}.\n"
            "  Gitea migrates its schema forward on start, so restoring across\n"
            "  versions is a different experiment: install Gitea "
            f"{source} (the release\n"
            "  tarball, or a package manager once it carries that release), point\n"
            "  --gitea PATH at it and run the drill again. Nothing was restored."
        )
    print(f"version   source {source} ({source_from}); local {binary} {local}")


# --------------------------------------------------------------------------- #
# Taking the dump, where the instance runs
# --------------------------------------------------------------------------- #


def dump_filename(stamp: str) -> str:
    return f"gitea-dump-{stamp}.zip"


def take_dump(args: argparse.Namespace, dest_dir: Path, stamp: str) -> Path:
    """``gitea dump`` in the container, the zip copied out, the temp file removed.

    The only things this writes on the daemon's host are the dump's own temp
    file and the removal of it: no container is created, and no directory is
    left behind.
    """
    name = dump_filename(stamp)
    inner = f"{CONTAINER_TMPDIR}/{name}"
    docker = docker_argv(args)
    env = docker_env(args)
    exec_argv = docker + ["exec", "-u", CONTAINER_USER, args.container]
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    print(f"dump      {args.container}:{inner} -> {dest}")
    try:
        result = run(exec_argv + ["gitea", "dump", "-f", inner], env=env)
        said = [
            line.strip()
            for line in (result.stdout + result.stderr).splitlines()
            if line.strip()
        ]
        print(f"          {said[-1] if said else 'gitea dump said nothing'}")
        run(exec_argv + ["test", "-s", inner], env=env)
        run(docker + ["cp", f"{args.container}:{inner}", str(dest)], env=env)
    finally:
        removed = run(exec_argv + ["rm", "-f", inner], check=False, env=env)
        state = "removed" if removed.returncode == 0 else "NOT removed"
        print(f"cleanup   in-container temp file {inner} {state}")
    if not zipfile.is_zipfile(dest):
        die(f"tracker-restore: {dest} is not a zip archive — the copy failed.")
    return dest


# --------------------------------------------------------------------------- #
# Restoring it here
# --------------------------------------------------------------------------- #


def extract(dump: Path, scratch: Path) -> None:
    """Put the dump's members back at the paths the generated config names."""
    if not dump.is_file():
        die(
            f"tracker-restore: no dump at {dump}.\n"
            "  Pass --dump FILE — the archive `gitea dump` wrote — or --take-dump\n"
            "  to take a fresh one through the Docker endpoint."
        )
    if not zipfile.is_zipfile(dump):
        die(f"tracker-restore: {dump} is not a zip archive — is it a dump?")
    if (
        scratch.exists()
        and any(scratch.iterdir())
        and not (scratch / DRILL_MARKER).exists()
    ):
        die(
            f"tracker-restore: {scratch} is not empty, and this drill did not write "
            "it.\n  Pass --scratch DIR naming an empty (or new) directory, so a run "
            "cannot delete something else."
        )
    if scratch.exists():
        shutil.rmtree(scratch)
    scratch.mkdir(parents=True)
    with zipfile.ZipFile(dump) as archive:
        archive.extractall(scratch)
    # Only the AppDataPath is required here: the database inside it is what the
    # restore is (and the audit refuses a dump without one), while the instance's
    # configuration copy — the file this drill deliberately does not use — is not.
    if not (scratch / APP_DATA_PATH).is_dir():
        die(
            f"tracker-restore: the dump has no {APP_DATA_PATH}/ — is it a "
            "`gitea dump` archive?"
        )
    (scratch / DRILL_MARKER).write_text(
        f"extracted from {dump.name}\n", encoding="utf-8"
    )
    print(f"restore   {dump.name} extracted to {scratch}")


def fresh_secret(binary: str, kind: str) -> str:
    result = subprocess.run(
        [binary, "generate", "secret", kind], capture_output=True, text=True
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        die(
            f"tracker-restore: `{binary} generate secret {kind}` produced nothing "
            "(status "
            f"{result.returncode}); the generated configuration needs it."
        )
    return value


def write_config(scratch: Path, port: int, binary: str) -> Path:
    """Write the drill's own app.ini: the scratch root, loopback, and nothing else.

    Generated rather than restored, and complete on purpose. The dump's own
    configuration names the *container's* absolute paths — ``/data/gitea`` for
    the AppDataPath, ``/data/git/repositories`` for the repository root, and a
    dozen more — which do not exist here: started against it, the local instance
    would come up as an empty instance, and the audit would then compare the live
    counts against nothing. It also names the live host and whatever services the
    instance uses, and carries its secrets.

    So this file names every path the live one names, each one under the scratch
    root, and enables nothing that could reach outward. The database is still the
    dump's, so the copy is the instance in miniature exactly where it matters.
    """
    host = f"127.0.0.1:{port}"
    data = f"{scratch}/{APP_DATA_PATH}"
    config = scratch / CUSTOM_DIR / "conf" / CONFIG_NAME
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        f"""; Generated by scripts/restore_tracker_dump.py — not the instance's
; app.ini. That one is in the dump, is deliberately unused, and names the
; container's own paths (/data/gitea, /data/git/repositories, …); every path it
; names is re-pointed here at the scratch root, and every feature that could
; reach outward is off.
APP_NAME = clear-record restore drill
WORK_PATH = {scratch}

[server]
PROTOCOL = http
HTTP_ADDR = 127.0.0.1
HTTP_PORT = {port}
ROOT_URL = http://{host}/
DOMAIN = 127.0.0.1
SSH_DOMAIN = 127.0.0.1
DISABLE_SSH = true
START_SSH_SERVER = false
OFFLINE_MODE = true
LFS_START_SERVER = false
APP_DATA_PATH = {data}

[database]
DB_TYPE = sqlite3
PATH = {data}/gitea.db

[repository]
ROOT = {scratch}/{REPO_ROOT_PATH}

[repository.local]
LOCAL_COPY_PATH = {data}/tmp/local-repo

[repository.upload]
TEMP_PATH = {data}/uploads

[picture]
AVATAR_UPLOAD_PATH = {data}/avatars
REPOSITORY_AVATAR_UPLOAD_PATH = {data}/repo-avatars

[attachment]
PATH = {data}/attachments

[indexer]
ISSUE_INDEXER_PATH = {data}/indexers/issues.bleve

[lfs]
PATH = {data}/lfs

[session]
PROVIDER = file
PROVIDER_CONFIG = {data}/sessions

[log]
MODE = console
LEVEL = info
ROOT_PATH = {data}/log

[security]
INSTALL_LOCK = true
SECRET_KEY = {fresh_secret(binary, "SECRET_KEY")}
INTERNAL_TOKEN = {fresh_secret(binary, "INTERNAL_TOKEN")}

[oauth2]
JWT_SECRET = {fresh_secret(binary, "JWT_SECRET")}

[service]
DISABLE_REGISTRATION = true
ENABLE_NOTIFY_MAIL = false

[mailer]
ENABLED = false

[federation]
ENABLED = false

[actions]
ENABLED = false

[cron]
ENABLED = false
""",
        encoding="utf-8",
    )
    print(f"config    {config} (generated: {host} only, offline, no mail)")
    return config


def config_paths(config: Path) -> dict[str, str]:
    """The path-valued settings the generated config names, and their values."""
    section = ""
    found: dict[str, str] = {}
    for line in config.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        if stripped.startswith(";") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key, value = key.strip(), value.strip()
        if (section, key) in CONFIG_PATH_KEYS:
            found[f"{section + '.' if section else ''}{key}"] = value
    return found


def verify_paths(
    scratch: Path, config: Path, log_path: Path, port: int
) -> tuple[dict[str, str], dict[str, str]]:
    """Refuse unless the running instance names only paths inside the scratch.

    The claim a drill has to earn is that the instance the audit reads *is* the
    restored dump. A copy pointed at the dump's container paths would answer that
    question with an empty instance, so both halves are checked: the paths the
    generated config names, and the paths the instance's own log says it uses.
    Returns both, for the report and the record.
    """
    inside = scratch.resolve()
    outside: list[tuple[str, str]] = []

    def check(key: str, value: str) -> None:
        path = Path(value)
        resolved = path.resolve()
        if not path.is_absolute() or (
            resolved != inside and inside not in resolved.parents
        ):
            outside.append((key, value))

    named = config_paths(config)
    for key, value in named.items():
        check(key, value)
    log = log_path.read_text("utf-8", "replace") if log_path.is_file() else ""
    used: dict[str, str] = {}
    for match in LOG_PATH_RE.finditer(log):
        used[match.group(1)] = match.group(2)
    for index, match in enumerate(LOG_STORAGE_RE.finditer(log)):
        used[f"Local Storage ({index + 1})"] = match.group(1)
    for key, value in used.items():
        check(key, value)

    problems: list[str] = [f"  {key} = {value}" for key, value in outside]
    missing = [
        f"{section + '.' if section else ''}{key}"
        for section, key in CONFIG_PATH_KEYS
        if f"{section + '.' if section else ''}{key}" not in named
    ]
    if missing:
        problems.append(f"  the config names no path for: {', '.join(missing)}")
    if problems:
        die(
            "tracker-restore: the restored instance is not confined to the scratch "
            "root — refusing to audit it.\n"
            "  An instance on paths outside the scratch is an instance on someone "
            "else's data,\n  or on no data at all, and its counts would mean "
            "nothing:\n" + "\n".join(problems) + f"\n  scratch: {inside}"
        )
    listen = LOG_LISTEN_RE.search(log)
    if listen is None:
        die(
            "tracker-restore: the restored instance never said what it listens "
            f"on.\n  Read {log_path}."
        )
    address = listen.group(1)
    if address != f"127.0.0.1:{port}":
        die(
            f"tracker-restore: the restored instance listened on {address}, not "
            f"127.0.0.1:{port} — refusing.\n"
            "  The copy carries the dump's data and is not to be reachable off "
            "this machine."
        )
    return named, used


def verify_database(dump: Path, scratch: Path, facts: Any) -> str:
    """Refuse unless the extracted database is the dump's own, byte for byte.

    The audit reads the dump's database from the zip; the instance reads the file
    the generated config names. This is what ties the two together: the same
    bytes, so the counts the audit compares are counts of the same database.
    """
    extracted = scratch / facts.db_member
    if not extracted.is_file():
        die(
            f"tracker-restore: the dump's database {facts.db_member} is not at "
            f"{extracted}.\n  The extraction and the generated config disagree "
            "about where it lives."
        )
    with zipfile.ZipFile(dump) as archive:
        dumped = hashlib.sha256(archive.read(facts.db_member)).hexdigest()
    here = file_sha256(extracted)
    if here != dumped:
        die(
            f"tracker-restore: {extracted} is not the dump's "
            f"{facts.db_member}.\n  dump {dumped}\n  here {here}\n"
            "  The instance would be reading a database the dump does not hold."
        )
    if not facts.issues:
        die(
            "tracker-restore: the dump holds no tickets, so a restore of it could "
            "not show anything.\n  Is this the tracker's dump? Nothing was started."
        )
    print(
        f"database  {extracted} — the dump's {facts.db_member} byte for byte "
        f"(sha256 {here[:16]}…)"
    )
    return here


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_instance(
    binary: str, config: Path, scratch: Path, log_path: Path
) -> subprocess.Popen[bytes]:
    """Run the local binary against the restored data, logging into the scratch."""
    env = {
        **os.environ,
        "GITEA_WORK_DIR": str(scratch),
        "GITEA_CUSTOM": str(scratch / CUSTOM_DIR),
    }
    argv = [binary, "web", "--config", str(config)]
    print(f"start     {' '.join(argv)}")
    print(f"          work dir {scratch}, log {log_path}")
    log = log_path.open("wb")
    try:
        return subprocess.Popen(argv, cwd=str(scratch), env=env, stdout=log, stderr=log)
    finally:
        # The child holds its own descriptor; the parent's copy is not needed.
        log.close()


def log_tail(path: Path, lines: int = 12) -> str:
    if not path.is_file():
        return f"(no log at {path})"
    text = [
        line for line in path.read_text("utf-8", "replace").splitlines() if line.strip()
    ]
    return "\n  ".join(text[-lines:])


def health_summary(body: str) -> str:
    """The health endpoint's answer, as one line fit to print as evidence."""
    try:
        payload = json.loads(body)
    except ValueError:
        return body[:120]
    checks = ", ".join(str(name) for name in (payload.get("checks") or {}))
    return f"status={payload.get('status', '?')} ({checks or 'no checks'})"


def wait_for_instance(
    port: int, proc: subprocess.Popen[bytes], timeout: float, log_path: Path
) -> str:
    """Poll the health endpoint until the instance answers or time runs out."""
    url = f"http://127.0.0.1:{port}{HEALTH_PATH}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    last = "no answer yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            die(
                f"tracker-restore: the restored instance exited (status "
                f"{proc.returncode}) before it was ready.\n  {log_tail(log_path)}"
            )
        try:
            with opener.open(url, timeout=10) as response:
                body = response.read().decode("utf-8", "replace")
            if response.status == 200:
                return health_summary(body)
            last = f"HTTP {response.status}"
        except (urllib.error.URLError, OSError) as exc:
            last = str(exc)
        time.sleep(1)
    die(
        f"tracker-restore: the restored instance did not become ready within "
        f"{timeout:.0f}s ({url}): {last}\n  {log_tail(log_path)}"
    )


def stop_instance(proc: subprocess.Popen[bytes], log_path: Path) -> bool:
    """Stop the local instance, and say so; the drill leaves nothing running."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
    status = proc.poll()
    print(f"stop      the restored instance exited (status {status})")
    if status not in (0, -15, 143) and status is not None:
        print(f"          last words:\n  {log_tail(log_path)}")
    return status is not None


def drop_payload(scratch: Path) -> None:
    """Remove the extracted dump, keeping the records, the config and the log."""
    for name in DUMP_PAYLOAD:
        path = scratch / name
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    print(f"cleanup   the extracted dump is removed from {scratch}")


# --------------------------------------------------------------------------- #
# The audit half, reused in process
# --------------------------------------------------------------------------- #


def load_audit() -> Any:
    """The audit script, loaded as a module (it is not part of any package)."""
    path = Path(__file__).with_name("audit_tracker_backup.py")
    spec = importlib.util.spec_from_file_location("audit_tracker_backup", path)
    if spec is None or spec.loader is None:
        die(f"tracker-restore: cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # `dataclasses` looks the class's module up in `sys.modules` while the
    # decorator runs, so the module must be registered before execution.
    sys.modules["audit_tracker_backup"] = module
    spec.loader.exec_module(module)
    return module


def audit_argv(dump: Path, url: str, repo: str, sample: int, record: Path) -> list[str]:
    return [
        "--dump",
        str(dump),
        "--url",
        url,
        "--repo",
        repo,
        "--sample",
        str(sample),
        "--record",
        str(record),
    ]


def read_verdict(record: Path) -> tuple[str, list[str], list[str]]:
    if not record.is_file():
        die(
            f"tracker-restore: the audit wrote no record at {record} — read its "
            "output above for why."
        )
    try:
        payload = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        die(f"tracker-restore: the audit's record at {record} cannot be read: {exc}")
    comparison = payload.get("comparison") or {}
    return (
        str(payload.get("verdict", "unknown")),
        [str(line) for line in comparison.get("lost", [])],
        [str(line) for line in comparison.get("drift", [])],
    )


def run_audit(audit: Any, argv_list: list[str], label: str) -> int:
    """One audit run; a missing prerequisite is reported, not raised."""
    print(f"\naudit     {label}")
    try:
        return int(audit.main(argv_list))
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def resolve_source_url(args: argparse.Namespace) -> str | None:
    """The instance this run compares against: ``--source-url``, or the env var.

    Deliberately not defaulted, for ``scripts/migrate_tracker.py``'s reason: the
    instance is private, and a convenience default here would be the hostname the
    committed-file guard rejects.
    """
    if args.source_url:
        return args.source_url
    return os.environ.get(URL_ENV, "").strip() or None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="restore_tracker_dump.py",
        description=(
            "Take the tracker's dump where the instance runs and restore it here "
            "with the local Gitea, then audit the restored copy against the dump "
            "— the restore half of the tracker backup drill."
        ),
        epilog=(
            "the drill refuses to restore across Gitea versions; the local binary "
            "must be the source's release. The restored instance binds 127.0.0.1 "
            "only, and carries the dump's own database, so the operator's token "
            "authenticates against it. docs/tracker-backup.md is the procedure."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--dump",
        metavar="FILE",
        help=(
            "the dump to restore — a reader with a file needs no Docker, but does "
            "need --source-version X.Y.Z (or a --source-url) to say which release "
            "it came from"
        ),
    )
    source.add_argument(
        "--take-dump",
        action="store_true",
        help="take a fresh dump from the instance's container first",
    )
    parser.add_argument(
        "--container",
        metavar="NAME",
        help="the container running the instance (required with --take-dump)",
    )
    parser.add_argument(
        "--context",
        metavar="NAME",
        help="docker context naming the daemon the container runs on",
    )
    parser.add_argument(
        "--docker-host",
        metavar="URL",
        help="docker endpoint URL (sets $DOCKER_HOST for this run)",
    )
    parser.add_argument(
        "--dump-out",
        metavar="DIR",
        help=f"where a taken dump lands (default: <repo>/{DEFAULT_DUMP_DIR})",
    )
    parser.add_argument(
        "--scratch",
        metavar="DIR",
        help=f"where the dump is restored (default: <repo>/{DEFAULT_SCRATCH})",
    )
    parser.add_argument(
        "--gitea",
        default="gitea",
        metavar="PATH",
        help="the local Gitea binary that restores it (default: `gitea` on PATH)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        metavar="N",
        help="loopback port for the restored instance (default: a free one)",
    )
    parser.add_argument(
        "--source-url",
        metavar="URL",
        help=(
            "base URL of the instance this dump came from; it is compared against "
            f"the restored copy, and answers for its version (or set ${URL_ENV})"
        ),
    )
    parser.add_argument(
        "--source-version",
        metavar="X.Y.Z",
        help=(
            "the release the dump came from, when neither the container's image "
            "nor the source's version endpoint can say it — the only way a reader "
            "with no Docker (and no --source-url) can satisfy the version check"
        ),
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        metavar="OWNER/NAME",
        help=f"repository holding the tickets (default: {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=5,
        metavar="N",
        help="issues the audit compares field by field (default: 5, so 10 issues)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        metavar="SECONDS",
        help=f"how long to wait for the restored instance (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the restored data after a pass, to look at (a failure keeps it "
        "either way)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned sequence and exit without touching Docker or the dump",
    )
    raw = list(sys.argv[1:] if argv is None else argv)
    # `just tracker-restore -- --dump FILE` is the usual way to hand flags through
    # a recipe; argparse would read everything after `--` as positional, and this
    # script has no positionals, so drop the separator.
    return parser.parse_args([arg for arg in raw if arg != "--"])


def print_plan(args: argparse.Namespace, scratch: Path, dump: Path | None) -> None:
    """The whole sequence, for a reader who cannot run it (or wants to see it)."""
    step = 0

    def line(text: str) -> None:
        nonlocal step
        step += 1
        print(f"  {step}. {text}")

    print("plan (nothing has been run, and nothing has been touched):")
    line("versions   the source's (container image or /api/v1/version) against")
    print(f"                the local `{args.gitea} --version`; equal, or refused")
    if args.take_dump:
        line(
            f"dump       docker --context NAME exec -u {CONTAINER_USER} "
            f"{args.container} gitea dump \\"
        )
        print(f"                    -f {CONTAINER_TMPDIR}/{dump_filename('<stamp>')}")
        print(
            f"                docker --context NAME cp {args.container}:<that file> "
            f"<dump-out>/"
        )
        print(
            f"                docker --context NAME exec -u {CONTAINER_USER} "
            f"{args.container} rm -f <that file>"
        )
    else:
        line(f"dump       {dump} (as passed)")
    line(f"restore    the dump's members, extracted to {scratch}")
    line(f"config     {scratch}/{CUSTOM_DIR}/conf/{CONFIG_NAME} — generated:")
    print(
        "                127.0.0.1:<free port> only, offline, no mail, no federation;"
    )
    print("                every path the dump's own app.ini names re-pointed here")
    line(f"run        {args.gitea} web (work dir {scratch})")
    line("check      the extracted database is the dump's, byte for byte, and")
    print("                the instance's own log names only paths under the scratch")
    print("                root and listens on 127.0.0.1:<free port> — or it refuses")
    line("audit      the restored copy at http://127.0.0.1:<free port> — MATCH")
    print("                is the pass a restore must read; the record is written")
    print(f"                under {scratch}")
    if resolve_source_url(args):
        line("audit      the dump against the source as well (MATCH or DRIFT)")
    line("stop       the restored instance, and the extracted dump removed")
    print()
    print("a reader with no Docker at all (the release has to be named):")
    print(
        f"  uv run --no-project scripts/restore_tracker_dump.py --dump "
        f"{dump or 'FILE'} --source-version X.Y.Z"
    )
    print("a reader with the daemon the instance runs on:")
    print(
        "  uv run --no-project scripts/restore_tracker_dump.py --take-dump \\\n"
        "      --context NAME --container NAME --source-url URL"
    )


def drill_payload(
    args: argparse.Namespace,
    *,
    stamp: str,
    dump: Path,
    taken: bool,
    image: str | None,
    source_version: str | None,
    source_from: str,
    local: str,
    scratch: Path,
    port: int,
    config: Path,
    log_path: Path,
    paths: dict[str, dict[str, str]],
    records: dict[str, Path | None],
    verdicts: dict[str, str],
    cleanup: dict[str, Any],
    failure: str | None,
) -> dict[str, Any]:
    """The drill's own record: one dump, the versions on both sides, the binds.

    Written whenever a restore started, pass or fail — an audit that could not
    run is exactly when the record matters most, so ``failure`` says what did not
    happen instead of the file being absent.
    """
    return {
        "tool": "restore_tracker_dump",
        "run_at": stamp,
        "failure": failure,
        "dump": {
            "path": str(dump),
            "name": dump.name,
            "bytes": dump.stat().st_size,
            "sha256": file_sha256(dump),
            "taken_by_this_run": taken,
        },
        "provenance": {"container": args.container or None, "image": image},
        "version": {
            "source": source_version,
            "source_from": source_from,
            "local": local,
            "local_binary": args.gitea,
        },
        "instance": {
            "bind": f"127.0.0.1:{port}",
            "url": f"http://127.0.0.1:{port}",
            "work_dir": str(scratch),
            "config": str(config),
            "log": str(log_path),
            "paths_verified": paths,
        },
        "records": {
            label: str(path) if path else None for label, path in records.items()
        },
        "verdict": verdicts,
        "cleanup": cleanup,
    }


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    scratch = (
        Path(args.scratch).expanduser().resolve()
        if args.scratch
        else repo_root() / DEFAULT_SCRATCH
    )
    dump_dir = (
        Path(args.dump_out).expanduser().resolve()
        if args.dump_out
        else repo_root() / DEFAULT_DUMP_DIR
    )
    dump_path = Path(args.dump).expanduser().resolve() if args.dump else None
    restored_record = scratch / "audit.json"
    source_record = scratch / "audit-source.json"
    drill_record = scratch / "drill.json"
    log_path = scratch / "gitea.log"
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    stamp_slug = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    source_url = resolve_source_url(args)

    if args.context and args.docker_host:
        die(
            "tracker-restore: pass --context NAME or --docker-host URL, not both —\n"
            "  they name the same thing two ways."
        )
    if not args.take_dump and not args.dump:
        die(
            "tracker-restore: nothing to restore.\n"
            "  Pass --dump FILE (a dump in hand needs no Docker), or --take-dump\n"
            "  with --container NAME to take a fresh one through the daemon."
        )
    if args.take_dump and not args.container:
        die(
            "tracker-restore: --take-dump needs --container NAME — the container\n"
            "  the instance runs in, on the daemon named by --context/--docker-host."
        )

    print(f"drill     restore the tracker's dump on this machine ({stamp})")
    if args.dry_run:
        print_plan(args, scratch, dump_path)
        return 0

    if args.take_dump or args.container:
        require_docker(args)

    source_version, source_from, image = read_source_version(args)
    local = local_version(args.gitea)
    check_versions(source_version, source_from, local, args.gitea)

    dump = dump_path
    if args.take_dump:
        dump = take_dump(args, dump_dir, stamp_slug)
    assert dump is not None
    if not dump.is_file():
        die(
            f"tracker-restore: no dump at {dump}.\n"
            "  Pass --dump FILE — the archive `gitea dump` wrote."
        )
    print(
        f"dump      {dump} — {human_bytes(dump.stat().st_size)}, "
        f"sha256 {file_sha256(dump)[:16]}…"
    )

    extract(dump, scratch)
    port = args.port or free_port()
    config = write_config(scratch, port, args.gitea)
    if args.port == DEFAULT_PORT:
        print(f"port      {port} (free, chosen for this run)")

    audit = load_audit()
    # The dump read from the zip, and the extracted file the instance will open:
    # the two have to be the same database, or the counts mean nothing.
    facts = audit.read_dump(dump, args.repo)
    verify_database(dump, scratch, facts)
    print(
        f"contents  {len(facts.issues)} tickets, {facts.comments} comments, "
        f"{len(facts.wiki.pages)} wiki pages, {facts.users} users, "
        f"{len(facts.label_names)} labels"
    )

    verdicts: dict[str, str] = {"restored": "not run", "source": "not run"}
    records: dict[str, Path | None] = {
        "restored": restored_record,
        "source": source_record if source_url else None,
    }
    cleanup: dict[str, Any] = {"instance_stopped": False, "payload_removed": False}
    lost: list[str] = []
    drift: list[str] = []
    paths: dict[str, dict[str, str]] = {"config": {}, "instance_log": {}}
    # A restore that has started owes a record and a stopped instance, whatever
    # happens next: an audit that cannot run (an unreachable source, a refused
    # token) must not take the evidence down with it.
    problem: str | None = None
    refusal = 0
    stopped = False
    proc = start_instance(args.gitea, config, scratch, log_path)
    try:
        try:
            health = wait_for_instance(port, proc, args.timeout, log_path)
            print(f"ready     {health} at http://127.0.0.1:{port}{HEALTH_PATH}")
            named, used = verify_paths(scratch, config, log_path, port)
            paths = {"config": named, "instance_log": used}
            print(
                f"paths     {len(named)} path settings re-pointed at {scratch}, each "
                f"verified inside it, and listening on 127.0.0.1:{port}:"
            )
            for key, value in used.items():
                print(f"          {key}: {value}")
            code = run_audit(
                audit,
                audit_argv(
                    dump,
                    f"http://127.0.0.1:{port}",
                    args.repo,
                    args.sample,
                    restored_record,
                ),
                f"the restored copy, at http://127.0.0.1:{port}",
            )
            if restored_record.is_file():
                verdicts["restored"], lost, drift = read_verdict(restored_record)
            else:
                problem = (
                    f"the audit of the restored copy wrote no record (exit {code}); "
                    "its message is above"
                )
            if source_url is not None and problem is None:
                code = run_audit(
                    audit,
                    audit_argv(dump, source_url, args.repo, args.sample, source_record),
                    "the dump against the source it was taken from",
                )
                if source_record.is_file():
                    verdicts["source"] = read_verdict(source_record)[0]
                else:
                    problem = (
                        f"the source comparison wrote no record (exit {code}); its "
                        "message is above"
                    )
        except SystemExit as exc:
            # A refusal from deep inside the run — the instance would not come up,
            # a path or address was not the scratch root's. It said why already.
            refusal = exc.code if isinstance(exc.code, int) else 2
            problem = (
                f"the drill was refused after the instance started (exit {refusal})"
            )
    finally:
        stopped = stop_instance(proc, log_path)
    cleanup["instance_stopped"] = stopped

    passed = verdicts["restored"] == "match" and problem is None
    if passed and not args.keep:
        drop_payload(scratch)
        cleanup["payload_removed"] = True

    payload = drill_payload(
        args,
        stamp=stamp,
        dump=dump,
        taken=bool(args.take_dump),
        image=image,
        source_version=source_version,
        source_from=source_from,
        local=local,
        scratch=scratch,
        port=port,
        config=config,
        log_path=log_path,
        paths=paths,
        records=records,
        verdicts=verdicts,
        cleanup=cleanup,
        failure=problem,
    )
    drill_record.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print()
    if problem is not None:
        print(f"drill     FAIL — {problem}")
        print(
            f"          the restored data is left in place to look at: {config} is "
            f"its configuration,\n          {log_path} is its log"
        )
        print(f"          record: {drill_record}")
        return refusal or 1
    if not passed:
        print(
            f"drill     FAIL — the restored copy does not match the dump: "
            f"{verdicts['restored']}"
        )
        for line in lost:
            print(f"  - lost: {line}")
        for line in drift:
            print(f"  - drift: {line}")
        print(
            f"  the restored data is left in place to look at: {config} is its "
            f"configuration,\n  {log_path} is its log"
        )
        print(f"  records: {restored_record} and {drill_record}")
        return 1
    print(f"drill     PASS — the restored copy matches the dump ({dump.name})")
    print(f"          bind 127.0.0.1:{port}; Gitea {local} on both sides")
    if source_url is None:
        print(
            "          no source was named, so the dump's provenance was not "
            "compared: pass --source-url URL (or set the environment variable) to "
            "compare it"
        )
        print(f"          records: {restored_record} and {drill_record}")
        return 0
    print(f"          source comparison: {verdicts['source'].upper()}")
    print(f"          records: {restored_record} and {source_record}")
    print(f"          drill record: {drill_record}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
