import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_backend", ROOT / "tools/check_backend.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_secret_checker_detects_values_keys_and_urls(tmp_path):
    path = tmp_path / ".env"
    path.write_text("CCTV_TEST_RTSP='rtsp://example-user:example-pass@camera.example/video'\n"
                    "POSTGRES_PASSWORD=example-db-secret\n")
    tokens = checker.secret_tokens(path)
    assert checker.safe_document({"cameras": [{"camera_id": "first", "name": "First"}]}, tokens)
    for document in ({"rtsp_env": "CAMERA"}, {"url": "rtsp://other.example/video"},
                     {"detail": "camera.example"}, {"nested": [{"error": "example-pass"}]},
                     {"detail": "example-db-secret"}):
        assert not checker.safe_document(document, tokens)


def test_live_checker_outputs_no_leaked_value(tmp_path, monkeypatch, capsys):
    path = tmp_path / ".env"
    path.write_text("POSTGRES_PASSWORD=example-db-secret\n")
    def fetch(url):
        return [] if url.endswith("/api/cameras") else {"detail": "example-db-secret"}
    monkeypatch.setattr(checker, "fetch", fetch)
    assert checker.check("http://backend.example", "http://preview.example", path) == 1
    output = capsys.readouterr()
    assert "example-db-secret" not in output.err + output.out
    assert "FAIL" in output.err


def test_compose_preserves_isolation_and_secret_boundaries():
    services = yaml.safe_load((ROOT / "compose.yml").read_text())["services"]
    assert set(services) == {"retrace", "backend", "postgres"}
    assert "depends_on" not in services["retrace"]
    assert services["retrace"]["environment"]["WEB_PORT"] == "40225"
    assert "deploy" in services["retrace"]
    for name in ("backend", "postgres"):
        assert "env_file" not in services[name]
        assert not any(".env" in mount for mount in services[name].get("volumes", []))
        assert not any("RTSP" in key for key in services[name]["environment"])
    assert "ports" not in services["postgres"]
    assert services["backend"]["ports"] == ["${BACKEND_BIND_IP:-127.0.0.1}:${BACKEND_PORT:-8000}:8000"]
    dockerfile = (ROOT / "backend/Dockerfile").read_text()
    assert "--workers\", \"1" in dockerfile
    assert "USER backend" in dockerfile
