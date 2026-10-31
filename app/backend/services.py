"""跨模块复用的业务动作。放在路由之外的唯一理由：有第二个调用方。

现在只有一条——删项目时把定妆图文件也清掉。项目中心（删单个项目）和
系统设置（删账号时连带删他名下的项目）都要走它，写在某个 router 里
让另一个 router 去 import 是反的。
"""
from pathlib import Path

from config import FILES_DIR
from repositories.projects import projects as project_repo


def delete_project_cascade(project_id: int) -> None:
    """删项目并清掉它名下的定妆图文件。

    先记下文件路径再删行：删完就查不到路径了，磁盘上的图会变成孤儿。
    路径校验是为了防止库里的脏数据把删除范围带出 FILE_DIR 之外。
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
