"""策略只读提示、写边界与日志表示形式的回归。"""
from __future__ import annotations
from contextlib import contextmanager
from io import StringIO
from typing import Any, Iterator
import json
import sqlite3
import pytest
from pydantic import ValidationError
from db import get_conn
from repositories.model_center import model_center


@pytest.fixture(autouse=True)
def restore_policies() -> Iterator[None]:
    rows = [dict(row) for row in model_center.policies()]
    yield
    with get_conn() as conn:
        for row in rows:
            columns = [key for key in row if key != 'id']
            conn.execute('UPDATE model_policy SET '+','.join(key+'=?' for key in columns)+' WHERE id=?',
                         [row[key] for key in columns]+[row['id']])


def payload(**changes: Any) -> dict[str, Any]:
    return dict(scene_key='character_base', capability='image', required=[],
                slot_provider='mflux', slot_model='flux1-schnell', enabled=True) | changes


@pytest.mark.parametrize('kind', ['ascii_json', 'file_uri'])
@pytest.mark.parametrize('chunk_size', [1, 7, 8192])
def test_stream_private_representations(kind: str, chunk_size: int) -> None:
    from worker_pkg.output import OutputLog
    secret = '尚未公开的创作正文'
    text = json.dumps(secret) if kind=='ascii_json' else 'file:///server/private/model.ckpt'
    sink = StringIO()
    output = OutputLog(sink, [secret])
    data = (text+'\nhttps://example.test/status\n进度50%').encode()
    for start in range(0,len(data),chunk_size):
        output.write(data[start:start+chunk_size])
    output.finish()
    assert text not in sink.getvalue()
    assert 'https://example.test/status' in sink.getvalue()
    assert '进度50%' in sink.getvalue()


@pytest.mark.parametrize('enabled',[False,True])
def test_public_resolve_boolean_and_permissions(as_role: Any, enabled: bool) -> None:
    assert as_role('admin').put('/api/model-policies',json=[payload(enabled=enabled)]).status_code==200
    client=as_role('viewer')
    out=client.get('/api/model-policies/resolve/character_base').json()
    assert out['enabled'] is enabled
    assert out['slot_provider']=='mflux' and out['slot_model']=='flux1-schnell'
    assert out['auto_switch'] is False
    assert client.get('/api/model-policies').status_code==403
    assert client.put('/api/model-policies',json=[payload()]).status_code==403
    missing=client.get('/api/model-policies/resolve/nonexistent').json()
    assert missing==dict(scene_key='nonexistent',found=False,candidates=[],
                         blockers=['没有这个任务场景：nonexistent'],auto_switch=False)


@pytest.mark.parametrize('model',['','   '])
def test_enabled_requires_complete_tier(as_role: Any, model: str) -> None:
    before=[dict(row) for row in model_center.policies()]
    assert as_role('admin').put('/api/model-policies',json=[payload(slot_model=model)]).status_code==422
    assert [dict(row) for row in model_center.policies()]==before


def test_duplicate_scene_batch_is_rejected(as_role: Any) -> None:
    before=[dict(row) for row in model_center.policies()]
    assert as_role('admin').put('/api/model-policies',json=[payload(),payload(enabled=False)]).status_code==422
    assert [dict(row) for row in model_center.policies()]==before


def test_clear_disable_is_traced_and_unchanged_save_is_not(as_role: Any) -> None:
    from repositories.config_log import of_subject
    client=as_role('admin')
    assert client.put('/api/model-policies',json=[payload()]).status_code==200
    before=len(of_subject('model_policy','character_base',200))
    assert client.put('/api/model-policies',json=[payload()]).status_code==200
    assert len(of_subject('model_policy','character_base',200))==before
    assert client.put('/api/model-policies',json=[payload(enabled=False,slot_provider='',slot_model='')]).status_code==200
    records=of_subject('model_policy','character_base',200)
    assert len(records)==before+1
    change=json.loads(records[0]['payload_json'])
    assert change['before']['enabled'] is True and change['after']['enabled'] is False
    assert change['after']['slot_provider']==''


