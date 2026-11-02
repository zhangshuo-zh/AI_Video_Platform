-- 0024：撤掉两个没有接口引用的权限点（`setting:read`、`setting:write`）
--
-- 0002 一开始就把「设定」这一组 seed 进来了，因为当时规划里有一个设定中心。
-- 到现在（v0.86）设定中心仍然是空态页：没有表、没有接口、没有写路径。
-- 于是这两个代码变成"勾了不做事"的选项——角色编辑器里能勾、勾完什么都不会多，
-- 而《需求概要说明书》§2.3 一直挂着"尚未实现"四个字。
--
-- 为什么选撤而不是留着等接口：留着要付出的是**误信成本**——
-- 管理员会以为"给只读账号勾上设定＝他能看设定"，而这个判断今天不成立。
-- 等设定中心真立项时再加回来是一条普通迁移，届时权限点、接口、界面一起出现，
-- 比现在留两个空位更省事。
--
-- 为什么 `role_permission` 要显式删：migrate.py 在迁移期间关掉外键
-- （重建表时开着外键会失败），所以 0002 里那句 `ON DELETE CASCADE` 在这一趟不会触发。
-- 先删关联再删权限点，顺序反了就会留下指向不存在权限的行——
-- 迁移末尾的 `PRAGMA foreign_key_check` 会直接拒收整个迁移。

DELETE FROM role_permission
 WHERE permission_id IN (SELECT id FROM permission
                          WHERE code IN ('setting:read', 'setting:write'));

DELETE FROM permission
 WHERE code IN ('setting:read', 'setting:write');
