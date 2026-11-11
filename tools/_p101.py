import pathlib
gate = pathlib.Path('tools/_gate_align.txt')
t = pathlib.Path('app/backend/tests/test_infra.py').read_text(encoding='utf-8')
add = gate.read_text(encoding='utf-8')
if 'test_表格里居中的数据格表头也要居中' not in t:
    pathlib.Path('app/backend/tests/test_infra.py').write_text(t + add, encoding='utf-8')
    print('gate appended')
else:
    print('gate already there')
