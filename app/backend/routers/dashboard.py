"""项目看板与素材导出（W16）。

看板只读聚合：集、镜、作业、成本、审核、音频都从各自权威表现算，
不存第二份"手工状态"。导出只给镜头清单与产物路径，不做时间线编辑。
"""
from fastapi import APIRouter, Depends, Response

import access
import services
from deps import require_permission

router = APIRouter(prefix="/api", tags=["dashboard"])


@router.get("/projects/{project_id}/dashboard")
def get_dashboard(project_id: int,
                  user=Depends(require_permission("project:read"))) -> dict:
    access.ensure(user, project_id, "read")
    return services.project_dashboard(project_id)


@router.get("/projects/{project_id}/export/shots.csv")
def export_shots_csv(project_id: int,
                     user=Depends(require_permission("project:read"))) -> Response:
    access.ensure(user, project_id, "read")
    text = services.shots_csv(project_id)
    return Response(text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="shots-{project_id}.csv"'})


@router.get("/projects/{project_id}/export/shots.md")
def export_shots_md(project_id: int,
                    user=Depends(require_permission("project:read"))) -> Response:
    project = access.ensure(user, project_id, "read")
    text = services.shots_markdown(project_id, project["name"])
    return Response(text, media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition":
                             f'attachment; filename="shots-{project_id}.md"'})
