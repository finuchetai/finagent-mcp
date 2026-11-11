"""Bootstrap an isolated installed runtime from a verified local bundle; no system Python edits."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import shutil
import tempfile
import sys
import venv


def verify_source(folder, manifest):
    expected = manifest["files"]
    actual = {str(p.relative_to(folder)).replace(os.sep, "/") for p in folder.rglob("*") if p.is_file()}
    if actual != set(expected):
        raise ValueError("Состав комплекта изменился; получите исходный ZIP заново")
    for name, digest in expected.items():
        path = folder / name
        if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):
            raise ValueError("Некорректный путь в комплекте")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Контрольная сумма комплекта не совпала")


def install(bundle, *, runtime_root=None, creator=None, runner=None):
    bundle = Path(bundle).resolve()
    source = bundle / "source"
    manifest = json.loads((bundle / "bundle.json").read_text(encoding="utf-8"))
    verify_source(source, manifest)
    if sys.version_info < (3, 11):
        raise ValueError("Для этого комплекта нужен Python 3.11 или новее")
    digest = hashlib.sha256(json.dumps(manifest["files"], sort_keys=True).encode()).hexdigest()
    if manifest["source_sha256"] != digest:
        raise ValueError("Некорректная контрольная сумма состава комплекта")
    root = Path(runtime_root or Path.home() / ".finagent" / "runtime") / (manifest["version"] + "-" + digest[:12])
    root.mkdir(parents=True, exist_ok=True)
    environment = root / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = root / "installed.json"
    ready = False
    if marker.is_file() and python.is_file():
        saved = json.loads(marker.read_text(encoding="utf-8"))
        ready = saved.get("source_sha256") == digest
    if not ready:
        if not python.is_file():
            (creator or venv.EnvBuilder(with_pip=True)).create(environment)
        # pip/setuptools may create build/egg-info; keep the verified original bundle immutable.
        with tempfile.TemporaryDirectory(prefix="finagent-build-") as build_dir:
            build_source = Path(build_dir) / "source"
            shutil.copytree(source, build_source)
            command = [str(python), "-X", "utf8", "-m", "pip", "install",
                       "--disable-pip-version-check", str(build_source) + "[sheets,local]"]
            (runner or subprocess.run)(command, check=True, timeout=600)
        marker.write_text(json.dumps({"source_sha256": digest, "version": manifest["version"]}), encoding="utf-8")
    return python


def main(argv=None):
    bundle = Path(__file__).resolve().parents[2]  # bundle/source/scripts/install_local.py
    try:
        python = install(bundle)
        result = subprocess.run([str(python), "-X", "utf8", "-m", "finance_mcp.local_setup", *(argv or [])])
        return result.returncode
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        print("Установка не завершена. Проверьте доступность Python, сети и целостность комплекта. "
              "Системные настройки и прежние MCP удалять не требуется.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
