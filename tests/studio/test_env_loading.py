"""start.sh hands the process its credentials byte for byte.

A secret that arrives one character short does not fail here: it fails deep
inside a provider's token exchange, as "Invalid base64-encoded client secret:
Incorrect padding". So the parser is tested on the values that break naive
quoting — base64 padding, spaces, '#', quotes, '=' inside the value.
"""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
START_SH = REPO_ROOT / "start.sh"

ENV_BODY = """CIBIS_CONSUMER_SECRET=YWJjZGVmZ2g=
PLAIN=plain
SPACED="sh h#ok"
QUOTED='single'
WITH_EQUALS=a=b=c
APOSTROPHE=it's
EMPTY=
# comment line
not a name=ignored
"""

EXPECTED = {
    "CIBIS_CONSUMER_SECRET": "YWJjZGVmZ2g=",
    "PLAIN": "plain",
    "SPACED": "sh h#ok",
    "QUOTED": "single",
    "WITH_EQUALS": "a=b=c",
    "APOSTROPHE": "it's",
    "EMPTY": "",
}


def loader_snippet() -> str:
    """The credential block of start.sh, on its own."""
    lines = START_SH.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith('ENV_FILE=""'))
    end = next(i for i in range(start, len(lines)) if lines[i] == "fi")
    return "\n".join(lines[start:end + 1])


def run_loader(tmp_path, env_path: Path, preset=None) -> dict:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    (project / "loader.sh").write_text(loader_snippet(), encoding="utf-8")
    script = ('PY="$1"\n. ./loader.sh\n'
              'for name in ' + " ".join(EXPECTED) + '; do eval "printf \'%s=%s\\n\' $name \\"\\$$name\\""; done\n')
    (project / "probe.sh").write_text(script, encoding="utf-8")
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    if preset:
        environment.update(preset)
    finished = subprocess.run(["bash", "probe.sh", "python3"], cwd=project, env=environment,
                              capture_output=True, text=True)
    assert finished.returncode == 0, finished.stderr
    out = {}
    for line in finished.stdout.splitlines():
        if "=" in line and not line.startswith("Model credentials"):
            name, value = line.split("=", 1)
            out[name] = value
    return out


def test_every_value_arrives_exactly_as_written(tmp_path):
    (tmp_path / ".env").write_text(ENV_BODY, encoding="utf-8")

    assert run_loader(tmp_path, tmp_path / ".env") == EXPECTED


def test_a_env_beside_the_checkout_is_used(tmp_path):
    """A shared company .env one level up is a normal place to keep it."""
    (tmp_path / ".env").write_text("CIBIS_CONSUMER_SECRET=YWJjZGVmZ2g=\n", encoding="utf-8")

    assert run_loader(tmp_path, tmp_path / ".env")["CIBIS_CONSUMER_SECRET"] == "YWJjZGVmZ2g="


def test_an_exported_variable_is_never_overwritten(tmp_path):
    (tmp_path / ".env").write_text(ENV_BODY, encoding="utf-8")

    values = run_loader(tmp_path, tmp_path / ".env",
                        preset={"CIBIS_CONSUMER_SECRET": "from-the-shell"})

    assert values["CIBIS_CONSUMER_SECRET"] == "from-the-shell"
    assert values["PLAIN"] == "plain"
