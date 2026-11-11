# -*- coding: utf-8 -*-
"""F-04/F-07 反向对照：把刚补的三处逐个退回，看验收是不是真会变红。

一次只退一处，跑同一套探针；每处都必须有相关的红条，
否则说明探针没测到这一处。备份放在工作区内，finally 一定还原。
"""
import pathlib, shutil, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = ROOT / 'app' / 'backend' / '.venv' / 'bin' / 'python'

MON = ROOT / 'app' / 'frontend' / 'js' / 'views' / 'models' / 'monitor.js'
NOT = ROOT / 'app' / 'frontend' / 'js' / 'notifications.js'
APP = ROOT / 'app' / 'frontend' / 'js' / 'app.js'

ADD_VIS = "document.addEventListener('visibilitychange', visHandler);"
REVERTS = [
    ('F-04 监控回前台补一拍', MON, ADD_VIS, '// REVERT: no catch-up'),
    ('F-07 通知回前台补一拍', NOT, ADD_VIS, '// REVERT: no catch-up'),
    ('F-07 过期停轮询', APP, 'stopNotifications();', '// REVERT: keep polling'),
]

def probe():
    r = subprocess.run([str(PY), str(ROOT / 'tools' / '_verify_poll2.py')],
                       cwd=str(ROOT), capture_output=True, text=True)
    return r.returncode, r.stdout

def main():
    backups = {}
    for _, f, old, new in REVERTS:
        b = pathlib.Path(str(f) + '.revbak')
        assert not b.exists(), '上次没还原干净: ' + str(b)
        shutil.copy2(f, b)
        backups[f] = b
    caught = 0
    try:
        for name, f, old, new in REVERTS:
            t = backups[f].read_text(encoding='utf-8')
            assert t.count(old) == 1, (name, t.count(old))
            f.write_text(t.replace(old, new), encoding='utf-8')
            assert new in f.read_text(encoding='utf-8'), '回退没落盘: ' + name
            code, out = probe()
            fails = [l.strip()[5:].strip() for l in out.splitlines() if l.startswith('  FAIL ')]
            red = code != 0
            caught += red
            print('--- 退回 ' + name + ' → ' + ('RED' if red else 'GREEN(探针无效!)'), flush=True)
            for l in fails:
                print('      红: ' + l[:110], flush=True)
            shutil.copy2(backups[f], f)
    finally:
        for f, b in backups.items():
            shutil.copy2(b, f)
            b.unlink()
        for f in backups:
            assert 'REVERT' not in f.read_text(encoding='utf-8'), f
        print('全部还原', flush=True)
    code, out = probe()
    print('RESTORED ' + ('GREEN' if code == 0 else 'RED'), flush=True)
    tail = [l for l in out.splitlines() if 'PASS' in l][-1:]
    print(str(tail), flush=True)
    print('=== %d/%d 回退被抓 ===' % (caught, len(REVERTS)), flush=True)
    return 0 if (code == 0 and caught == len(REVERTS)) else 1

if __name__ == '__main__':
    sys.exit(main())
