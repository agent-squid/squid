"""Repair Python console-script shebangs left behind by a moved virtualenv."""
from pathlib import Path
import sys


def repair(venv):
    venv = Path(venv).resolve()
    bin_dir = venv / "bin"
    python = bin_dir / "python"
    if not python.exists():
        raise RuntimeError(f"Missing virtualenv interpreter: {python}")
    for script in bin_dir.iterdir():
        if script.is_symlink() or not script.is_file():
            continue
        with script.open("rb") as stream:
            first = stream.readline(4096)
        if not first.startswith(b"#!"):
            continue
        old = Path(first[2:].strip().decode("utf-8", errors="replace"))
        if (old.parent.name != "bin" or old.parent.parent.name != ".venv"
                or not old.name.startswith("python") or old.exists()):
            continue
        contents = script.read_bytes()
        script.write_bytes(b"#!" + str(python).encode() + b"\n" + contents[len(first):])
        print(f"Repaired virtualenv launcher: {script.name}")
    # Hatch's editable install also records the source directory absolutely.
    root = venv.parent
    if (root / "agent" / "cli.py").is_file():
        for registration in venv.glob("lib/python*/site-packages/_editable_impl_agentsquid.pth"):
            lines = registration.read_text().splitlines()
            if len(lines) == 1 and Path(lines[0]).is_absolute() and not Path(lines[0]).exists():
                registration.write_text(str(root) + "\n")
                print("Repaired Squid editable package path")


if __name__ == "__main__":
    repair(sys.argv[1])