@pytest.mark.parametrize('raw',['null','{}','[{}]','broken'])
def test_broken_required_returns_controlled_configuration_error(as_role: Any, raw: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE model_policy SET required_json=? WHERE scene_key='character_base'",(raw,))
    response=as_role('viewer').get('/api/model-policies/resolve/character_base')
    assert response.status_code==500
    assert '策略必需能力' in response.json()['detail']
    assert 'candidates' not in response.json()
    assert as_role('admin').put('/api/model-policies',json=[payload()]).status_code==200


def test_config_log_failure_warns_without_sensitive_error(monkeypatch: pytest.MonkeyPatch, caplog: Any) -> None:
    from repositories import config_log
    @contextmanager
    def fail() -> Iterator[Any]:
        raise sqlite3.OperationalError('synthetic-secret /server/private')
        yield
    monkeypatch.setattr(config_log,'get_conn',fail)
    config_log.record('model_policy','character_base','update','test')
    assert 'OperationalError' in caplog.text and 'config_change_record_failed' in caplog.text
    assert 'synthetic-secret' not in caplog.text and '/server/private' not in caplog.text


@pytest.mark.parametrize('changes',[{'candidates':42},{'enabled':'bad'},{'blockers':{}},{'auto_switch':True}])
def test_resolve_schema_rejects_wrong_shape(changes: dict[str, Any]) -> None:
    from schemas.model_center import ModelResolveOut
    with pytest.raises(ValidationError):
        ModelResolveOut.model_validate(dict(scene_key='missing',found=False,candidates=[],
                                             blockers=[],auto_switch=False)|changes)


def test_catalog_slot_snapshot_is_request_local(monkeypatch: pytest.MonkeyPatch) -> None:
    import model_center as mc
    from repositories.providers import providers
    from model_center_pkg.slots import _SLOT_MODELS
    import adapters
    adapter=next(a for a in adapters.ADAPTERS if a.backend=='local' and len(a.models)>=2)
    ids=[]
    for spec in adapter.models[:2]:
        row=model_center.create(dict(capability=adapter.capability,backend='remote',
                                    provider=adapter.code,model_code=spec.code))
        ids.append(row['id'])
    original=providers.all
    calls=[]
    def all_slots() -> list[sqlite3.Row]:
        calls.append(1)
        return original()
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError('目录不得逐行读取槽位')
    monkeypatch.setattr(providers,'all',all_slots)
    monkeypatch.setattr(providers,'slot',forbidden)
    for _ in range(2):
        mc.catalog()
        assert _SLOT_MODELS.get() is None
    assert len(calls)==2
    def fail() -> list[sqlite3.Row]:
        raise RuntimeError('injected catalog failure')
    monkeypatch.setattr(mc.model_center,'registrations',fail)
    with pytest.raises(RuntimeError):
        mc.catalog()
    assert _SLOT_MODELS.get() is None

    for identifier in ids:
        model_center.delete(identifier)


def test_policy_batch_database_failure_rolls_back(as_role: Any) -> None:
    from services.model_configuration import save_policies
    from models import ModelPolicyIn
    before=[dict(row) for row in model_center.policies()]
    with get_conn() as conn:
        conn.execute("CREATE TRIGGER contract_abort BEFORE UPDATE ON model_policy WHEN NEW.scene_key='scene_prompt' BEGIN SELECT RAISE(ABORT,'injected'); END")
    try:
        with pytest.raises(sqlite3.IntegrityError):
            save_policies([ModelPolicyIn(**payload()),ModelPolicyIn(scene_key='scene_prompt',capability='text')],1)
        assert [dict(row) for row in model_center.policies()]==before
    finally:
        with get_conn() as conn:
            conn.execute('DROP TRIGGER contract_abort')


def test_read_contracts_never_probe(as_role: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    import engines
    import cloud_probe
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError('GET 不得探测')
    monkeypatch.setattr(engines,'probe',forbidden)
    monkeypatch.setattr(cloud_probe,'probe_registered',forbidden)
    admin=as_role('admin')
    for path in ('/api/models','/api/engines','/api/model-policies/scenes',
                 '/api/model-policies/resolve/character_base'):
        assert admin.get(path).status_code==200


def test_response_extra_fields_survive() -> None:
    from schemas.model_center import ModelResolveOut, ModelScenesOut
    out=ModelResolveOut.model_validate(dict(scene_key='missing',found=False,candidates=[],
                       blockers=[],auto_switch=False,future_field={'nested':True}))
    result=out.model_dump(exclude_unset=True)
    assert result['future_field']=={'nested':True} and 'enabled' not in result
    assert ModelScenesOut.model_json_schema()['properties']['items']['items'].get('$ref')


def test_catalog_snapshot_reads_latest_values(monkeypatch: pytest.MonkeyPatch) -> None:
    import model_center as mc
    from repositories.providers import providers
    from model_center_pkg import slots
    original=providers.all
    observed=[]
    def registrations() -> list[sqlite3.Row]:
        observed.append(slots._SLOT_MODELS.get().copy())
        return []
    monkeypatch.setattr(mc.model_center,'registrations',registrations)
    first=[dict(row) for row in original()]
    monkeypatch.setattr(providers,'all',lambda: first)
    mc.catalog()
    second=[dict(row, model='next-request-model') for row in first]
    monkeypatch.setattr(providers,'all',lambda: second)
    mc.catalog()
    assert observed[0]!=observed[1]
    assert all(value=='next-request-model' for value in observed[1].values())
