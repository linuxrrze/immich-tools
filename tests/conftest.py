import importlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from fakes.llm import FakeLLM
from fakes.servers import FakeServer

REPO = Path(__file__).resolve().parent.parent
CONFIG_PREFIXES = ('IMMICH_', 'LLM_', 'NEXTCLOUD_', 'OPENAI_')

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


@pytest.fixture
def server():
    """A fresh fake Nextcloud + Immich (v3.4.1) per test."""
    srv = FakeServer().start()
    yield srv
    srv.stop()


@pytest.fixture
def make_server():
    """Factory for servers with other options, e.g. make_server(version=(2, 0, 0))."""
    servers = []

    def make(**kwargs):
        servers.append(FakeServer(**kwargs).start())
        return servers[-1]
    yield make
    for srv in servers:
        srv.stop()


@pytest.fixture
def llm():
    fake = FakeLLM().start()
    yield fake
    fake.stop()


def clean_env(extra=None):
    """The current environment without tool settings or proxies, plus `extra`."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(CONFIG_PREFIXES) and k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy')}
    env.update(NO_PROXY='127.0.0.1,localhost', no_proxy='127.0.0.1,localhost',
               PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
    env.update(extra or {})
    return env


class Script:
    """Runs a tool as a subprocess in a test directory (its CWD, where
    config.ini and cache/ are looked up)."""

    def __init__(self, cwd, env):
        self.cwd, self.env = Path(cwd), dict(env)

    def _cmd(self, name, args):
        return [sys.executable, str(REPO / name), *args]

    def run(self, name, *args, env=None, timeout=60):
        return subprocess.run(self._cmd(name, args), cwd=self.cwd, env=clean_env({**self.env, **(env or {})}),
                              capture_output=True, text=True, timeout=timeout)

    def start(self, name, *args, env=None):
        """Start in the background; output goes to a file (read with output())."""
        self._out = open(self.cwd / f'{name}.out', 'w+')
        return subprocess.Popen(
            self._cmd(name, args), cwd=self.cwd, env=clean_env({**self.env, **(env or {})}),
            stdout=self._out, stderr=subprocess.STDOUT, text=True,
            # SIGINT may be ignored if pytest runs as a background job; the
            # child must get Python's normal KeyboardInterrupt handling.
            preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))

    def output(self):
        self._out.seek(0)
        return self._out.read()


@pytest.fixture
def script(tmp_path):
    """Script runner with cwd=tmp_path; set script.env before running."""
    return Script(tmp_path, {})


@pytest.fixture
def load(monkeypatch, tmp_path):
    """Import the client modules in-process with the given settings.

    load(env, config_ini=None) -> (immich_client, nextcloud_client or None).
    Runs in tmp_path (so the repo's config.ini is never read), imports the
    modules afresh because they read their settings at import, and resets
    the cached server version.
    """
    monkeypatch.chdir(tmp_path)
    for key in list(os.environ):
        if key.startswith(CONFIG_PREFIXES):
            monkeypatch.delenv(key)
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')

    def _load(env, config_ini=None, nextcloud=True):
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        if config_ini is not None:
            (tmp_path / 'config.ini').write_text(config_ini)
        for name in ('immich_client', 'nextcloud_client'):
            monkeypatch.delitem(sys.modules, name, raising=False)
        immich = importlib.import_module('immich_client')
        immich._server_version = None
        nc = importlib.import_module('nextcloud_client') if nextcloud else None
        return immich, nc
    return _load
