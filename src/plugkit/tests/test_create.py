"""Tests for the create-cordis scaffolding CLI."""

import io
import json
import os
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from plugkit.cordis.create import TEMPLATE, main, scaffold


def build_template_sdist(version: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        files = {
            f"my-template-{version}/pyproject.toml": (
                '[project]\nname = "__NAME__"\nversion = "0.1.0"\n'
                "dependencies = [\"cordis\"]\n\n"
                "[dependency-groups]\ndev = [\"pytest\"]\n"
            ),
            f"my-template-{version}/main.py": 'print("__NAME__")\n',
            f"my-template-{version}/cordis.yml": "- id: main\n  name: ./main\n",
        }
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.fixture()
def registry(tmp_path):
    """A minimal PyPI-JSON-API server serving a fake template package."""
    tarballs = {"1.0.0": build_template_sdist("1.0.0"), "0.9.0": build_template_sdist("0.9.0")}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path.endswith("/my-template/json"):
                body = json.dumps(
                    {
                        "info": {"name": "my-template", "version": "1.0.0"},
                        "releases": {
                            "1.0.0": [
                                {
                                    "packagetype": "sdist",
                                    "url": f"http://127.0.0.1:{self.server.server_address[1]}/packages/my-template-1.0.0.tar.gz",
                                }
                            ],
                            "0.9.0": [
                                {
                                    "packagetype": "sdist",
                                    "url": f"http://127.0.0.1:{self.server.server_address[1]}/packages/my-template-0.9.0.tar.gz",
                                }
                            ],
                        },
                        "urls": [],
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            for version, payload in tarballs.items():
                if path.endswith(f"/my-template-{version}.tar.gz"):
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}/pypi"
    server.shutdown()


def test_scaffold_creates_project(tmp_path):
    target = str(tmp_path / "my-app")
    result = scaffold("my-app", target, yes=True)
    assert result == target
    for filename in TEMPLATE:
        assert os.path.exists(os.path.join(target, filename))
    with open(os.path.join(target, "pyproject.toml"), encoding="utf-8") as file:
        assert 'name = "my-app"' in file.read()
    with open(os.path.join(target, "main.py"), encoding="utf-8") as file:
        assert "from plugkit.cordis import Context" in file.read()


def test_scaffold_cli(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["cli-app", "-y"]) == 0
    assert os.path.exists(os.path.join(tmp_path, "cli-app", "cordis.yml"))


def test_scaffold_fetches_remote_template(tmp_path, registry):
    target = str(tmp_path / "remote-app")
    scaffold("remote-app", target, yes=True, template="my-template", mirror=registry)
    assert os.path.exists(os.path.join(target, "pyproject.toml"))
    assert os.path.exists(os.path.join(target, "main.py"))
    assert os.path.exists(os.path.join(target, "cordis.yml"))
    # the top-level directory of the sdist is stripped
    assert not os.path.exists(os.path.join(target, "my-template-1.0.0"))
    with open(os.path.join(target, "pyproject.toml"), encoding="utf-8") as file:
        content = file.read()
        assert 'name = "remote-app"' in content
    with open(os.path.join(target, "main.py"), encoding="utf-8") as file:
        assert 'print("remote-app")' in file.read()


def test_scaffold_pins_template_version(tmp_path, registry):
    target = str(tmp_path / "pinned-app")
    scaffold("pinned-app", target, yes=True, template="my-template", ref="0.9.0", mirror=registry)
    with open(os.path.join(target, "main.py"), encoding="utf-8") as file:
        assert 'print("pinned-app")' in file.read()


def test_scaffold_rejects_unknown_version(tmp_path, registry):
    with pytest.raises(Exception, match="unknown template version"):
        scaffold("bad-app", str(tmp_path / "bad-app"), yes=True, template="my-template", ref="9.9.9", mirror=registry)


def test_scaffold_prod_strips_dev_groups(tmp_path, registry):
    target = str(tmp_path / "prod-app")
    scaffold("prod-app", target, yes=True, template="my-template", mirror=registry, prod=True)
    with open(os.path.join(target, "pyproject.toml"), encoding="utf-8") as file:
        content = file.read()
    assert "[dependency-groups]" not in content
    assert "pytest" not in content


def test_scaffold_prod_keeps_dev_groups_by_default(tmp_path, registry):
    target = str(tmp_path / "dev-app")
    scaffold("dev-app", target, yes=True, template="my-template", mirror=registry)
    with open(os.path.join(target, "pyproject.toml"), encoding="utf-8") as file:
        assert "[dependency-groups]" in file.read()
