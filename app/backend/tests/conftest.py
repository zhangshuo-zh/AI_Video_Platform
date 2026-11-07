"""测试地基（W01-a）。

三条设计决定，都是为"测出来的绿是真的"服务的：

1. **环境变量必须在 import config 之前设**。`config.py` 在模块导入时就把
   `FASTVIDEO_DB` 读成常量，晚设等于没设；而 `import main` 会立刻跑
   `migrate()` + `ensure_admin()`，所以这段 env 设置必须写在文件最顶上、
   任何项目 import 之前。
2. **测试不起端口、不真登录**。用 starlette 的 TestClient 走 ASGI 进程内调用，
   会话靠 `security.new_session_token()` + `sessions.create()` 直接铸一个
   cookie。真登录要过 scrypt 校验，慢且要把口令传来传去——铸会话既快又不碰口令。
3. **一个 session 共用一个库**。43 条路由的守卫回归是只读断言，共用没问题；
   会改数据的用例一律自己建项目（名字带 uuid），不假设库里干净。
"""
import os
import tempfile
import uuid
from datetime import datetime, timedelta
from pathlib import Path

# ↓ 必须在任何项目 import 之前
_TMP = Path(tempfile.mkdtemp(prefix="fvtest-"))
os.environ["FASTVIDEO_DB"] = str(_TMP / "test.db")
os.environ["FASTVIDEO_FILES"] = str(_TMP / "files")
# 稿件工作区也必须在临时目录里：不设在 import main 之前，测试就会在真正的
# backend/data/workspace 里建 Git 仓库、把测试文档提交进去——那是污染用户的稿子目录。
os.environ["FASTVIDEO_WORKSPACE"] = str(_TMP / "workspace")
# Job 的日志与产物也必须落在临时目录，别写进用户的 backend/data/jobs
os.environ["FASTVIDEO_JOBS"] = str(_TMP / "jobs")
# 队列语义的测试用假适配器（只 sleep/写字），不碰模型权重
os.environ["FASTVIDEO_ALLOW_MOCK"] = "1"
# 让 bootstrap 走"环境变量给了口令"这条分支，行为可预期；测试从不拿它登录
os.environ["FASTVIDEO_ADMIN_PASSWORD"] = "pytest-only-not-a-real-secret"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
import services  # noqa: E402
from repositories.content import assets, episodes, shots  # noqa: E402
from repositories.projects import projects as project_repo  # noqa: E402
from repositories.jobs import jobs as job_repo
from repositories.profiles import profiles as profile_repo
from repositories.users import sessions, users as user_repo  # noqa: E402
from db import get_conn  # noqa: E402
from security import hash_password, new_session_token  # noqa: E402


@pytest.fixture(scope="session")
def app():
    return main.app


@pytest.fixture
def client(app):
    """匿名客户端：没带任何会话 cookie。

    必须是函数级——TestClient 会自己攒 cookie，session 级共享的话
    某个用例里一登录，后面"未登录应 401"的用例就变成 200，假绿。
    """
    with TestClient(app) as c:
        yield c


def _mint(user_id: int) -> str:
    """给用户铸一个会话，返回要塞进 cookie 的明文 token。"""
    token, token_hash = new_session_token()
    expires = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
    sessions.create(token_hash, user_id, expires)
    return token


