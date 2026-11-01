"""W18：备份一致性、校验与恢复。"""
import sqlite3

import pytest

import backup


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    db = data / "platform.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO schema_version VALUES (19, '0019_audio.sql')")
    conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO demo (name) VALUES ('原件')")
    conn.commit()
    conn.close()
    for name in ("files", "workspace", "jobs"):
        (data / name).mkdir()
    (data / "files" / "a.png").write_bytes(b"png")
    (data / "workspace" / "draft.md").write_text("原稿", encoding="utf-8")
    (data / "jobs" / "run.log").write_text("log", encoding="utf-8")

    monkeypatch.setattr(backup, "DB_PATH", db)
    monkeypatch.setattr(backup, "FILES_DIR", data / "files")
    monkeypatch.setattr(backup, "WORKSPACE_DIR", data / "workspace")
    monkeypatch.setattr(backup, "JOBS_DIR", data / "jobs")
    return data


def test_备份_校验_恢复_完整闭环(sandbox):
    target = backup.create(sandbox / "backup")
    report = backup.verify(target)
    assert report["schema_version"] == 19
    assert report["counts"] == {"files": 1, "workspace": 1, "jobs": 1}

    # 改库、删文件，再恢复
    conn = sqlite3.connect(sandbox / "platform.db")
    conn.execute("INSERT INTO demo (name) VALUES ('恢复前多出来的')")
    conn.commit()
    conn.close()
    (sandbox / "files" / "a.png").unlink()
    (sandbox / "workspace" / "draft.md").write_text("改坏了", encoding="utf-8")

    safety = backup.restore(target, yes=True)
    assert safety.is_dir() and (safety / "platform.db").is_file()
    conn = sqlite3.connect(sandbox / "platform.db")
    names = [r[0] for r in conn.execute("SELECT name FROM demo")]
    conn.close()
    assert names == ["原件"]
    assert (sandbox / "files" / "a.png").read_bytes() == b"png"
    assert (sandbox / "workspace" / "draft.md").read_text(encoding="utf-8") == "原稿"


def test_不确认就不恢复(sandbox):
    target = backup.create(sandbox / "backup")
    with pytest.raises(RuntimeError):
        backup.restore(target, yes=False)


def test_备份被改过就拒绝恢复(sandbox):
    target = backup.create(sandbox / "backup")
    with open(target / "platform.db", "ab") as fh:
        fh.write(b"tamper")
    with pytest.raises(RuntimeError):
        backup.verify(target)
    with pytest.raises(RuntimeError):
        backup.restore(target, yes=True)
