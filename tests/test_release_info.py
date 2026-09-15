from __future__ import annotations

from release_info import get_release_info


def test_release_info_prefers_render_metadata(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_BRANCH", "main")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "92d8569eb7f736a35fd42d117b1fa5581f56c43d")
    monkeypatch.setenv("APP_ENV", "production")

    assert get_release_info() == {
        "branch": "main",
        "commit": "92d8569eb7f736a35fd42d117b1fa5581f56c43d",
        "commit_short": "92d8569",
        "environment": "production",
    }


def test_release_info_does_not_expose_untrusted_commit_as_short(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_BRANCH", "main")
    monkeypatch.setenv("RENDER_GIT_COMMIT", "not-a-sha")
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)

    info = get_release_info()

    assert info["commit"] == "not-a-sha"
    assert info["commit_short"] == "unknown"
    assert info["environment"] == "unknown"


def test_release_info_falls_back_to_generic_runtime_names(monkeypatch):
    monkeypatch.delenv("RENDER_GIT_BRANCH", raising=False)
    monkeypatch.delenv("RENDER_GIT_COMMIT", raising=False)
    monkeypatch.setenv("GIT_BRANCH", "feature/test")
    monkeypatch.setenv("GIT_COMMIT", "abcdef0123456789")
    monkeypatch.setenv("ENVIRONMENT", "test")

    assert get_release_info() == {
        "branch": "feature/test",
        "commit": "abcdef0123456789",
        "commit_short": "abcdef0",
        "environment": "test",
    }
