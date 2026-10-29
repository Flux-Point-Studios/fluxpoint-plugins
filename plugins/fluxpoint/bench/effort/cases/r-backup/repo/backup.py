"""Back up and restore project checkouts for users of the web console.

`name`, `branch` and the uploaded `archive` come from the form; the
archive is a tar file the user uploads.
"""
import os
import re
import shlex
import subprocess
import tarfile

ROOT = "/var/lib/projects"
BACKUPS = "/var/lib/backups"
NAME = re.compile(r"^[a-z0-9-]{1,40}$")


def project_dir(name):
    if not NAME.match(name):
        raise ValueError("bad project name")
    return os.path.join(ROOT, name)


def backup(name, branch):
    path = project_dir(name)
    out_path = os.path.join(BACKUPS, name + ".tar")
    cmd = f"git -C {shlex.quote(path)} archive --format=tar {branch} > {shlex.quote(out_path)}"  # @seed:branch-injection
    subprocess.run(cmd, shell=True, check=True)
    return out_path


def restore(name, archive):
    path = project_dir(name)
    os.makedirs(path, exist_ok=True)
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            if member.name.startswith("/"):
                raise ValueError("absolute path in archive")
        tar.extractall(path)  # @seed:tar-traversal


def size_of(name):
    path = project_dir(name)
    out = subprocess.run(["du", "-sb", path], capture_output=True, text=True, check=True)  # @decoy:du-argv
    return int(out.stdout.split()[0])


def safe_label(text):
    return shlex.quote(text)  # @decoy:quote
