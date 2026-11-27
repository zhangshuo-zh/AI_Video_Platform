"""设定中心业务逻辑（M4）：项目画风词缀与剧本/分镜一致性扫描。"""
from __future__ import annotations

import json
import re
from typing import Any

from repositories.audio import clips
from repositories.content import assets, episodes, profiles, shots, variants
from repositories.documents import documents as docs
from repositories.knowledge import knowledge
from repositories.projects import projects
from workspace import PathError

from .documents import read_content
from .jobs import shot_asset_ids

_DIALOGUE_LINE_RE = re.compile(r"^\s*([^\s：:#*\[\]<>0-9/\\]{2,12})：\s*\S+")


def _json_list(raw: object) -> list[Any]:
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(str(raw or "[]"))
    except ValueError:
        return []
    return parsed if isinstance(parsed, list) else []


def affix_view(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "project_id": int(row["project_id"]),
        "name": str(row["name"]),
        "category": str(row["category"]),
        "positive": str(row["positive"] or ""),
        "negative": str(row["negative"] or ""),
        "is_default": bool(row["is_default"]),
        "sort_order": int(row["sort_order"] or 0),
        "created_by": int(row["created_by"]) if row["created_by"] is not None else None,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def issue_view(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "project_id": int(row["project_id"]),
        "issue_key": str(row["issue_key"]),
        "category": str(row["category"]),
        "severity": str(row["severity"]),
        "title": str(row["title"]),
        "description": str(row["description"] or ""),
        "source_type": str(row["source_type"] or ""),
        "source_id": int(row["source_id"]) if row["source_id"] is not None else None,
        "suggestion": str(row["suggestion"] or ""),
        "status": str(row["status"]),
        "resolved_by": int(row["resolved_by"]) if row["resolved_by"] is not None else None,
        "resolved_by_name": str(row["resolved_by_name"] or ""),
        "resolved_at": str(row["resolved_at"]) if row["resolved_at"] is not None else None,
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def _collect_alias_conflicts(project_assets: list[Any], findings: list[dict[str, Any]]) -> None:
    seen: dict[str, list[Any]] = {}
    for asset in project_assets:
        names = [str(asset["name"] or "").strip()]
        names.extend(str(a).strip() for a in _json_list(asset["aliases"]))
        for raw_name in names:
            key = raw_name.lower()
            if len(key) < 2:
                continue
            bucket = seen.setdefault(key, [])
            if all(int(a["id"]) != int(asset["id"]) for a in bucket):
                bucket.append(asset)
    for key, group in seen.items():
        if len(group) < 2:
            continue
        names_label = "、".join(f"{a['name']}(#{a['id']})" for a in group)
        findings.append({
            "issue_key": f"alias_conflict:{key}",
            "category": "character_conflict",
            "severity": "error",
            "title": f"资产名称或别名冲突：「{key}」",
            "description": f"多个资产共用了相同的名称或别名：{names_label}，自动匹配时可能绑定错对象。",
            "source_type": "asset",
            "source_id": int(group[0]["id"]),
            "suggestion": "进入资产详情清理重复别名，确保每个角色/场景标识唯一。",
        })


def _collect_asset_ref_issues(
    project_assets: list[Any],
    referenced_ids: set[int],
    findings: list[dict[str, Any]],
) -> None:
    for asset in project_assets:
        aid = int(asset["id"])
        if aid not in referenced_ids or asset["project_id"] is None:
            continue
        refs = _json_list(asset["ref_images"])
        prompt = str(asset["consistency_prompt"] or "").strip()
        if not refs and not prompt:
            findings.append({
                "issue_key": f"asset_no_ref:{aid}",
                "category": "asset_missing_ref",
                "severity": "warning",
                "title": f"核心资产「{asset['name']}」缺少参考图与设定提示词",
                "description": "该资产已被分镜引用，但尚未上传参考图或填写一致性提示词，多镜生成容易画风漂移。",
                "source_type": "asset",
                "source_id": aid,
                "suggestion": f"前往资产「{asset['name']}」详情页上传定妆/参考图或补充一致性提示词。",
            })


def _collect_shot_issues(
    project_shots: list[Any],
    project_assets: list[Any],
    findings: list[dict[str, Any]],
) -> set[int]:
    referenced_ids: set[int] = set()
    searchable_assets: list[tuple[Any, list[str]]] = []
    for asset in project_assets:
        terms = [str(asset["name"] or "").strip()]
        terms.extend(str(a).strip() for a in _json_list(asset["aliases"]))
        valid_terms = [t for t in terms if len(t) >= 2]
        if valid_terms:
            searchable_assets.append((asset, valid_terms))

    for shot in project_shots:
        sid = int(shot["id"])
        ep_seq = int(shot["episode_seq"])
        s_seq = int(shot["seq"])
        linked = set(shot_asset_ids(shot))
        referenced_ids.update(linked)
        summary = str(shot["summary"] or "").strip()
        prompt = str(shot["prompt"] or "").strip()

        if summary and not prompt:
            findings.append({
                "issue_key": f"shot_empty_prompt:{sid}",
                "category": "continuity",
                "severity": "warning",
                "title": f"S{ep_seq:02d}E{s_seq:02d} 缺少画面提示词",
                "description": f"镜头「{summary[:40]}」仅有剧情简介而未填写生图提示词，批量出图将缺少画面约束。",
                "source_type": "shot",
                "source_id": sid,
                "suggestion": "在分镜工作台或创作中心补充该镜视觉提示词（或使用画风词缀辅助生成）。",
            })

        haystack = f"{summary}\n{prompt}"
        for asset, terms in searchable_assets:
            aid = int(asset["id"])
            if aid in linked:
                continue
            matched = next((t for t in terms if t in haystack), None)
            if not matched:
                continue
            category = "dialogue_speaker" if asset["kind"] == "character" else "prop_mismatch"
            findings.append({
                "issue_key": f"shot_missing_asset:{sid}:{aid}",
                "category": category,
                "severity": "warning",
                "title": f"S{ep_seq:02d}E{s_seq:02d} 提及「{matched}」但未绑定资产",
                "description": f"第 {ep_seq} 集第 {s_seq} 镜文案中出现了资产「{asset['name']}」，但资产引用列表中尚未绑定该资产。",
                "source_type": "shot",
                "source_id": sid,
                "suggestion": f"点击「采纳」自动将「{asset['name']}」(#{aid}) 绑定到本镜资产列表。",
            })
    return referenced_ids


def _collect_audio_issues(
    project_clips: list[Any],
    project_assets: list[Any],
    shot_map: dict[int, Any],
    findings: list[dict[str, Any]],
) -> None:
    char_by_term: dict[str, Any] = {}
    for asset in project_assets:
        if asset["kind"] != "character":
            continue
        terms = [str(asset["name"] or "").strip()]
        terms.extend(str(a).strip() for a in _json_list(asset["aliases"]))
        for term in terms:
            if term:
                char_by_term.setdefault(term.lower(), asset)

    for clip in project_clips:
        speaker = str(clip["speaker"] or "").strip()
        if not speaker:
            continue
        char = char_by_term.get(speaker.lower())
        if char is None:
            findings.append({
                "issue_key": f"clip_unknown_speaker:{clip['id']}",
                "category": "dialogue_speaker",
                "severity": "info",
                "title": f"对白说话人「{speaker}」未在角色库建档",
                "description": f"音频片段 #{clip['id']} 指定了说话人「{speaker}」，但项目可用角色资产中未找到匹配角色。",
                "source_type": "shot" if clip["shot_id"] else "",
                "source_id": int(clip["shot_id"]) if clip["shot_id"] else None,
                "suggestion": f"在资产中心新建角色「{speaker}」并设置音色，或在音频条目中修正说话人名称。",
            })
            continue
        sid = int(clip["shot_id"]) if clip["shot_id"] is not None else 0
        shot = shot_map.get(sid)
        if shot is not None and int(char["id"]) not in shot_asset_ids(shot):
            ep_seq = int(shot["episode_seq"])
            s_seq = int(shot["seq"])
            findings.append({
                "issue_key": f"shot_missing_asset:{sid}:{int(char['id'])}",
                "category": "dialogue_speaker",
                "severity": "warning",
                "title": f"S{ep_seq:02d}E{s_seq:02d} 对白角色「{speaker}」未绑定到分镜",
                "description": f"该镜挂载了说话人「{speaker}」的台词音频，但镜头尚未绑定对应角色资产「{char['name']}」。",
                "source_type": "shot",
                "source_id": sid,
                "suggestion": f"点击「采纳」自动将角色「{char['name']}」绑定到此镜。",
            })


def _collect_doc_issues(
    project_docs: list[Any],
    project_assets: list[Any],
    findings: list[dict[str, Any]],
) -> int:
    known_names: set[str] = set()
    for asset in project_assets:
        if asset["kind"] != "character":
            continue
        known_names.add(str(asset["name"] or "").strip().lower())
        for alias in _json_list(asset["aliases"]):
            a_str = str(alias).strip().lower()
            if a_str:
                known_names.add(a_str)

    scanned_docs = 0
    seen_speakers: set[tuple[int, str]] = set()
    for doc in project_docs:
        try:
            content = str(read_content(doc)["content"] or "")
        except (OSError, PathError):
            continue
        scanned_docs += 1
        if str(doc["kind"]) not in ("script", "outline"):
            continue
        for line in content.splitlines()[:400]:
            m = _DIALOGUE_LINE_RE.match(line)
            if not m:
                continue
            speaker = m.group(1).strip()
            low = speaker.lower()
            if not speaker or low in known_names:
                continue
            if speaker in ("场景", "时间", "地点", "旁白", "画外音", "备注", "提示", "镜号"):
                continue
            key = (int(doc["id"]), low)
            if key in seen_speakers:
                continue
            seen_speakers.add(key)
            findings.append({
                "issue_key": f"doc_unregistered_char:{doc['id']}:{low}",
                "category": "dialogue_speaker",
                "severity": "info",
                "title": f"文档《{doc['title']}》中出现未建档角色「{speaker}」",
                "description": f"文稿对白格式提到了角色「{speaker}」，但项目角色库中尚无同名角色或别名。",
                "source_type": "document",
                "source_id": int(doc["id"]),
                "suggestion": f"可先运行剧本抽取或在资产中心建立「{speaker}」角色设定。",
            })
    return scanned_docs


def scan_consistency(project_id: int) -> dict[str, Any]:
    """扫描项目资产、分镜、台词与文稿设定的一致性并持久化审阅列表。"""
    project_assets = assets.list_visible(project_id)
    project_shots = knowledge.list_shots_by_project(project_id)
    project_docs = docs.list_by_project(project_id)
    project_clips = clips.list(project_id)
    shot_map = {int(s["id"]): s for s in project_shots}

    findings: list[dict[str, Any]] = []
    _collect_alias_conflicts(project_assets, findings)
    referenced_ids = _collect_shot_issues(project_shots, project_assets, findings)
    _collect_asset_ref_issues(project_assets, referenced_ids, findings)
    _collect_audio_issues(project_clips, project_assets, shot_map, findings)
    scanned_docs = _collect_doc_issues(project_docs, project_assets, findings)

    # 去重 issue_key（例如分镜正文与音频台词同时发现同一缺失绑定）
    deduped: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for item in findings:
        key = str(item["issue_key"])
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(item)

    new_count, reopened_count = knowledge.upsert_scanned_issues(project_id, deduped)
    all_issues = [issue_view(r) for r in knowledge.list_issues(project_id)]
    open_count = sum(1 for it in all_issues if it["status"] == "open")
    return {
        "project_id": project_id,
        "scanned_shots": len(project_shots),
        "scanned_assets": len(project_assets),
        "scanned_docs": scanned_docs,
        "new_count": new_count,
        "reopened_count": reopened_count,
        "open_count": open_count,
        "issues": all_issues,
    }


def _apply_auto_fix_on_adopt(project_id: int, issue_row: Any) -> None:
    """对可安全自动修复的矛盾（如分镜漏绑资产），采纳时自动落库修正。"""
    key = str(issue_row["issue_key"] or "")
    if not key.startswith("shot_missing_asset:"):
        return
    parts = key.split(":")
    if len(parts) != 3:
        return
    try:
        shot_id = int(parts[1])
        asset_id = int(parts[2])
    except ValueError:
        return
    shot = shots.get(shot_id)
    if shot is None:
        return
    if asset_id not in assets.usable_ids(project_id, [asset_id]):
        return
    linked = sorted(shot_asset_ids(shot))
    if asset_id in linked:
        return
    linked.append(asset_id)
    variant_ids = [int(v) for v in _json_list(shot["variant_ids"]) if isinstance(v, (int, str)) and str(v).isdigit()]
    shots.update(
        shot_id,
        str(shot["summary"]),
        str(shot["prompt"]),
        float(shot["duration"]),
        linked,
        variant_ids,
        shot_size=str(shot["shot_size"] or ""),
        camera_move=str(shot["camera_move"] or ""),
        aspect=str(shot["aspect"] or ""),
        style=str(shot["style"] or ""),
        negative_prompt=str(shot["negative_prompt"] or ""),
        trim_in_ms=int(shot["trim_in_ms"] or 0),
        trim_out_ms=int(shot["trim_out_ms"] or 0),
        transition=str(shot["transition"] or "cut"),
        first_frame_image=str(shot["first_frame_image"] or ""),
        last_frame_image=str(shot["last_frame_image"] or ""),
    )


def resolve_consistency_issue(
    project_id: int,
    issue_row: Any,
    *,
    action: str,
    user_id: int | None,
) -> dict[str, Any]:
    if action == "adopt":
        _apply_auto_fix_on_adopt(project_id, issue_row)
        target_status = "adopted"
    elif action == "ignore":
        target_status = "ignored"
    else:
        target_status = "open"
    updated = knowledge.resolve_issue(
        int(issue_row["id"]),
        status=target_status,
        resolved_by=user_id,
    )
    assert updated is not None
    return issue_view(updated)


def build_notebooklm_bundle(project_id: int) -> tuple[str, str]:
    """将项目设定、角色档案、画风词缀、分集分镜与剧本文档聚合为单份 Markdown。

    面向 NotebookLM Pro 等长上下文知识库工具：下载后直接拖入即可做防吃书校验、
    人物关系核对与剧集复盘。纯只读，不修改任何库状态。
    """
    proj = projects.get(project_id)
    proj_name = str(proj["name"]) if proj is not None else f"项目 #{project_id}"
    proj_desc = str(proj["description"] or "").strip() if proj is not None else ""

    lines: list[str] = [
        f"# 《{proj_name}》设定与剧本知识包（NotebookLM Pro 防吃书版）",
        "",
        "> 本文件由 AI 视频生产平台自动聚合生成，包含项目角色/场景/道具设定、人物档案、全局画风词缀、分集分镜大纲与项目剧本文档，可直接上传至 NotebookLM Pro 作为项目防吃书与剧情核对知识源。",
        "",
    ]
    if proj_desc:
        lines.extend(["## 项目简介", "", proj_desc, ""])

    # 1. 全局画风词缀
    affix_rows = knowledge.list_affixes(project_id)
    lines.extend(["## 一、全局画风词缀库", ""])
    if not affix_rows:
        lines.append("（暂无画风词缀）")
    else:
        for row in affix_rows:
            flag = "（默认注入）" if row["is_default"] else ""
            lines.append(f"- **{row['name']}** [{row['category']}]{flag}")
            if row["positive"]:
                lines.append(f"  - 正向：{row['positive']}")
            if row["negative"]:
                lines.append(f"  - 负向：{row['negative']}")
    lines.append("")

    # 2. 资产与人物档案
    visible_assets = assets.list_visible(project_id)
    kind_titles = [("character", "角色设定"), ("scene", "场景设定"), ("prop", "道具设定")]
    lines.extend(["## 二、核心资产与人物档案", ""])
    if not visible_assets:
        lines.append("（暂无资产设定）")
        lines.append("")
    else:
        for kind, title in kind_titles:
            group = [a for a in visible_assets if str(a["kind"]) == kind]
            if not group:
                continue
            lines.extend([f"### {title}（{len(group)}）", ""])
            for a in group:
                aid = int(a["id"])
                scope_label = "全局池" if a["project_id"] is None else "本项目"
                aliases = [str(x).strip() for x in _json_list(a["aliases"]) if str(x).strip()]
                tags = [str(x).strip() for x in _json_list(a["tags"]) if str(x).strip()]
                lines.append(f"#### {a['name']}（{scope_label} · #{aid}）")
                if aliases:
                    lines.append(f"- **别名**：{'、'.join(aliases)}")
                if tags:
                    lines.append(f"- **标签**：{'、'.join(tags)}")
                if a["summary"]:
                    lines.append(f"- **简介**：{str(a['summary']).strip()}")
                if a["consistency_prompt"]:
                    lines.append(f"- **外形一致性提示词**：{str(a['consistency_prompt']).strip()}")
                if kind == "character":
                    prof = profiles.get(aid)
                    if prof and prof.get("exists"):
                        for label, key in (
                            ("人物履历", "biography"),
                            ("性格特征", "personality"),
                            ("成长弧光", "growth_arc"),
                            ("文字写作提示词", "text_prompt"),
                        ):
                            val = str(prof.get(key) or "").strip()
                            if val:
                                lines.append(f"- **{label}**：{val}")
                        if prof.get("tts_voice") or prof.get("tts_mood"):
                            lines.append(
                                f"- **配音音色**：声线={prof.get('tts_voice') or '默认'}，"
                                f"语速={prof.get('tts_rate', 1.0)}x，情绪={prof.get('tts_mood') or '默认'}"
                            )
                var_rows = variants.list_by_asset(aid)
                if var_rows:
                    var_names = [f"{v['name']}({v['variant_type']})" for v in var_rows]
                    lines.append(f"- **命名形态**：{'、'.join(var_names)}")
                lines.append("")

    # 3. 分集与分镜大纲
    ep_rows = episodes.list_by_project(project_id)
    lines.extend(["## 三、分集与分镜大纲", ""])
    if not ep_rows:
        lines.append("（暂无分集）")
        lines.append("")
    else:
        for ep in ep_rows:
            ep_id = int(ep["id"])
            lines.append(f"### 第 {ep['seq']} 集：{ep['title']}（状态：{ep['status']}）")
            ep_shots = shots.list_by_episode(ep_id)
            if not ep_shots:
                lines.append("（本集暂无分镜）")
            else:
                for sh in ep_shots:
                    meta = [f"{sh['duration']}s"]
                    if sh["shot_size"]:
                        meta.append(str(sh["shot_size"]))
                    if sh["camera_move"]:
                        meta.append(str(sh["camera_move"]))
                    lines.append(f"- **镜 {sh['seq']}**（{' / '.join(meta)}）：{str(sh['summary'] or '').strip()}")
                    if sh["prompt"]:
                        lines.append(f"  - 画面提示词：{str(sh['prompt']).strip()}")
            lines.append("")

    # 4. 剧本文档正文
    doc_rows = docs.list_by_project(project_id)
    lines.extend(["## 四、项目剧本与设定文档", ""])
    if not doc_rows:
        lines.append("（暂无文档）")
        lines.append("")
    else:
        for doc in doc_rows:
            lines.append(f"### {doc['title']}（类型：{doc['kind']} · 状态：{doc['status']}）")
            lines.append("")
            try:
                content = str(read_content(doc).get("content") or "").strip()
            except (OSError, PathError, ValueError):
                content = ""
            lines.append(content or "（正文为空或文件尚未写入）")
            lines.append("")

    filename = f"project-{project_id}-notebooklm.md"
    return filename, "\n".join(lines).strip() + "\n"