@pytest.fixture(scope="session")
def people(app):
    """五种身份一次备齐。超级用户由 bootstrap 建，这里只铸会话。"""
    superadmin = user_repo.get_by_username("superadmin")
    made = {}
    for role in ("admin", "editor", "viewer"):
        uname = f"t_{role}_{uuid.uuid4().hex[:6]}"
        made[role] = user_repo.create(uname, f"{role} 测试账号", hash_password("x"),
                                      role_codes=[role])
    # 第二个 editor：用来验"别人的项目看不见＝404，而不是 403"
    outsider = user_repo.create(f"t_outsider_{uuid.uuid4().hex[:6]}", "外人",
                                hash_password("x"), role_codes=["editor"])
    return {
        "superadmin": {"id": superadmin["id"], "token": _mint(superadmin["id"]),
                       "name": superadmin["display_name"] or superadmin["username"]},
        "admin": {"id": made["admin"]["id"], "token": _mint(made["admin"]["id"]),
                  "name": made["admin"]["display_name"]},
        "editor": {"id": made["editor"]["id"], "token": _mint(made["editor"]["id"]),
                   "name": made["editor"]["display_name"]},
        "viewer": {"id": made["viewer"]["id"], "token": _mint(made["viewer"]["id"]),
                   "name": made["viewer"]["display_name"]},
        "outsider": {"id": outsider["id"], "token": _mint(outsider["id"]),
                     "name": outsider["display_name"]},
    }


@pytest.fixture
def as_role(app, people):
    """`with as_role("editor") as c:` —— 拿到带该身份 cookie 的客户端。"""
    class _Factory:
        def __call__(self, role: str) -> TestClient:
            token = people[role]["token"]
            c = TestClient(app, cookies={"fv_session": token})
            c.headers.update({"X-Test-Role": role})
            return c

        def id(self, role: str) -> int:
            return people[role]["id"]
    return _Factory()


@pytest.fixture(scope="session")
def seeded(app, people):
    """一套真实对象：项目 → 集 → 镜 → 资产 → 文档，所有人是 editor。

    一律走仓储层建，不写裸 SQL——不然测试替代码兜了底，真接口坏了还不知道。
    文档走 services.create_document：那是"文件 + Git + 元数据"三步一起的入口，
    只 docs.create 的话会留下一条库里说有、盘上没有的假文档。
    """
    pid = project_repo.create(f"测试项目_{uuid.uuid4().hex[:6]}", "介绍", ["标签A"],
                              people["editor"]["id"])["id"]
    eid = episodes.create(pid, "第 1 集")["id"]
    sid = shots.create(eid, "开场", "", 5.0, [])["id"]
    aid = assets.create(pid, "character", f"角色_{uuid.uuid4().hex[:6]}", "", "", [])["id"]
    doc = services.create_document(pid, eid, "novel", "测试稿", "燧人蹲在洞口。", "测试账号")
    # 这个 fixture 是 session 级的，可能在别的用例留下排队作业之后才第一次执行；
    # 先把那些活收掉，下面"领取到的必须是我这一条"才成立（不是靠用例顺序）。
    with get_conn() as conn:
        conn.execute(
            """UPDATE job SET status = 'cancelled',
                      finished_at = datetime('now','localtime'),
                      error_code = 'fixture_cleanup'
                WHERE status IN ('queued','starting','running','cancel_requested')""")
    # 一条已经跑完的 Job：守卫表要测"看不见的作业回 404"，需要一个真实 id。
    # 用 succeeded 而不是 queued，免得 worker 测试把它当活干。
    job = job_repo.create({
        "project_id": pid, "episode_id": eid, "shot_id": sid,
        "capability": "video", "kind": "video_local", "execution_mode": "local",
        "provider_code": "mock", "model_code": "mock-1", "adapter_version": "mock",
        "prompt_snapshot": "守卫用", "params": {"sleep": 0.0}, "input_refs": [],
        "created_by": people["editor"]["id"],
    })
    claimed = job_repo.claim_next("fixture-worker", 60)
    assert claimed is not None and claimed["id"] == job["id"]
    job_repo.succeed(job["id"], "fixture-worker", "", [], 1)
    profile = profile_repo.create({
        "project_id": pid, "name": "守卫用模板", "capability": "video",
        "provider_code": "", "model_code": "", "params": {},
        "created_by": people["editor"]["id"],
    })
    return {"project_id": pid, "episode_id": eid, "shot_id": sid, "asset_id": aid,
            "document_id": doc["row"]["id"], "commit": doc["row"]["git_commit"],
            "job_id": job["id"], "profile_id": profile["id"]}


