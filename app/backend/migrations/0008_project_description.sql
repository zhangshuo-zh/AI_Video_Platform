-- 项目介绍：项目管理里的「修改」弹窗要能写一段说明。
--
-- 这里能直接 ADD COLUMN，而 0007 必须重建整表，区别在默认值：
-- SQLite 禁止的是"新列默认值非常量"（0007 那一列是 datetime('now','localtime')，
-- 属于表达式）。这里的默认值就是空串，恒定，所以一行 ALTER 就够。
ALTER TABLE project ADD COLUMN description TEXT NOT NULL DEFAULT '';
