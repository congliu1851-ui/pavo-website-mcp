import copy
import hashlib
import json
from pathlib import Path
import zipfile

import httpx
import pytest

from pavo_website_mcp.script import Script, fingerprint
from pavo_website_mcp.store import Store, OperationUnknown
from pavo_website_mcp.client import APIError, Client
from pavo_website_mcp.canvas import Pavo


class ScriptClient:
    def __init__(self):
        self.project = {"project_id": "100", "projectName": "测试剧本", "project_type": "normal", "readonly": False,
            "status": "outline_confirming", "active_tasks": [], "outline": {"editable": True, "story_type": "奇幻", "world_setting": "雪庭", "story_summary": "保留的梗概", "character_biographies": [{"name": "小雪", "bio": "守雪人"}]},
            "episodes": [{"episode_id": "200", "episode_no": 1, "title": "第一集", "content": "原文<script>不执行</script>", "script_status": "completed"}, {"episode_id": "201", "episode_no": 2, "title": "第二集", "content": "不能清空", "script_status": "completed"}]}
        self.calls=[]
        self.timeout=False
    def request(self, method, path, payload=None, params=None, **kw):
        self.calls.append((method,path,copy.deepcopy(payload),params))
        if method != "GET":
            if self.timeout:raise TimeoutError("未知提交")
            return {"project_id": "100", "task": {"task_id": "300"}}
        if path.endswith("/styles"):return {"categories": [{"styles": [{"style_id": "style1"}]}]}
        if path.endswith("/progress"):return copy.deepcopy(self.project)
        if path.endswith("/episodes/200"):return {"project_id": "100", "episode": copy.deepcopy(self.project["episodes"][0])}
        if path.endswith("/storyboard/detail"):return {"episode_id": "200", "segments": [{"segment_id": "400", "title": "雪庭", "scenes": []}], "active_tasks": []}
        if path.endswith("/assets"):return {"characters": [], "scenes": [], "props": []}
        if path.endswith("/projects"):return {"items": [], "pagination": {"page": 1}}
        raise AssertionError((method,path,payload))


@pytest.fixture
def script(tmp_path):
    return Script(ScriptClient(),Store(tmp_path))


def test_ai_project_exact_contract_and_replay(script):
    r=script.create_ai("一位守雪人找回春天",2,"9:16",{"style_id":"style1"},"ai-1")
    write=[c for c in script.client.calls if c[0]=="POST"]
    assert write[0][1]=="/api/v1/pixa/short-drama/projects/ai-generate"
    assert write[0][2]=={"requirements":"一位守雪人找回春天","planned_episode_count":2,"ratio":"9:16","style":{"style_id":"style1"}}
    assert r["project_id"]=="100"
    assert script.create_ai("一位守雪人找回春天",2,"9:16",{"style_id":"style1"},"ai-1")==r
    assert len([c for c in script.client.calls if c[0]=="POST"])==1


def test_unknown_ai_creation_cannot_be_repeated_under_another_id(script):
    script.client.timeout=True
    args=("创建测试要求",1,"9:16",{"style_id":"style1"})
    with pytest.raises(OperationUnknown):script.create_ai(*args,"unknown-create-1")
    with pytest.raises(OperationUnknown):script.create_ai(*args,"unknown-create-2")
    assert len([c for c in script.client.calls if c[0]=="POST"])==1


def test_utf16_text_limit_and_invalid_style(script):
    with pytest.raises(ValueError,match="100000"):
        script.import_text("😀"*50001,"标题","9:16",{"style_id":"style1"},"too-long",planned_episode_count=2)
    with pytest.raises(ValueError,match="风格"):
        script.create_ai("中文",1,"9:16",{"style_id":"invented"},"bad-style")
    assert not [c for c in script.client.calls if c[0]=="POST"]


