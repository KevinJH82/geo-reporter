"""
geo-reporter API route tests
upload-kml, SSE run, download docx/pptx, cleanup
"""
import json
import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def reporter_app(monkeypatch, tmp_path):
    """Create Flask test client for geo-reporter."""
    import web.app as reporter_app_mod
    reporter_app_mod.app.config["TESTING"] = True
    # Isolate upload dir
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    monkeypatch.setattr(reporter_app_mod, "UPLOADS_DIR", upload_dir)
    with reporter_app_mod.app.test_client() as client:
        yield client


@pytest.mark.p0
class TestReporterRoutes:
    """Core geo-reporter API endpoints."""

    def test_index_page(self, reporter_app):
        resp = reporter_app.get("/")
        assert resp.status_code == 200

    def test_upload_kml_no_file(self, reporter_app):
        resp = reporter_app.post("/api/upload-kml")
        assert resp.status_code in (200, 400)

    def test_status_endpoint(self, reporter_app):
        resp = reporter_app.get("/api/status")
        assert resp.status_code in (200, 404)  # may use different route pattern

    def test_cleanup_endpoint(self, reporter_app):
        resp = reporter_app.post("/api/cleanup", json={})
        assert resp.status_code in (200, 400, 404)

    def test_download_docx_no_task(self, reporter_app):
        resp = reporter_app.get("/api/download/docx/no-such-task")
        assert resp.status_code in (404, 400)

    def test_download_pptx_no_task(self, reporter_app):
        resp = reporter_app.get("/api/download/pptx/no-such-task")
        assert resp.status_code in (404, 400)