@pytest.fixture
def provider_state(app):
    """provider_config 是全局单份的：动过就还原，免得后面的用例读到别人的配置。"""
    with get_conn() as conn:
        saved = [dict(r) for r in conn.execute("SELECT * FROM provider_config")]
    yield
    with get_conn() as conn:
        for r in saved:
            conn.execute(
                """UPDATE provider_config
                      SET provider = ?, model = ?, enabled = ?, is_default = ?,
                          key_env = ?, notes = ?
                    WHERE id = ?""",
                (r["provider"], r["model"], r["enabled"], r["is_default"],
                 r["key_env"], r["notes"], r["id"]),
            )


@pytest.fixture
def clean_queue(app):
    """worker 测试前把上一轮留下的活收掉，别让领取逻辑拿到别人的作业。"""
    with get_conn() as conn:
        conn.execute(
            """UPDATE job SET status = 'cancelled',
                      finished_at = datetime('now','localtime'),
                      error_code = 'test_cleanup'
                WHERE status IN ('queued','starting','running','cancel_requested')""")
    return None


@pytest.fixture
def fresh_project(app, people):
    """函数级：给调用方一个自己的新项目，避免互相踩。"""
    def _make(owner_role: str = "editor") -> int:
        return project_repo.create(f"临时_{uuid.uuid4().hex[:8]}", "", [],
                                   people[owner_role]["id"])["id"]
    return _make


@pytest.fixture
def register_tier(app):
    """显式提交要的登记：`register_tier("video","local","fastmetal","fastmetal-1.3b-qad")`。

    2026-11-07 起 `submit_job` 的显式那一支要求「这一档在模型仓库登记过」
    （原来要求"与 provider_config 槽位逐字相等"，那条把同能力多引擎钉死了）。
    于是所有**显式指定 provider/model** 的用例都要先登记一行——这个 fixture
    就是把那一步收成一句，用完自己清掉，不给别的用例留行。
    """
    made: list[int] = []

    def _make(capability: str, backend: str, provider: str, model_code: str,
              *, enabled: bool = True, source: str = "") -> int:
        from repositories.model_center import model_center as _mc
        row = _mc.create({
            "capability": capability, "backend": backend, "provider": provider,
            "model_code": model_code, "source": source or backend,
            "display_name": "", "version": "", "artifact_path": "",
            "remote_model_id": "", "enabled": 1 if enabled else 0, "note": "",
            "capabilities": [], "created_by": None,
        })
        made.append(row["id"])
        return row["id"]

    yield _make
    from db import get_conn
    with get_conn() as conn:
        for rid in made:
            conn.execute("DELETE FROM model_registration WHERE id = ?", (rid,))


@pytest.fixture
def delete_guard(tmp_path):
    """环境不允许删文件时跳过「真的删东西」的用例。

    本机与 CI 正常；容器/沙箱里 `unlink`／`rmtree` 可能被宿主策略拦成
    EPERM／EISDIR，用例就从一个产品断言变成一条环境错误。这类用例量的
    是「删资产有没有把图一起清掉」，不是「这台机器让不让删」——所以明说
    跳过，别把环境限制演成回归。
    """
    probe_dir = tmp_path / ".delete-probe-dir"
    probe_dir.mkdir()
    probe = probe_dir / "x"
    probe.write_text("x", encoding="utf-8")
    try:
        # 删文件与删目录是两条不同的系统调用，沙箱可能只拦其中一条；
        # 删除类用例两样都会用到，所以两样都探一次。
        probe.unlink()
        probe_dir.rmdir()
    except OSError as exc:
        pytest.skip(f"这个环境不允许删文件/目录（{exc}）；删除类用例无法取证")
    if os.environ.get("FASTVIDEO_TEST_SKIP_ENV") == "1":
        # CI 与开发机走同一条规则：这几条靠真实文件系统取证，
        # 两边都跳过，基线的 passed 数才不会随 runner 漂。
        pytest.skip("按 FASTVIDEO_TEST_SKIP_ENV 跳过环境依赖用例")
