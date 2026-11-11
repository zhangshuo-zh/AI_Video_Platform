# -*- coding: utf-8 -*-
'''反向对照：把「就地刷新」退回「关掉弹窗」，验收必须变红。'''
import pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = ROOT / 'app' / 'backend' / '.venv' / 'bin' / 'python'
F = ROOT / 'app' / 'frontend' / 'js' / 'views' / 'models' / 'engines.js'

MUTANTS = [
    ('A 启动完直接关掉弹窗（回退成原来的行为）',
     '        try {\n          const fresh = await api.engines();\n          paintWith(fresh);\n        } catch {\n          paint();     // 取不到新数据就至少把结论行画出来\n        }',
     '        close();'),
    ('B 不重画（只留结论行、表格保持旧状态）',
     '          paintWith(fresh);',
     '          /* 不重画 */;'),
]

def probe():
    r = subprocess.run([str(PY), str(ROOT / 'tools' / '_verify_inplace.py')],
                       cwd=str(ROOT), capture_output=True, text=True)
    return r.returncode, r.stdout

def main():
    orig = F.read_bytes()
    text0 = orig.decode('utf-8')
    caught = 0
    try:
        code, out = probe()
        print('BASELINE ' + ('GREEN' if code == 0 else 'RED'))
        assert code == 0, out[-1500:]
        for name, old, new in MUTANTS:
            n = text0.count(old)
            if n != 1:
                print('  SKIP(锚点 %d) %s' % (n, name))
                continue
            F.write_text(text0.replace(old, new), encoding='utf-8')
            code, out = probe()
            fails = [l.strip()[5:].strip() for l in out.splitlines() if l.startswith('  FAIL ')]
            red = code != 0
            caught += red
            print(('--- RED  ' if red else '--- GREEN(探针无效!) ') + name, flush=True)
            for l in fails[:2]:
                print('      红: ' + l[:90], flush=True)
            F.write_bytes(orig)
    finally:
        F.write_bytes(orig)
        code, out = probe()
        print('RESTORED ' + ('GREEN' if code == 0 else 'RED') + ' '
              + str([l for l in out.splitlines() if 'PASS' in l][-1:]), flush=True)
    print('=== %d/%d 回退被抓 ===' % (caught, len(MUTANTS)))
    return 0 if caught == len(MUTANTS) else 1

if __name__ == '__main__':
    sys.exit(main())
