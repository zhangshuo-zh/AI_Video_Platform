import pathlib
p = pathlib.Path('app/frontend/js/views/models/engines.js')
t = p.read_text(encoding='utf-8')
old = '        <th>引擎实例</th><th>端点</th><th>状态</th><th>操作</th>'
new = '        <th>引擎实例</th><th>端点</th><th>状态</th><th class="c">操作</th>'
assert t.count(old) == 1, t.count(old)
p.write_text(t.replace(old, new), encoding='utf-8')
print('th aligned with td.c')
