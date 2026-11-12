#!/usr/bin/env python3
"""Upgrade DashPlay in place on a running OneCloud / Phicomm N1 box, keeping everything the owner set up.

    python3 upgrade.py BUNDLE.tar [--dry-run]     # as root, on the box
    python3 upgrade.py --rollback                 # put the program from before the last upgrade back

BUNDLE.tar is dashplay-bundle-<version>.tar from a DashPlay release. Only program files change: state/
(first-run setup, hotspot name and password, paired iPhones, certificates, logs), identity/ (the CarPlay
identity) and service.json (this box's interface and channel) stay as they are. Files the new program no
longer has are removed; each file is replaced whole (a new file renamed over the old one). The program from
before is kept in /opt/dashplay-previous.tar for --rollback. Then the service restarts; when its check fails or
it does not come up, the previous program goes back by itself.

Restarting the service restarts the box's Wi-Fi, so an SSH session over that Wi-Fi drops: run it apart from
the session, and read the outcome afterwards:
    systemd-run --unit=dashplay-upgrade --collect python3 /tmp/upgrade.py /tmp/dashplay-bundle.tar
    cat /opt/dashplay/state/upgrade.log
For the same system packages and services as when the box was installed (firmware/install.sh); a release
that changes those says so and needs the firmware image instead.
"""
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path("/opt/dashplay")
PREVIOUS = Path("/opt/dashplay-previous.tar")
PROGRAM_BACKUP_FSYNC = 1  # capability checked by the updater without importing/executing this tool
PLATFORM_UPGRADE_CHECK = 1
UNIT = Path("/etc/systemd/system/dashplay.service")
KEEP = ("state/", "identity/")          # the owner's data
KEEP_FILES = ("service.json",)          # this box's own settings, written at installation
STALE = ("state/receiver/app-cds.jsa", "state/receiver/app-cds.classpath")   # belongs to the old jars
NEEDED = ("supervisor.py", "run-receiver.py", "dashplay.service", "playport/server/build/install/server/lib/server-0.1.0.jar")


def program_files(root):
    """Relative paths of the program's files under root: not the owner's data."""
    root = Path(root)
    return {str(p.relative_to(root)) for p in root.rglob("*")
            if (p.is_file() or p.is_symlink()) and not str(p.relative_to(root)).startswith(KEEP)
            and str(p.relative_to(root)) not in KEEP_FILES and "__pycache__" not in p.parts}


def plan(old, new):
    """(paths to remove, paths to write) to make the program under old match new."""
    before, after = program_files(old), program_files(new)
    remove = sorted(before - after)
    write = sorted(p for p in after if p not in before or not same(Path(old) / p, Path(new) / p))
    check_targets(old, write)
    return remove, write


def check_targets(root, paths):
    # A previously installed directory symlink must not redirect a public write into firmware files.
    root = Path(root)
    for name in paths:
        if any((root / parent).is_symlink() for parent in Path(name).parents if str(parent) != '.'):
            raise ValueError("升级目标目录包含符号链接，已停止")


def same(a, b):
    if a.is_symlink() or b.is_symlink():
        return a.is_symlink() and b.is_symlink() and os.readlink(a) == os.readlink(b)
    return a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()


def apply(old, new, remove, write):
    old, new = Path(old), Path(new)
    check_targets(old, write)
    for path in write:
        source, target = new / path, old / path
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.upgrade")
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        if source.is_symlink():
            os.symlink(os.readlink(source), temporary)
        else:
            shutil.copy2(source, temporary)
            if os.geteuid() == 0:
                os.chown(temporary, 0, 0)   # bundles made on a Mac carry its user
        os.replace(temporary, target)   # a running program keeps the old file until it restarts
    for path in remove:
        (old / path).unlink(missing_ok=True)
    for directory in sorted({p for p in old.rglob("*") if p.is_dir()}, key=lambda d: -len(d.parts)):
        relative = str(directory.relative_to(old)) + "/"
        if not relative.startswith(KEEP) and not any(directory.iterdir()):
            directory.rmdir()
    for path in STALE:
        (old / path).unlink(missing_ok=True)


def say(message, log):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    with open(log, "a") as out:
        out.write(line + "\n")


def run(*args):
    return subprocess.run(list(args), capture_output=True, text=True)


def service_up(seconds=150, check=None, steady=15):
    """Require a stable settings service and, when configured, the owner's domain/DNS/HTTPS."""
    until = time.monotonic() + seconds
    healthy_since = None
    while time.monotonic() < until:
        if run("systemctl", "is-active", "dashplay").stdout.strip() == "active" and \
                run("python3", "-c", "import socket; socket.create_connection(('127.0.0.1', 80), 2)").returncode == 0 and \
                (check is None or check()):
            if healthy_since is None:
                healthy_since = time.monotonic()
            if time.monotonic() - healthy_since >= steady:
                return True
        else:
            healthy_since = None
        time.sleep(2)
    return False


