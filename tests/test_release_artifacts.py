import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from scripts.verify_release_artifacts import verify_sdist_contents


def test_sdist_policy_rejects_node_modules(tmp_path: Path) -> None:
    archive_path = tmp_path / "package.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        info = tarfile.TarInfo("package/web/node_modules/vendor.js")
        info.size = 1
        archive.addfile(info, __import__("io").BytesIO(b"x"))

    try:
        verify_sdist_contents(archive_path)
    except AssertionError as error:
        assert "dependency tree" in str(error)
    else:
        raise AssertionError("node_modules must not be accepted in an sdist")
