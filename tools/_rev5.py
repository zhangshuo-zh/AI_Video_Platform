# -*- coding: utf-8 -*-
"""F-05 反向对照：把键盘可达性逐个拆掉，看 12 项验收是不是真会变红。"""
import pathlib, shutil, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
NOT = ROOT / 'app' / 'frontend' / 'js' / 'notifications.js'
PY = ROOT / 'app' / 'backend' / '.venv' / 'bin' / 'python'

REVERTS = [
    ('打开时不送焦点', '      menuItems()[0]?.focus();', '      // REVERT'),
    ('方向键不处理', '    if (!step) return;', '    return;  // REVERT'),
    ('Home/End 不处理', "    if (e.key === 'Home' || e.key === 'End') {", '    if (false) {  // REVERT'),
    ('Esc 不关菜单', "    if (e.key === 'Escape' && !menu.hidden) {", '    if (false) {  // REVERT'),
    ('条目不是 menuitem', 'role="menuitem" class="bell-item', 'class="bell-item'),
]

def probe():
    r = subprocess.run([str(PY), str(ROOT / 'tools' / '_verify_bell5.py')],
                       cwd=str(ROOT), capture_output=True, text=True)
    return r.returncode, r.stdout

def main():
    bak = pathlib.Path(str(NOT) + '.revbak')
    assert not bak.exists(), '上次没还原干净'
    shutil.copy2(NOT, bak)
    orig = bak.read_text(encoding='utf-8')
    caught = 0
    try:
        for name, old, new in REVERTS:
            assert orig.count(old) == 1, ('锚点不唯一: ' + name, orig.count(old))
            NOT.write_text(orig.replace(old, new), encoding='utf-8')
            assert new in NOT.read_text(encoding='utf-8'), '没落盘: ' + name
            code, out = probe()
            fails = [l.strip()[5:].strip() for l in out.splitlines() if l.startswith('  FAIL ')]
            red = code != 0
            caught += red
            print('--- 退回 ' + name + ' → ' + ('RED' if red else 'GREEN(探针无效!)'), flush=True)
            for l in fails:
                print('      红: ' + l[:90], flush=True)
            NOT.write_text(orig, encoding='utf-8')
    finally:
        shutil.copy2(bak, NOT)
        bak.unlink()
        assert 'REVERT' not in NOT.read_text(encoding='utf-8')
        print('已还原', flush=True)
    code, out = probe()
    tail = [l for l in out.splitlines() if 'PASS' in l][-1:]
    print('RESTORED ' + ('GREEN ' + str(tail) if code == 0 else 'RED'), flush=True)
    print('=== %d/%d 回退被抓 ===' % (caught, len(REVERTS)), flush=True)
    return 0 if (code == 0 and caught == len(REVERTS)) else 1

if __name__ == '__main__':
    sys.exit(main())
