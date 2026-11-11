# -*- coding: utf-8 -*-
'''启动按钮的反向对照：拆掉关键行为，看 17 项验收是否真会红。'''
import pathlib, subprocess, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PY = ROOT / 'app' / 'backend' / '.venv' / 'bin' / 'python'
MC = ROOT / 'app' / 'backend' / 'routers' / 'model_center.py'
EL = ROOT / 'app' / 'backend' / 'engine_launch.py'

READY = '                and status.state != "ok"),'

MUTANTS = [
    ('A 永不返回 launch_ready（按钮不出现）', MC, READY, '                and False),'),
    ('B 在跑时也给 launch_ready（第二个入口）', MC, READY, '                and True),'),
    ('C 起了就报 reachable（把命令发出当能用了）', EL,
     '    return LaunchResult(False, engine_code, True, False,',
     '    return LaunchResult(True, engine_code, True, True,'),
    ('D 去掉「已在跑就不重复启动」', EL,
     '    # 第 3 条边界：已经在跑就别再起一个。\n    if _already_answering(engine_code, endpoint):',
     '    # 第 3 条边界：已经在跑就别再起一个。\n    if False:'),
]


def probe():
    r = subprocess.run([str(PY), str(ROOT / 'tools' / '_verify_launch.py')],
                       cwd=str(ROOT), capture_output=True, text=True)
    return r.returncode, r.stdout


def main():
    originals = {f: f.read_bytes() for f in (MC, EL)}
    caught = 0
    try:
        code, out = probe()
        print('BASELINE ' + ('GREEN' if code == 0 else 'RED'))
        assert code == 0, out[-1500:]
        for name, f, old, new in MUTANTS:
            text = originals[f].decode('utf-8')
            if text.count(old) != 1:
                print('  SKIP(锚点 %d) ' % text.count(old) + name)
                continue
            f.write_text(text.replace(old, new), encoding='utf-8')
            code, out = probe()
            fails = [l.strip()[5:].strip() for l in out.splitlines() if l.startswith('  FAIL ')]
            red = code != 0
            caught += red
            print(('--- RED  ' if red else '--- GREEN(漏!) ') + name, flush=True)
            for l in fails[:2]:
                print('      红: ' + l[:95], flush=True)
            f.write_bytes(originals[f])
    finally:
        for f, data in originals.items():
            f.write_bytes(data)
        code, out = probe()
        print('RESTORED ' + ('GREEN' if code == 0 else 'RED') + ' '
              + str([l for l in out.splitlines() if 'PASS' in l][-1:]), flush=True)
        assert code == 0, out[-1500:]
    print('=== %d/%d 回退被抓 ===' % (caught, len(MUTANTS)))
    return 0 if caught == len(MUTANTS) else 1


if __name__ == '__main__':
    sys.exit(main())
