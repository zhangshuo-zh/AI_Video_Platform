"""音频素材（W15）。SQL 只在这一层。"""
from __future__ import annotations

import json
import sqlite3

from db import get_conn
from repositories.projects import touch

_SELECT = """
    SELECT a.*, COALESCE(e.seq, 0) AS episode_seq,
           COALESCE(e.title, '') AS episode_title,
           COALESCE(s.seq, 0) AS shot_seq,
           j.status AS job_status, j.error_message AS job_error
      FROM audio_clip a
      LEFT JOIN episode e ON e.id = a.episode_id
      LEFT JOIN shot s ON s.id = a.shot_id
      LEFT JOIN job j ON j.id = a.job_id
"""


class AudioRepository:
    def list(self, project_id: int, kind: str | None = None,
             episode_id: int | None = None) -> list[sqlite3.Row]:
        clauses = ["a.project_id = ?"]
        args: list = [project_id]
        if kind:
            clauses.append("a.kind = ?")
            args.append(kind)
        if episode_id:
            clauses.append("a.episode_id = ?")
            args.append(episode_id)
        with get_conn() as conn:
            return conn.execute(
                _SELECT + " WHERE " + " AND ".join(clauses) +
                " ORDER BY a.kind, a.id DESC", args).fetchall()

    def get(self, clip_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            return conn.execute(_SELECT + " WHERE a.id = ?", (clip_id,)).fetchone()

    def create(self, *, project_id: int, episode_id: int | None,
               shot_id: int | None, kind: str, speaker: str, text: str,
               created_by: int | None) -> sqlite3.Row:
        with get_conn() as conn:
            cursor = conn.execute(
                """INSERT INTO audio_clip
                   (project_id, episode_id, shot_id, kind, speaker, text, created_by)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (project_id, episode_id, shot_id, kind, speaker, text, created_by))
            touch(conn, "project", project_id)
            return conn.execute(_SELECT + " WHERE a.id = ?",
                                (cursor.lastrowid,)).fetchone()

    def update(self, clip_id: int, *, kind: str, speaker: str, text: str,
               notes: str) -> sqlite3.Row | None:
        with get_conn() as conn:
            row = conn.execute("SELECT project_id FROM audio_clip WHERE id = ?",
                               (clip_id,)).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE audio_clip
                      SET kind = ?, speaker = ?, text = ?, notes = ?,
                          updated_at = datetime('now','localtime')
                    WHERE id = ?""",
                (kind, speaker, text, notes, clip_id))
            touch(conn, "project", row["project_id"])
            return conn.execute(_SELECT + " WHERE a.id = ?", (clip_id,)).fetchone()

    def set_job(self, clip_id: int, job_id: int) -> sqlite3.Row | None:
        with get_conn() as conn:
            row = conn.execute("SELECT project_id FROM audio_clip WHERE id = ?",
                               (clip_id,)).fetchone()
            if row is None:
                return None
            conn.execute(
                """UPDATE audio_clip
                      SET status = 'queued', job_id = ?,
                          updated_at = datetime('now','localtime')
                    WHERE id = ?""", (job_id, clip_id))
            return conn.execute(_SELECT + " WHERE a.id = ?", (clip_id,)).fetchone()

    def finish(self, clip_id: int, *, status: str, path: str | None,
               duration_ms: int | None, timestamps: list | None = None,
               visemes: list | None = None) -> None:
        with get_conn() as conn:
            conn.execute(
                """UPDATE audio_clip
                      SET status = ?, path = ?, duration_ms = ?,
                          timestamps_json = COALESCE(?, timestamps_json),
                          viseme_json = COALESCE(?, viseme_json),
                          updated_at = datetime('now','localtime')
                    WHERE id = ?""",
                (status, path, duration_ms,
                 json.dumps(timestamps, ensure_ascii=False) if timestamps is not None else None,
                 json.dumps(visemes, ensure_ascii=False) if visemes is not None else None,
                 clip_id))

    def delete(self, clip_id: int) -> bool:
        with get_conn() as conn:
            row = conn.execute("SELECT project_id FROM audio_clip WHERE id = ?",
                               (clip_id,)).fetchone()
            if row is None:
                return False
            conn.execute("DELETE FROM audio_clip WHERE id = ?", (clip_id,))
            touch(conn, "project", row["project_id"])
            return True


clips = AudioRepository()
