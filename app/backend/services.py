"""跨模块复用的业务动作。放在路由之外的唯一理由：有第二个调用方。

两条：删项目时把定妆图文件也清掉；标签归一化。项目中心（删单个项目）和
系统设置（删账号时连带删他名下的项目）都要走第一条；项目标签与资产标签都要走
第二条。写在某个 router 里让另一个 router 去 import 是反的（规格 02 §4.1）。
"""
from pathlib import Path

from config import FILES_DIR
from repositories.projects import projects as project_repo

MAX_TAGS = 10
MAX_TAG_LEN = 20


def clean_tags(raw: list[str]) -> list[str]:
    """去空白、去重、限长、限量。超出的直接截掉而不是报错——标签是辅助信息，
    不该挡住主流程。项目与资产共用这一条规则，两边口径不能分叉。"""
    seen: list[str] = []
    for item in raw:
        tag = item.strip()[:MAX_TAG_LEN]
        if tag and tag not in seen:
            seen.append(tag)
    return seen[:MAX_TAGS]


def delete_project_cascade(project_id: int) -> None:
    """删项目并清掉它名下的定妆图文件。

    先记下文件路径再删行：删完就查不到路径了，磁盘上的图会变成孤儿。
    路径校验是为了防止库里的脏数据把删除范围带出 FILE_DIR 之外。

    全局资产（project_id 为 NULL）天然不在清理范围内：`asset_images` 按
    `project_id = ?` 取，NULL 比不中。这正是"删项目不能删掉全局资产"
    （指令 §5.3）在文件这一侧的落点。
    """
    images = project_repo.asset_images(project_id)
    project_repo.delete(project_id)

    # 上传的图落在 FILES_DIR/<资产id>/ 下（content.py 上传接口），文件清完
    # 把空了的资产目录也收掉；目录里还有别的东西就留着，不越界。
    files_root = str(FILES_DIR.resolve())
    asset_dirs: set[Path] = set()
    for rel in images:
        target = (FILES_DIR / rel).resolve()
        if str(target).startswith(files_root) and target.is_file():
            target.unlink()
            asset_dirs.add(target.parent)
    for d in asset_dirs:
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()
