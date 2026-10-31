-- 超级用户改名 superadmin，并挂上全部角色。
--
-- 为什么用迁移而不是一次性脚本：改名这件事对新库也要成立。
-- 新库跑到这一步时还没有任何用户，下面两句都是空操作，
-- 随后 bootstrap 会按新的默认用户名建号。
--
-- 先判 NOT EXISTS：username 上有 UNIQUE 约束，
-- 万一已经手工建过 superadmin，这句不能把整个迁移炸掉。
UPDATE app_user
   SET username = 'superadmin'
 WHERE is_superuser = 1
   AND username = 'admin'
   AND NOT EXISTS (SELECT 1 FROM app_user WHERE username = 'superadmin');

-- 权限判定走 is_superuser 本来就不受角色限制；挂上全部角色是为了
-- "角色"那几列能如实显示它属于哪几组（用户明确要求"赋予所有角色"）。
INSERT OR IGNORE INTO user_role (user_id, role_id)
SELECT u.id, r.id FROM app_user u CROSS JOIN role r WHERE u.is_superuser = 1;
