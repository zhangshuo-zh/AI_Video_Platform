import io
import json
import pytest
from PIL import Image
from config import FILES_DIR
from db import transaction
from services import asset_upscale as up

@pytest.fixture
def setup_asset(as_role, fresh_project, monkeypatch):
    client = as_role('admin')
    aid = client.post(f'/api/projects/{fresh_project()}/assets', json={'kind': 'character', 'name': '超分测试'}).json()['id']
    folder = FILES_DIR / str(aid)
    folder.mkdir(parents=True, exist_ok=True)
    for name in ('cover.png', 'other.png'):
        Image.new('RGB', (4, 3)).save(folder / name)
    old = f'{aid}/cover.png'
    with transaction() as conn:
        conn.execute('UPDATE asset SET ref_images=?, image_purposes=? WHERE id=?', (json.dumps([old, f'{aid}/other.png']), json.dumps({old: '封面'}), aid))
    monkeypatch.setattr(up.engine_instances, 'pick', lambda code: {'endpoint': 'http://127.0.0.1:8188'})
    return client, aid, old

def mock_comfy(monkeypatch, image):
    monkeypatch.setattr(up, '_upload', lambda *args: 'input.png')
    monkeypatch.setattr(up, '_submit', lambda *args: 'task')
    monkeypatch.setattr(up, '_wait', lambda *args: {'filename': 'output.png'})
    monkeypatch.setattr(up, '_request', lambda *args, **kwargs: image)

def png():
    stream = io.BytesIO()
    Image.new('RGB', (16, 12)).save(stream, format='PNG')
    return stream.getvalue()

@pytest.mark.parametrize('replace', [True, False])
def test_upscale(setup_asset, monkeypatch, replace):
    client, aid, old = setup_asset
    mock_comfy(monkeypatch, png())
    response = client.post(f'/api/assets/{aid}/images/upscale', json={'path': old, 'replace': replace})
    assert response.status_code == 200, response.text
    out = response.json()
    new = out['new_path']
    assert (out['width'], out['height'], out['replaced']) == (16, 12, replace)
    assert (FILES_DIR / new).is_file()
    assert (FILES_DIR / old).exists() is not replace
    assert out['asset']['ref_images'] == ([new, f'{aid}/other.png'] if replace else [old, f'{aid}/other.png', new])
    assert out['asset']['image_purposes'] == ({new: '封面'} if replace else {old: '封面'})

def test_validation(setup_asset, monkeypatch):
    client, aid, old = setup_asset
    url = f'/api/assets/{aid}/images/upscale'
    assert client.post(url, json={'path': 'wrong.png'}).status_code == 404
    assert client.post(url, json={'path': old, 'model_name': '../evil.pth'}).status_code == 422
    monkeypatch.setattr(up.engine_instances, 'pick', lambda code: None)
    assert client.post(url, json={'path': old}).status_code == 503

original_request = up._request

@pytest.mark.parametrize('data', [b'not an image', b'x' * 100])
def test_bad_output_does_not_change_asset(setup_asset, monkeypatch, data):
    client, aid, old = setup_asset
    mock_comfy(monkeypatch, data)
    if len(data) == 100:
        monkeypatch.setattr(up, 'MAX_IMAGE_BYTES', 50)
        monkeypatch.setattr(up, '_request', original_request)
        monkeypatch.setattr(up.safenet, 'safe_urlopen', lambda *a, **kw: io.BytesIO(data))
    response = client.post(f'/api/assets/{aid}/images/upscale', json={'path': old})
    assert response.status_code == 502
    asset = client.get(f'/api/assets/{aid}').json()
    assert asset['ref_images'] == [old, f'{aid}/other.png']
    assert (FILES_DIR / old).is_file()

def test_semaphore_limit_and_release(setup_asset, monkeypatch):
    client, aid, old = setup_asset
    mock_comfy(monkeypatch, png())
    class Semaphore:
        allowed = False
        released = 0
        def acquire(self, timeout):
            assert timeout == 2.0
            return self.allowed
        def release(self):
            self.released += 1
    sem = Semaphore()
    monkeypatch.setattr(up, '_SEMAPHORE', sem)
    url = f'/api/assets/{aid}/images/upscale'
    assert client.post(url, json={'path': old}).status_code == 429
    assert sem.released == 0
    sem.allowed = True
    assert client.post(url, json={'path': old}).status_code == 200
    assert sem.released == 1

def test_comfy_protocol(monkeypatch, tmp_path):
    calls = []
    def fake_request(url, deadline, data=None, content_type=None, limit=0):
        calls.append((url, data, content_type))
        if url.endswith('/upload/image'):
            return b'{"name":"input.png","subfolder":"foo"}'
        if url.endswith('/prompt'):
            return b'{"prompt_id":"abc"}'
        return b'{"abc":{"outputs":{"4":{"images":[{"filename":"out.png"}]}}}}'
    monkeypatch.setattr(up, '_request', fake_request)
    source = tmp_path / 'in.png'
    source.write_bytes(png())
    name = up._upload('http://localhost:8188', source, 100)
    task = up._submit('http://localhost:8188', name, 'model.pth', 100)
    assert up._wait('http://localhost:8188', task, 100)['filename'] == 'out.png'
    assert b'name="overwrite"\r\n\r\ntrue' in calls[0][1]
    workflow = json.loads(calls[1][1])['prompt']
    assert workflow['1']['inputs']['image'] == 'foo/input.png'
    assert workflow['2']['inputs']['model_name'] == 'model.pth'
    assert workflow['3']['inputs'] == {'upscale_model': ['2', 0], 'image': ['1', 0]}
    assert workflow['4']['class_type'] == 'SaveImage'


def test_removed_during_upscale_cleans_file(setup_asset, monkeypatch):
    client, aid, old = setup_asset
    mock_comfy(monkeypatch, png())
    def remove(*args):
        with transaction() as conn:
            conn.execute('UPDATE asset SET ref_images=? WHERE id=?', (json.dumps([f'{aid}/other.png']), aid))
        return {'filename': 'output.png'}
    monkeypatch.setattr(up, '_wait', remove)
    before = set((FILES_DIR / str(aid)).iterdir())
    response = client.post(f'/api/assets/{aid}/images/upscale', json={'path': old})
    assert response.status_code == 404
    assert set((FILES_DIR / str(aid)).iterdir()) == before
