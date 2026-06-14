import os
import pytest
from pathlib import Path
from friday.mcp_server import files, safety


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    # Confine all destructive ops to tmp_path for the duration of the test.
    monkeypatch.setattr(files, "ALLOWED_ROOTS", [tmp_path.resolve()])
    safety._reset()
    yield
    safety._reset()


def test_safe_path_accepts_inside_root(tmp_path):
    p = files._safe_path(str(tmp_path / "sub" / "x.txt"))
    assert str(p).startswith(str(tmp_path.resolve()))


def test_safe_path_rejects_traversal():
    with pytest.raises(ValueError):
        files._safe_path("../../../etc/passwd")


def test_safe_path_rejects_absolute_outside(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(ValueError):
        files._safe_path(str(outside))


def test_safe_path_rejects_symlink_escape(tmp_path):
    target_outside = tmp_path.parent / "secret"
    target_outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    try:
        os.symlink(target_outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this machine")
    with pytest.raises(ValueError):
        files._safe_path(str(link / "f.txt"))


def test_create_folder_inside_root(tmp_path):
    msg = files.create_folder(str(tmp_path / "newdir"))
    assert (tmp_path / "newdir").is_dir()
    assert "Created" in msg


def test_create_folder_outside_root_rejected(tmp_path):
    msg = files.create_folder(str(tmp_path.parent / "evil"))
    assert "outside allowed roots" in msg
    assert not (tmp_path.parent / "evil").exists()


def test_delete_path_stages_not_runs(tmp_path):
    f = tmp_path / "doomed.txt"
    f.write_text("bye")
    msg = files.delete_path(str(f))
    assert msg.startswith("CONFIRM_REQUIRED")
    assert f.exists()  # not deleted yet


def test_delete_path_confirm_runs(tmp_path):
    f = tmp_path / "doomed.txt"
    f.write_text("bye")
    files.delete_path(str(f))
    safety.confirm_action(safety._pending.id)
    assert not f.exists()


def test_delete_path_outside_root_rejected(tmp_path):
    msg = files.delete_path(str(tmp_path.parent / "x.txt"))
    assert "outside allowed roots" in msg
    assert safety._pending is None  # never staged


def test_move_path_stages_and_confirm_runs(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hi")
    dst = tmp_path / "b.txt"
    msg = files.move_path(str(src), str(dst))
    assert msg.startswith("CONFIRM_REQUIRED")
    safety.confirm_action(safety._pending.id)
    assert dst.exists() and not src.exists()


def test_search_files_finds_match(tmp_path):
    (tmp_path / "report.txt").write_text("x")
    out = files.search_files("report", str(tmp_path))
    assert "report.txt" in out