def test_story_text_uses_ai_generation_and_keeps_source(script):
    story='小雪捡到雪铃。\n雪铃引她进入庭院，最后她找回春天。'
    result=script.import_text(story,'雪铃','9:16',{'style_id':'style1'},'story-text',planned_episode_count=3)
    posts=[c for c in script.client.calls if c[0]=='POST']
    assert len(posts)==1 and posts[0][1]=='/api/v1/pixa/short-drama/projects/ai-generate'
    body=posts[0][2]
    assert body.keys()=={'requirements','planned_episode_count','ratio','style'}
    assert body['planned_episode_count']==3
    assert story in body['requirements'] and '雪铃' in body['requirements'] and '分集' in body['requirements']
    assert result['project_id']=='100' and result['task_ids']==['300']


def uploaded_story(script,tmp_path,extension):
    p=tmp_path/('雪铃'+extension)
    story='小雪捡到雪铃。\n她找回了春天。'
    if extension=='.txt':p.write_text(story,encoding='utf-8-sig')
    else:
        with zipfile.ZipFile(p,'w') as z:
            z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>小雪捡到雪</w:t></w:r><w:r><w:t>铃。</w:t></w:r></w:p><w:p><w:r><w:t>她找回了春天。</w:t></w:r></w:p></w:body></w:document>')
    original={'file_name':p.name,'file_url':'https://cdn.example/original','sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size,'inspected':True}
    script.store.operation('uploaded','script_upload_file',{'path':str(p),'sha256':original['sha256'],'bytes':original['bytes']},lambda _:original)
    return p,story


@pytest.mark.parametrize('extension',['.txt','.docx'])
def test_uploaded_story_uses_ai_not_complete_script_import(script,tmp_path,extension):
    p,story=uploaded_story(script,tmp_path,extension)
    result=script.import_file('uploaded','16:9',{'style_id':'style1'},'story-file',planned_episode_count=4)
    posts=[c for c in script.client.calls if c[0]=='POST']
    assert len(posts)==1 and posts[0][1]=='/api/v1/pixa/short-drama/projects/ai-generate'
    assert story in posts[0][2]['requirements']
    assert posts[0][2]['planned_episode_count']==4 and posts[0][2]['ratio']=='16:9'
    assert 'file_url' not in posts[0][2] and 'raw_text' not in posts[0][2]
    assert result['project_id']=='100'


def test_changed_uploaded_story_rejected_before_generation(script,tmp_path):
    p,_=uploaded_story(script,tmp_path,'.txt');p.write_text('已换成别的故事')
    with pytest.raises(ValueError,match='改变|SHA'):
        script.import_file('uploaded','9:16',{'style_id':'style1'},'changed-source',planned_episode_count=2)
    assert not [c for c in script.client.calls if c[0]=='POST']


def test_unknown_story_import_blocks_equivalent_ai_create(script):
    script.client.timeout=True
    with pytest.raises(OperationUnknown):
        script.import_text('守雪人找回春天','雪铃','9:16',{'style_id':'style1'},'story-unknown',planned_episode_count=2)
    body=[c[2] for c in script.client.calls if c[0]=='POST'][0]
    with pytest.raises(OperationUnknown):
        script.create_ai(body['requirements'],2,'9:16',{'style_id':'style1'},'direct-unknown')
    assert len([c for c in script.client.calls if c[0]=='POST'])==1


@pytest.mark.parametrize('count',[None,0,101,True])
def test_story_generation_requires_real_planned_count(script,count):
    with pytest.raises(ValueError,match='集数'):
        script.import_text('守雪人找回春天','雪铃','9:16',{'style_id':'style1'},'bad-count',planned_episode_count=count)
    assert not [c for c in script.client.calls if c[0]=='POST']


def test_story_request_limit_includes_generation_instructions(script):
    with pytest.raises(ValueError,match='100000'):
        script.import_text('雪'*99999,'雪铃','9:16',{'style_id':'style1'},'combined-limit',planned_episode_count=1)
    assert not [c for c in script.client.calls if c[0]=='POST']


def test_missing_uploaded_source_is_not_downloaded_or_generated(script,tmp_path):
    p,_=uploaded_story(script,tmp_path,'.txt');p.unlink()
    with pytest.raises(ValueError,match='文件'):
        script.import_file('uploaded','9:16',{'style_id':'style1'},'missing-source',planned_episode_count=2)
    assert not script.client.calls


def test_uploaded_story_replay_does_not_create_second_project(script,tmp_path):
    uploaded_story(script,tmp_path,'.txt')
    first=script.import_file('uploaded','9:16',{'style_id':'style1'},'file-replay',planned_episode_count=2)
    assert script.import_file('uploaded','9:16',{'style_id':'style1'},'file-replay',planned_episode_count=2)==first
    assert len([c for c in script.client.calls if c[0]=='POST'])==1


def test_readonly_demo_never_written(script):
    script.client.project["readonly"]=True
    h=script.get_project("100")["fingerprints"]["outline"]
    with pytest.raises(ValueError,match="只读"):
        script.update_outline("100",{"story_type":"都市"},h,"edit-demo")
    with pytest.raises(ValueError,match="只读"):
        script.run_stage("100","confirm_outline","confirm-demo")
    assert not [c for c in script.client.calls if c[0]!="GET"]


def test_outline_edit_preserves_other_fields_and_checks_fingerprint(script):
    before=script.get_project("100")
    script.update_outline("100",{"story_type":"冒险"},before["fingerprints"]["outline"],"outline1")
    payload=[c[2] for c in script.client.calls if c[0]=="PATCH"][0]
    assert payload["story_type"]=="冒险" and payload["story_summary"]=="保留的梗概"
    assert payload["character_biographies"]==[{"name":"小雪","bio":"守雪人"}]
    assert payload["update_character_biographies"] is False
    script.client.project["outline"]["story_summary"]="网页刚刚修改"
    with pytest.raises(ValueError,match="改变"):
        script.update_outline("100",{"story_type":"都市"},before["fingerprints"]["outline"],"outline2")


def test_update_episodes_merges_and_preserves_unspecified(script):
    before=script.get_project("100")
    script.update_episodes("100",[{"episode_id":"200","content":"新第一集"}],[],before["fingerprints"]["episodes"],"episodes1")
    payload=[c[2] for c in script.client.calls if c[0]=="PUT"][0]
    assert payload=={"episodes":[{"episode_id":"200","title":"第一集","content":"新第一集"},{"episode_id":"201","title":"第二集","content":"不能清空"}],"new_episodes":[]}


def test_episode_ownership_before_storyboard_or_generation(script):
    script.client.project["episodes"]=[{"episode_id":"201"}]
    with pytest.raises(ValueError,match="属于"):
        script.get_storyboard("100","200")
    with pytest.raises(ValueError,match="属于"):
        script.run_stage("100","generate_storyboard","bad-owner",episode_id="200")
    assert not [c for c in script.client.calls if c[0]=="POST"]


def test_stage_active_task_and_unknown_new_operation_block(script):
    script.client.project["active_tasks"]=[{"task_id":"other"}]
    with pytest.raises(ValueError,match="运行"):
        script.run_stage("100","confirm_outline","busy")
    script.client.project["active_tasks"]=[]
    script.client.timeout=True
    with pytest.raises(OperationUnknown):
        script.run_stage("100","confirm_outline","unknown1")
    with pytest.raises(OperationUnknown):
        script.run_stage("100","confirm_outline","unknown2")
    assert len([c for c in script.client.calls if c[0]=="POST"])==1


def test_stages_cannot_trigger_media_production(script):
    with pytest.raises(ValueError,match="阶段"):
        script.run_stage("100","generate_video","bad-video")
    r=script.run_stage("100","confirm_outline","confirm1")
    assert r["task_ids"]==["300"]
    assert [c[1] for c in script.client.calls if c[0]=="POST"]==["/api/v1/pixa/short-drama/projects/100/outline/confirm"]


def test_docx_validation_does_not_upload_wrong_format(script,tmp_path):
    bad=tmp_path/'伪.docx';bad.write_text('{\\rtf 真实格式不是docx}')
    with pytest.raises(ValueError,match="DOCX"):
        script.upload_file(str(bad),'bad-docx')
    bad=tmp_path/'多字.txt';bad.write_text('😀'*50001)
    with pytest.raises(ValueError,match="100000"):
        script.upload_file(str(bad),'bad-txt')


@pytest.mark.parametrize('inspect_data',[{'valid':True},None,False])
def test_script_file_upload_and_inspect_no_bearer(tmp_path,monkeypatch,inspect_data):
    monkeypatch.setattr('pavo_website_mcp.script.safe_url',lambda u:None)
    store=Store(tmp_path/'state');store.save_session('secret',{})
    requests=[]
    def api(req):
        requests.append(req)
        data=json.loads(req.content)
        if req.url.path.endswith('presigned-url'):
            assert data['purpose']=='chat_attachment'
            return httpx.Response(200,json={'code':'000000','data':{'upload_url':'https://storage.example/file?sig=private','public_url':'https://cdn.example/test.txt','method':'PUT','required_headers':{'Content-Type':'text/plain'}}})
        assert req.url.path.endswith('/file/inspect')
        assert data=={'content_type':'text/plain','url':'https://cdn.example/test.txt'}
        return httpx.Response(200,json={'code':'000000','data':inspect_data})
    def media(req):
        assert 'authorization' not in req.headers
        assert req.read()=='剧本测试'.encode()
        return httpx.Response(200)
    client=Client(store,httpx.MockTransport(api),httpx.MockTransport(media))
    p=tmp_path/'剧本.txt';p.write_text('剧本测试')
    if not inspect_data:
        with pytest.raises((APIError,OperationUnknown)):
            Script(client,store).upload_file(str(p),'upload-doc')
        assert store.get_operation('upload-doc')['status']!='SUCCESS'
        return
    result=Script(client,store).upload_file(str(p),'upload-doc')
    assert result['file_url']=='https://cdn.example/test.txt'
    assert 'sig=' not in json.dumps(store.get_operation('upload-doc'))


@pytest.mark.parametrize('status,outline,episodes,stage,path',[
    ('asset_extract_credits_locked',{'story_type':'奇幻'},[], 'extract_assets','/asset-extract'),
    ('failed',None,[], 'retry_outline','/outline/retry'),
    ('failed',{'story_type':'奇幻'},[], 'retry_episodes','/episodes/retry'),
    ('failed',{'story_type':'奇幻'},[{'episode_id':'200','episode_no':1,'script_status':'completed'}], 'extract_assets','/asset-extract'),
])
def test_native_failure_recovery_branches(script,status,outline,episodes,stage,path):
    script.client.project.update(status=status,outline=outline,episodes=episodes)
    script.run_stage('100',stage,'resume1')
    assert [c[1] for c in script.client.calls if c[0]=='POST']==['/api/v1/pixa/short-drama/projects/100'+path]


def test_generic_failure_with_episodes_cannot_retry_outline(script):
    script.client.project['status']='failed'
    with pytest.raises(ValueError,match='阶段'):
        script.run_stage('100','retry_outline','wrong-recovery')
    assert not [c for c in script.client.calls if c[0]=='POST']


def test_acknowledged_null_data_is_not_false_task_success(script):
    original=script.client.request
    script.client.request=lambda method,path,payload=None,params=None,**kw: None if method=='POST' else original(method,path,payload,params,**kw)
    result=script.run_stage('100','confirm_outline','empty-ack')
    assert result['acknowledged'] and result['task_ids']==[]
    assert result['data'] is None


def test_asset_finalize_is_explicit_native_stage(script):
    script.client.project['status']='asset_ready'
    script.run_stage('100','finalize_assets','finalize1')
    call=[c for c in script.client.calls if c[0]=='POST'][0]
    assert call[1]=='/api/v1/pixa/short-drama/projects/100/assets/finalize'
    assert call[2]=={'is_manual':True}


@pytest.mark.parametrize('format',['json','markdown','doc'])
def test_export_real_content_atomic_and_safe(script,tmp_path,format):
    result=script.export('100',str(tmp_path/'export'),format,include_storyboards=True)
    p=Path(result['path']);raw=p.read_bytes()
    assert result['sha256']==hashlib.sha256(raw).hexdigest()
    assert '不能清空' in raw.decode('utf-8-sig')
    if format=='doc':
        assert b'<script>' not in raw and b'&lt;script&gt;' in raw
    assert script.export('100',str(tmp_path/'export'),format,include_storyboards=True)['reused']
