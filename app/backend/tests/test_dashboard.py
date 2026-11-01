"""W16：看板只读聚合与镜头清单导出。"""
import services
from repositories.content import episodes, shots


def test_看板数字与权威表一致(as_role, seeded):
    c = as_role("editor")
    pid = seeded["project_id"]
    got = c.get(f"/api/projects/{pid}/dashboard")
    assert got.status_code == 200, got.text
    data = got.json()
    assert data["episode_total"] == 1
    assert data["shots"] == 1 and data["assets"] == 1 and data["documents"] == 1
    # 别的用例可能往同一个 seeded 项目里加过作业；这里只验"总数与分组自洽"
    assert data["job_total"] == sum(x["count"] for x in data["jobs"])
    assert any(x["status"] == "succeeded" and x["count"] >= 1 for x in data["jobs"])
    # 本地假适配器不计费：成本桶不该凭空多一行
    assert data["costs"] == []


def test_看板跟着镜头增加而变(as_role, fresh_project):
    c = as_role("editor")
    pid = fresh_project("editor")
    ep = episodes.create(pid, "第 1 集")["id"]
    shots.create(ep, "第一镜", "", 5.0, [])
    assert c.get(f"/api/projects/{pid}/dashboard").json()["shots"] == 1
    shots.create(ep, "第二镜", "雨夜", 6.0, [])
    data = c.get(f"/api/projects/{pid}/dashboard").json()
    assert data["shots"] == 2


def test_导出_CSV_与_Markdown(as_role, seeded):
    c = as_role("editor")
    pid = seeded["project_id"]
    csv = c.get(f"/api/projects/{pid}/export/shots.csv")
    assert csv.status_code == 200
    assert csv.headers["content-type"].startswith("text/csv")
    text = csv.text
    assert text.startswith("\ufeff集序,集标题,镜号,内容")
    assert "开场" in text

    md = c.get(f"/api/projects/{pid}/export/shots.md")
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert "镜头清单" in md.text and "| 第 1 集 | 1 | 开场 |" in md.text


def test_看不见项目就_404(as_role, seeded):
    pid = seeded["project_id"]
    assert as_role("outsider").get(f"/api/projects/{pid}/dashboard").status_code == 404
    assert as_role("outsider").get(
        f"/api/projects/{pid}/export/shots.csv").status_code == 404
    assert as_role("viewer").get(f"/api/projects/{pid}/dashboard").status_code == 404


def test_项目名进_Markdown_标题(as_role, seeded):
    c = as_role("editor")
    pid = seeded["project_id"]
    md = c.get(f"/api/projects/{pid}/export/shots.md").text
    assert md.startswith("# ") and "· 镜头清单" in md.splitlines()[0]
