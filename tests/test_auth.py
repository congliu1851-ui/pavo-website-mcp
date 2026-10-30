import base64
import json
import time
import hashlib

import httpx
import pytest

from pavo_website_mcp.client import Client, APIError
from pavo_website_mcp.store import Store
from pavo_website_mcp.store import OperationUnknown
from pavo_website_mcp.script import Script


def jwt():
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "account-1", "exp": time.time() + 3600}).encode()).decode().rstrip("=")
    return "eyJhbGciOiJIUzI1NiJ9." + payload + ".test-signature"


def test_import_bearer_validates_readonly_then_stores_privately(tmp_path):
    store, token, calls = Store(tmp_path), jwt(), []
    def route(req):
        calls.append(req)
        assert req.method == "GET"
        assert req.url.path == "/api/v1/pixa/canvas/folder/entries"
        assert req.headers["authorization"] == "Bearer " + token
        assert not store.status()["logged_in"]
        return httpx.Response(200, json={"code": "000000", "data": {"items": []}})
    result = Client(store, transport=httpx.MockTransport(route)).import_authorization("Bearer " + token)
    assert result["verified_by_readonly_api"] is True
    assert result["user"]["id"] == "account-1"
    assert token not in json.dumps(result)
    assert token not in (tmp_path / "operations.sqlite3").read_bytes().decode(errors="ignore")
    assert len(calls) == 1


def test_rejected_bearer_does_not_replace_existing_session(tmp_path):
    store = Store(tmp_path)
    store.save_session("old-token", {"id": "old"})
    client = Client(store, transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(APIError):
        client.import_authorization("Bearer " + jwt())
    assert store.session()["access_token"] == "old-token"


@pytest.mark.parametrize('fail_first',[False,True])
def test_account_switch_does_not_move_inflight_generation(tmp_path,fail_first):
    store=Store(tmp_path/'state');store.save_session('fake-token-A',{'id':'account-A'})
    headers=[];switched=False
    def route(req):
        nonlocal switched
        headers.append(req.headers['authorization'])
        if req.url.path.endswith('/styles'):
            if not switched:
                Store(store.root).save_session('fake-token-B',{'id':'account-B'});switched=True
                nested=store.operation('nested','check',{},lambda _: {'account':store.account_key()})
                assert nested['account']=='account-A'
            return httpx.Response(200,json={'code':'000000','data':{'categories':[{'styles':[{'style_id':'style1'}]}]}})
        assert req.url.path.endswith('/ai-generate')
        if fail_first and len(headers)==2:return httpx.Response(502)
        return httpx.Response(200,json={'code':'000000','data':{'project_id':'project-1','task':{'task_id':'task-1'}}})
    client=Client(store,httpx.MockTransport(route));script=Script(client,store)
    if fail_first:
        with pytest.raises(OperationUnknown):script.create_ai('剧情A',1,'9:16',{'style_id':'style1'},'first')
    else:script.create_ai('剧情A',1,'9:16',{'style_id':'style1'},'first')
    assert headers==['Bearer fake-token-A','Bearer fake-token-A']
    assert store.account_key()=='account-B'
    script.create_ai('剧情B',1,'9:16',{'style_id':'style1'},'second')
    assert headers[2:]==['Bearer fake-token-B','Bearer fake-token-B']
    with store.connect() as db:
        recorded={op:json.loads(payload)['_account'] for op,payload in db.execute('SELECT operation_id,payload FROM operations')}
    assert recorded=={'first':'account-A','nested':'account-A','second':'account-B'}


def test_invalid_operation_cleans_auth_context(tmp_path):
    store=Store(tmp_path);store.save_session('fake-token-A',{'id':'account-A'})
    with pytest.raises(ValueError):store.operation('','invalid',{},lambda _: {})
    Store(tmp_path).save_session('fake-token-B',{'id':'account-B'})
    assert store.account_key()=='account-B'


def test_uploaded_story_keeps_receipt_account_during_source_read(tmp_path,monkeypatch):
    store=Store(tmp_path/'state');store.save_session('fake-token-A',{'id':'account-A'})
    source=tmp_path/'剧情.txt';source.write_text('账号A的剧情',encoding='utf-8')
    payload={'path':str(source),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'bytes':source.stat().st_size}
    store.operation('upload','script_upload_file',payload,lambda _: {'file_name':source.name,**payload})
    original=Script._read_file
    def switching(path):
        result=original(path)
        Store(store.root).save_session('fake-token-B',{'id':'account-B'})
        return result
    monkeypatch.setattr(Script,'_read_file',staticmethod(switching))
    headers=[]
    def route(req):
        headers.append(req.headers['authorization'])
        data={'categories':[{'styles':[{'style_id':'style1'}]}]} if req.url.path.endswith('/styles') else {'project_id':'project-1'}
        return httpx.Response(200,json={'code':'000000','data':data})
    Script(Client(store,httpx.MockTransport(route)),store).import_file('upload','9:16',{'style_id':'style1'},'import',planned_episode_count=2)
    assert headers==['Bearer fake-token-A','Bearer fake-token-A']
    assert store.account_key()=='account-B'
    with store.connect() as db:
        recorded=json.loads(db.execute("SELECT payload FROM operations WHERE operation_id='import'").fetchone()[0])
    assert recorded['_account']=='account-A'


@pytest.mark.parametrize("value", ["Basic abc", "Bearer ", "Bearer token\r\nX-Evil: yes", "Bearer a b"])
def test_bad_authorization_not_sent(tmp_path, value):
    client = Client(Store(tmp_path), transport=httpx.MockTransport(lambda r: pytest.fail("不能发送错误凭证")))
    with pytest.raises(ValueError):
        client.import_authorization(value)