def domain_check(program, root, health=False, require_https=False):
    """Use the candidate's own reader and hooks before overwriting any files."""
    if not (root / "state/ddns.json").exists() and not require_https:
        return {"https": False}
    checker = Path(program) / "upgrade_checks.py"
    if not checker.is_file():
        raise ValueError("升级包不能验证已有的域名配置，已停止")
    command = [sys.executable, str(checker), "--root", str(root)]
    if health:
        command.append("--health")
    if require_https:
        command.append("--require-https")
    result = run(*command)
    if result.returncode:
        raise ValueError("设备域名、验证脚本、本地 DNS 或 HTTPS 检查失败")
    return json.loads(result.stdout)


def platform_environment():
    """Read the main service's selection, even when invoked by the separate version service or CLI.

    The running process also covers EnvironmentFile settings. For a stopped service only direct unit
    Environment entries are supported; refuse unreadable/ambiguous settings instead of guessing.
    """
    result = subprocess.run(['systemctl', 'show', 'dashplay.service', '--property=Environment',
                             '--property=EnvironmentFiles', '--property=MainPID'],
                            capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError("无法核对主服务的设备适配配置，已停止")
    properties = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    if 'MainPID' not in properties:
        raise ValueError("无法核对主服务的设备适配配置，已停止")
    wanted = ('DASHPLAY_PLATFORM_MODULE', 'PYTHONPATH')
    declared = dict(item.split('=', 1) for item in shlex.split(properties.get('Environment', '')) if '=' in item)
    declared = {key: value for key, value in declared.items() if key in wanted}
    pid = int(properties['MainPID'])
    if pid < 0:
        raise ValueError("无法核对主服务的设备适配配置，已停止")
    running = {}
    if pid > 0:
        raw = Path(f'/proc/{pid}/environ').read_bytes().decode()
        running = dict(item.split('=', 1) for item in raw.split('\0') if '=' in item)
        running = {key: value for key, value in running.items() if key in wanted}
        if any(key in running and running[key] != value for key, value in declared.items()):
            raise ValueError("主服务的设备适配配置已改变，请重启主服务后再升级")
    elif properties.get('EnvironmentFiles'):
        raise ValueError("请先启动主服务，再核对设备适配配置")
    return {**running, **declared}


def platform_check(program, root):
    """Reject incompatible public bundles before backup, service stop or replacement."""
    settings = platform_environment()
    if 'DASHPLAY_PLATFORM_MODULE' not in settings:
        return
    source = Path(program) / 'platform_api.py'
    try:
        tree = ast.parse(source.read_text())
        version = next(node.value.value for node in tree.body if isinstance(node, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == 'PLATFORM_API_VERSION' for t in node.targets)
                       and isinstance(node.value, ast.Constant))
    except (OSError, UnicodeError, SyntaxError, StopIteration):
        raise ValueError("升级包缺少设备所需的适配入口，已停止") from None
    if type(version) is not int or version != 1:
        raise ValueError("升级包的设备适配接口不兼容，已停止")
    env = os.environ.copy()
    # Never let the installed program mask missing dependencies in the candidate.
    paths = [str(Path(p).resolve()) for p in settings.get('PYTHONPATH', '').split(os.pathsep) if p]
    paths = [p for p in paths if not Path(p).is_relative_to(Path(root).resolve())
             and not Path(p).is_relative_to(Path(program).resolve())]
    env['PYTHONPATH'] = os.pathsep.join(paths)
    env['DASHPLAY_PLATFORM_MODULE'] = settings['DASHPLAY_PLATFORM_MODULE']
    result = subprocess.run([sys.executable, '-B', str(source), '--check-external', str(root)],
                            cwd=program, env=env, capture_output=True, text=True, timeout=30)
    if result.returncode or result.stdout.strip() != 'platform-ok:1':
        raise ValueError("升级包与设备已有适配不兼容，已停止")


def backup(root):
    temporary = PREVIOUS.with_suffix(".tar.tmp")
    with tarfile.open(temporary, "w") as out:
        for path in sorted(program_files(root)):
            out.add(root / path, arcname=path, recursive=False)
    temporary.chmod(0o600)
    # Close the tar (including its end records), then sync before publishing.
    # A failure here keeps the previous archive and must not stop the service.
    with temporary.open('rb') as saved:
        os.fsync(saved.fileno())
    os.replace(temporary, PREVIOUS)
    directory = os.open(PREVIOUS.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def extract_bundle(bundle, staging):
    # Older Python versions without the security backport must not extract as root unchecked.
    if not hasattr(tarfile, "data_filter"):
        raise ValueError("Python lacks safe tar extraction; update Python before upgrading")
    with tarfile.open(bundle) as archive:
        archive.extractall(staging, filter="data")


def install_unit(root, log):
    unit = root / "dashplay.service"
    if not UNIT.exists() or UNIT.read_bytes() != unit.read_bytes():
        shutil.copy2(unit, UNIT)
        run("systemctl", "daemon-reload")
        say("service unit updated", log)


def restore(root, log):
    """Put the program from PREVIOUS back (its files over the current ones, files it lacked removed)."""
    with tempfile.TemporaryDirectory(prefix="dashplay-previous.") as staging:
        extract_bundle(PREVIOUS, staging)
        remove, write = plan(root, staging)
        apply(root, staging, remove, write)
    install_unit(root, log)
    say(f"previous program restored ({len(write)} files back, {len(remove)} removed)", log)


def main(argv):
    if os.geteuid() != 0:
        raise SystemExit("run as root")
    if not (ROOT / "supervisor.py").exists():
        raise SystemExit(f"no DashPlay in {ROOT}: install it with the firmware image instead")
    log = ROOT / "state" / "upgrade.log"
    if argv[:1] == ["--rollback"]:
        if not PREVIOUS.exists():
            raise SystemExit(f"{PREVIOUS} missing: nothing to roll back to")
        restore(ROOT, log)
        run("systemctl", "restart", "dashplay")
        up = service_up()
        say("rolled back; service " + ("up" if up else "NOT up: send state/upgrade.log and receiver.log"), log)
        return 0 if up else 1
    if not argv or not Path(argv[0]).is_file():
        raise SystemExit(__doc__)
    bundle, dry = Path(argv[0]), "--dry-run" in argv
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix="dashplay-upgrade.") as staging:
        extract_bundle(bundle, staging)
        missing = [p for p in NEEDED if not (Path(staging) / p).exists()]
        if missing:
            raise SystemExit(f"{bundle} is not a DashPlay program bundle (missing {', '.join(missing)})")
        try:
            platform_check(Path(staging), ROOT)
            expected = domain_check(Path(staging), ROOT)
            remove, write = plan(ROOT, staging)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            say(str(error), log)
            return 1
        domain_file = ROOT / "state/ddns.json"
        domain_before = domain_file.read_bytes() if domain_file.exists() else None
        print("\n".join([f"replace/add {p}" for p in write] + [f"remove {p}" for p in remove]) or "nothing to change")
        if dry:
            return 0
        if not write and not remove:
            say(f"already at bundle {digest[:12]}", log)
            return 0
        say(f"upgrade to bundle sha256 {digest}: {len(write)} files new or changed, {len(remove)} removed", log)
        try:
            backup(ROOT)
        except (OSError, tarfile.TarError) as error:
            say(f"program backup failed ({type(error).__name__}); current program left unchanged", log)
            return 1
        say(f"previous program saved in {PREVIOUS}", log)
        # Stop before replacing hooks or modules a signer might still be using.
        if run("systemctl", "stop", "dashplay").returncode:
            say("could not stop service; program left unchanged", log)
            return 1
        try:
            apply(ROOT, staging, remove, write)
        except (OSError, ValueError):
            say("program replacement failed; restoring previous program", log)
            restore(ROOT, log)
            run("systemctl", "restart", "dashplay")
            return 1
    install_unit(ROOT, log)
    check = run("python3", str(ROOT / "supervisor.py"), "--root", str(ROOT), "--check")
    if check.returncode != 0:
        say("new program failed its check: " + (check.stderr or check.stdout).strip()[-400:], log)
        restore(ROOT, log)
        run("systemctl", "restart", "dashplay")
        return 1
    say("check passed; restarting the service (the box's Wi-Fi restarts too)", log)
    run("systemctl", "restart", "dashplay")

    def domain_healthy():
        try:
            if (domain_file.read_bytes() if domain_file.exists() else None) != domain_before:
                return False
            domain_check(ROOT, ROOT, health=True, require_https=expected.get("https", False))
            return True
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    if service_up(check=domain_healthy):
        (ROOT / "state" / "upgrade.json").write_text(json.dumps({"bundle_sha256": digest, "at": int(time.time())}) + "\n")
        say("upgrade done; service up", log)
        return 0
    say("service or domain/DNS/HTTPS did not become healthy: rolling back", log)
    restore(ROOT, log)
    run("systemctl", "restart", "dashplay")
    say("rolled back; service " + ("up" if service_up() else "NOT up"), log)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
