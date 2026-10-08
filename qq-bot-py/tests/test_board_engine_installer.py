import hashlib
import importlib.util
import io
from pathlib import Path
import stat
import zipfile

import pytest

spec = importlib.util.spec_from_file_location("_board_engine_installer_tests",
    Path(__file__).parents[1] / "tools/install_board_engines.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class Response(io.BytesIO):
    url = "https://example.invalid/engine"


class Opener:
    def __init__(self, content):
        self.content = content
    def open(self, request, timeout):
        assert request.full_url.startswith("https://")
        assert timeout == 30
        return Response(self.content)


def test_download_digest_and_does_not_overwrite(tmp_path):
    content = b"trusted test engine"
    sha = hashlib.sha256(content).hexdigest()
    target = tmp_path / "engine"
    result = installer.download(Opener(content), "https://example.invalid/engine", target, sha)
    assert result["sha256"] == sha
    assert target.read_bytes() == content
    with pytest.raises(FileExistsError):
        installer.download(Opener(b"other"), "https://example.invalid/engine", target, sha)
    assert target.read_bytes() == content


@pytest.mark.parametrize("content,expected,maximum", [(b"", None, 100), (b"bad", "0"*64, 100),
                                                      (b"12345", None, 4)])
def test_download_rejects_empty_bad_hash_or_oversized(tmp_path, content, expected, maximum):
    with pytest.raises(ValueError):
        installer.download(Opener(content), "https://example.invalid/engine", tmp_path / "engine", expected, maximum)


def test_download_rejects_plain_http_before_request(tmp_path):
    with pytest.raises(ValueError):
        installer.download(Opener(b"never"), "http://example.invalid/engine", tmp_path / "engine")
    assert not (tmp_path / "engine").exists()


@pytest.mark.parametrize("name", ["../escape", "/absolute", "C:/escape", "..\\escape"])
def test_archive_traversal_rejected(tmp_path, name):
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr(name, b"bad")
    with pytest.raises(ValueError):
        installer.extract_safe(archive, tmp_path / "stage")
    assert not (tmp_path / "escape").exists()


def test_archive_symlink_rejected(tmp_path):
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as packed:
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        packed.writestr(info, "../target")
    with pytest.raises(ValueError):
        installer.extract_safe(archive, tmp_path / "stage")


def test_archive_normal_files_and_no_overwrite(tmp_path):
    archive = tmp_path / "release.zip"
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr("bin/katago", b"test-only")
    target = tmp_path / "stage"
    installer.extract_safe(archive, target)
    assert (target / "bin/katago").read_bytes() == b"test-only"
    with pytest.raises(FileExistsError):
        installer.extract_safe(archive, target)


def test_existing_install_never_overwritten_or_downloaded(tmp_path):
    target = tmp_path / "data/engines/fairy-stockfish" / installer.XQ_TAG
    target.mkdir(parents=True)
    sentinel = target / "precious"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="already exists"):
        installer.install_game(tmp_path, "xiangqi", "avx2", None)
    assert sentinel.read_text() == "keep"


def test_architecture_rejected_before_mount_or_download(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.platform, "system", lambda: "Windows")
    with pytest.raises(ValueError, match="Linux x86_64"):
        installer.preflight(tmp_path)


def test_pinned_manifest_contains_expected_cpu_assets():
    assert len(installer.ASSETS) == 5
    for url, sha in installer.ASSETS.values():
        assert url.startswith("https://")
        assert len(sha) == 64 and set(sha) <= set("0123456789abcdef")
    assert "numEigenThreadsPerModel = 1" in installer.GO_CONFIG
    assert "nnCacheSizePowerOfTwo = 14" in installer.GO_CONFIG
    assert "logAllGTPCommunication = false" in installer.GO_CONFIG
