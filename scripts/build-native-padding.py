#!/usr/bin/env python3
"""Build the pinned padding extension; never replace the packaged Herdr binary."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BASE_REVISION = "7b116c05bfda646af39d2524c54e70c751f57ee8"
OWNER = "blr.herdr-shell.native-spacing"
PATCH = ROOT / "integrations/herdr-native-spacing-v1.patch"


def run(*args, cwd=None, env=None, capture=False):
    return subprocess.run(args, cwd=cwd, env=env, check=True, text=True,
                          capture_output=capture)


def zig_binary():
    candidate = os.environ.get("ZIG")
    if not candidate and shutil.which("mise"):
        result = subprocess.run(["mise", "where", "zig@0.16.0"], capture_output=True, text=True)
        if result.returncode == 0:
            candidate = str(Path(result.stdout.strip()) / "zig")
    if not candidate:
        candidate = shutil.which("zig")
    if not candidate:
        raise ValueError("Building Herdr requires Zig 0.16.0. Set ZIG or run mise install zig@0.16.0.")
    if run(candidate, "version", capture=True).stdout.strip() != "0.16.0":
        raise ValueError("The pinned Herdr build requires Zig 0.16.0.")
    return candidate


def capabilities(binary):
    reply = json.loads(run(str(binary), "--client-capabilities", capture=True).stdout)
    if not isinstance(reply, dict) or type(reply.get("pane_spacing")) is not int or reply["pane_spacing"] != 1:
        raise ValueError("The candidate does not implement native pane spacing version 1.")


def atomic_copy(source, target, *, replace=False):
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".herdr-", dir=target.parent, delete=False) as stream:
        staged = Path(stream.name)
    try:
        shutil.copyfile(source, staged)
        staged.chmod(0o755)
        if replace:
            os.replace(staged, target)
        else:
            os.link(staged, target)
    finally:
        staged.unlink(missing_ok=True)


def atomic_write_marker(path, text):
    fd, name = tempfile.mkstemp(prefix=".herdr-owner-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def install(binary):
    folder = Path.home() / ".local/share/herdr-shell/native-spacing"
    target = folder / "herdr"
    marker = folder / "owner.json"
    link = Path.home() / ".local/bin/herdr"
    owned = None
    if folder.is_symlink():
        raise ValueError(f"Refusing a symlinked native directory: {folder}")
    if folder.exists():
        try:
            owned = json.loads(marker.read_text())
        except (OSError, ValueError) as exc:
            raise ValueError(f"Refusing to replace an unowned native directory: {folder}") from exc
        if not isinstance(owned, dict) or owned.get("owner") != OWNER:
            raise ValueError(f"Refusing to replace an unowned native directory: {folder}")
    if target.is_symlink() or marker.is_symlink():
        raise ValueError("Native binary and ownership marker must not be symlinks.")
    if target.exists():
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if not owned or digest not in (owned.get("binary_sha256"), owned.get("previous_binary_sha256")):
            raise ValueError(f"Managed native binary changed outside this builder; refusing to replace it: {target}")
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != target.resolve():
            raise ValueError(f"Refusing to replace an unrelated executable: {link}")
    folder.mkdir(parents=True, exist_ok=True)
    metadata = {"owner": OWNER, "base_revision": BASE_REVISION,
                "patch_sha256": hashlib.sha256(PATCH.read_bytes()).hexdigest(),
                "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}
    # Both hashes identify our artifacts if an install is interrupted around replacement.
    if owned and target.exists():
        metadata["previous_binary_sha256"] = digest
    atomic_write_marker(marker, json.dumps(metadata, indent=2) + "\n")
    atomic_copy(binary, target, replace=True)
    metadata.pop("previous_binary_sha256", None)
    atomic_write_marker(marker, json.dumps(metadata, indent=2) + "\n")
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.is_symlink():
        # Exclusive symlink creation refuses a concurrent unrelated install.
        link.symlink_to(target)
    print(f"Installed {target}\nCommand: {link}\nPackaged /usr/bin/herdr was not changed.")
    selected = shutil.which("herdr")
    if not selected or Path(selected).resolve() != target.resolve():
        print(f"PATH does not select this build. Prepend {folder} in your shell and desktop environment before cold-starting Herdr.")
    print("Running servers are unchanged. Use a verified server.live_handoff to apply this build without stopping panes.")


def build(source, args):
    revision = run("git", "rev-parse", "HEAD", cwd=source, capture=True).stdout.strip()
    if revision != BASE_REVISION:
        raise ValueError(f"The padding patch targets {BASE_REVISION}, not {revision}.")
    env = {**os.environ, "ZIG": zig_binary(), "CARGO_TARGET_DIR": str(source / "target")}
    run("cargo", "build", "--release", "--locked", cwd=source, env=env)
    binary = source / "target/release/herdr"
    capabilities(binary)
    if args.install:
        install(binary)
    else:
        output = args.output.expanduser().absolute()
        if output.exists() or output.is_symlink():
            raise ValueError(f"Refusing to overwrite an existing build output: {output}")
        atomic_copy(binary, output)
        print(f"Built native padding candidate: {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--install", action="store_true", help="Install only a user-owned binary and ~/.local/bin/herdr link")
    destination.add_argument("--output", type=Path, help="Save a candidate to a new file without changing the active Herdr command")
    parser.add_argument("--source", type=Path, help="Use an already-patched checkout of the pinned revision")
    args = parser.parse_args()
    if not PATCH.is_file():
        raise ValueError(f"Native padding patch is missing: {PATCH}")
    if args.source:
        build(args.source.expanduser().resolve(), args)
    else:
        with tempfile.TemporaryDirectory(prefix="herdr-native-padding-") as directory:
            source = Path(directory)
            run("git", "clone", "--depth", "1", "--branch", "v0.9.3",
                "https://github.com/herdrdev/herdr.git", str(source))
            revision = run("git", "rev-parse", "HEAD", cwd=source, capture=True).stdout.strip()
            if revision != BASE_REVISION:
                raise ValueError("The upstream tag no longer matches the pinned padding base; no build was installed.")
            run("git", "apply", str(PATCH), cwd=source)
            build(source, args)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit(str(exc)) from exc
