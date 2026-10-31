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
# 让 bootstrap 走"环境变量给了口令"这条分支，行为可预期；测试从不拿它登录
os.environ["FASTVIDEO_ADMIN_PASSWORD"] = "pytest-only-not-a-real-secret"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from repositories.content import assets, episodes, shots  # noqa: E402
from repositories.projects import projects as project_repo  # noqa: E402
from repositories.users import sessions, users as user_repo  # noqa: E402
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
        "superadmin": {"id": superadmin["id"], "token": _mint(superadmin["id"])},
        "admin": {"id": made["admin"]["id"], "token": _mint(made["admin"]["id"])},
        "editor": {"id": made["editor"]["id"], "token": _mint(made["editor"]["id"])},
        "viewer": {"id": made["viewer"]["id"], "token": _mint(made["viewer"]["id"])},
        "outsider": {"id": outsider["id"], "token": _mint(outsider["id"])},
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
    """一套真实对象：项目 → 集 → 镜 → 资产，所有人是 editor。

    一律走仓储层建，不写裸 SQL——不然测试替代码兜了底，真接口坏了还不知道。
    """
    pid = project_repo.create(f"测试项目_{uuid.uuid4().hex[:6]}", "介绍", ["标签A"],
                              people["editor"]["id"])["id"]
    eid = episodes.create(pid, "第 1 集")["id"]
    sid = shots.create(eid, "开场", "", 5.0, [])["id"]
    aid = assets.create(pid, "character", f"角色_{uuid.uuid4().hex[:6]}", "", "", [])["id"]
    return {"project_id": pid, "episode_id": eid, "shot_id": sid, "asset_id": aid}


@pytest.fixture
def fresh_project(app, people):
    """函数级：给调用方一个自己的新项目，避免互相踩。"""
    def _make(owner_role: str = "editor") -> int:
        return project_repo.create(f"临时_{uuid.uuid4().hex[:8]}", "", [],
                                   people[owner_role]["id"])["id"]
    return _make
