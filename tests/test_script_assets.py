import copy

import pytest

from pavo_website_mcp.script_assets import ScriptAssets
from pavo_website_mcp.store import OperationUnknown
from test_script import ScriptClient
from pavo_website_mcp.script import Script
from pavo_website_mcp.store import Store


@pytest.fixture
def assets(tmp_path):
    client = ScriptClient()
    client.assets = {}
    for plural,key,label in [('characters','character_id','name'),('scenes','scene_id','title'),('props','prop_id','title')]:
        client.assets[plural]=[{key:plural+'1',label:'原名称','origin_source':'manual','states':[{
            'state_id':'state-'+plural,'parent_id':plural+'1','state_name':'默认','description':'原描述','sort_order':0,
            'is_default':True,'main_image':{'url':'https://cdn.example/keep.png','thumbnail_url':'https://cdn.example/keep-small.png','width':720,'height':1280,'aspect_ratio':'9:16'},'main_image_status':'success',
            'reference_images':['https://cdn.example/ref.png'],'episode_nos':[1],'episode_binding_editable':True,
            'style':{'style_id':'style1'},'model_code':'original-model'}]}]
    original = client.request
    def request(method,path,payload=None,params=None,**kw):
        if method=='GET' and path.endswith('/assets'):return copy.deepcopy(client.assets)
        if method=='POST' and '/assets/' in path and not path.endswith('/states'):
            client.calls.append((method,path,payload,params))
            if client.timeout:raise TimeoutError('未知创建')
            plural=path.rsplit('/',1)[1]
            singular={'characters':'character','scenes':'scene','props':'prop'}[plural]
            return {singular:{singular+'_id':'created-1','states':[{'state_id':'new-state','state_name':payload['state_name']}]}}
        if method=='POST' and path.endswith('/states'):
            client.calls.append((method,path,payload,params))
            return {'state':{'state_id':payload.get('state_id','created-state'),'state_name':payload['state_name']}}
        return original(method,path,payload,params,**kw)
    client.request=request
    return ScriptAssets(Script(client,Store(tmp_path)))


@pytest.mark.parametrize('kind,plural,name_field',[('character','characters','name'),('scene','scenes','title'),('prop','props','title')])
def test_manual_asset_creation_uses_native_entity_and_initial_state(assets,kind,plural,name_field):
    result=assets.create('100',kind,'新增资料','create-'+kind,description='手工描述')
    method,path,body,_=[c for c in assets.client.calls if c[0]=='POST'][0]
    assert path.endswith('/projects/100/assets/'+plural)
    assert body[name_field]=='新增资料' and body['description']=='手工描述'
    assert body['state_name']=='默认'
    assert 'state_id' not in body
    assert result['asset_id']=='created-1' and result['state_id']=='new-state'
    assert 'generate' not in path


def test_unknown_asset_creation_cannot_change_operation_id(assets):
    assets.client.timeout=True
    with pytest.raises(OperationUnknown):assets.create('100','character','新增角色','a1')
    with pytest.raises(OperationUnknown):assets.create('100','character','新增角色','a2')
    assert len([c for c in assets.client.calls if c[0]=='POST'])==1


@pytest.mark.parametrize('kind,plural',[('character','characters'),('scene','scenes'),('prop','props')])
def test_state_update_is_native_upsert_and_preserves_images(assets,kind,plural):
    read=assets.get('100')
    asset_id=plural+'1';state_id='state-'+plural
    fp=read['_mcp_fingerprints'][plural][asset_id]['states'][state_id]
    result=assets.save_state('100',kind,asset_id,{'description':'修改描述'},'state1',state_id=state_id,expected_sha256=fp)
    _,path,body,_=[c for c in assets.client.calls if c[0]=='POST'][0]
    assert path.endswith('/assets/'+plural+'/'+asset_id+'/states')
    assert body['state_id']==state_id and body['description']=='修改描述'
    assert body['main_image']==assets.client.assets[plural][0]['states'][0]['main_image'] and body['main_image_status']=='success'
    assert body['reference_images']==['https://cdn.example/ref.png'] and body['episode_nos']==[1]
    assert body['model_code']=='original-model' and body['style_id']=='style1'
    assert result['state_id']==state_id


def test_rename_keeps_asset_type_and_requires_current_fingerprint(assets):
    r=assets.get('100');fp=r['_mcp_fingerprints']['scenes']['scenes1']['asset']
    assets.rename('100','scene','scenes1','新场景名',fp,'rename1')
    write=[c for c in assets.client.calls if c[0]=='PATCH'][0]
    assert write[2]=={'title':'新场景名'}
    assets.client.assets['scenes'][0]['title']='网页修改'
    with pytest.raises(ValueError,match='改变'):
        assets.rename('100','scene','scenes1','覆盖',fp,'rename2')


def test_asset_state_ownership_readonly_and_bound_episode_protection(assets):
    with pytest.raises(ValueError,match='属于'):
        assets.save_state('100','character','characters1',{'description':'错状态'},'wrong',state_id='state-scenes',expected_sha256='x')
    state=assets.client.assets['characters'][0]['states'][0]
    state['episode_binding_editable']=False
    assets.client.assets['characters'][0]['origin_source']='extracted'
    fp=assets.get('100')['_mcp_fingerprints']['characters']['characters1']['states']['state-characters']
    with pytest.raises(ValueError,match='绑定'):
        assets.save_state('100','character','characters1',{'episode_nos':[2]},'bind1',state_id='state-characters',expected_sha256=fp)
    assets.client.project['readonly']=True
    with pytest.raises(ValueError,match='只读'):
        assets.create('100','prop','不能创建','readonly1')


def test_new_state_has_no_fake_id_and_reference_private_url_rejected(assets):
    r=assets.save_state('100','scene','scenes1',{'state_name':'夜景','description':'月光下的庭院'},'new-state')
    assert r['state_id']=='created-state'
    body=[c[2] for c in assets.client.calls if c[0]=='POST'][0]
    assert 'state_id' not in body
    assert body['state_name']=='夜景'
    with pytest.raises(ValueError):
        assets.save_state('100','prop','props1',{'state_name':'参考','description':'','reference_images':['http://127.0.0.1/a']},'bad-url')


@pytest.mark.parametrize('style',[{'style_id':'state-style'},{'custom_style_prompt':'状态独有风格'}])
def test_flat_state_style_survives_description_edit(assets,style):
    state=assets.client.assets['characters'][0]['states'][0]
    state.pop('style');state.update(style)
    fp=assets.get('100')['_mcp_fingerprints']['characters']['characters1']['states']['state-characters']
    assets.save_state('100','character','characters1',{'description':'新描述'},'flat-style',state_id='state-characters',expected_sha256=fp)
    body=[c[2] for c in assets.client.calls if c[0]=='POST'][0]
    for key,value in style.items():assert body[key]==value


def test_conflicting_native_state_id_is_unknown_not_success(assets):
    original=assets.client.request
    def request(method,path,*args,**kw):
        if method=='POST' and path.endswith('/states'):return {'state':{'state_id':'different-state'}}
        return original(method,path,*args,**kw)
    assets.client.request=request
    fp=assets.get('100')['_mcp_fingerprints']['characters']['characters1']['states']['state-characters']
    with pytest.raises(OperationUnknown):
        assets.save_state('100','character','characters1',{'description':'新描述'},'conflict-id',state_id='state-characters',expected_sha256=fp)
    assert assets.store.get_operation('conflict-id')['status']=='UNKNOWN'


@pytest.mark.parametrize('response',[{'state_id':'direct-state'},{'prop':{'state_id':'direct-state'}}])
def test_new_state_accepts_native_direct_state_id_response(assets,response):
    original=assets.client.request
    def request(method,path,*args,**kw):
        if method=='POST' and path.endswith('/states'):return response
        return original(method,path,*args,**kw)
    assets.client.request=request
    result=assets.save_state('100','prop','props1',{'state_name':'备用','description':'备用道具'},'direct-state')
    assert result['state_id']=='direct-state'


def test_new_state_rejects_unused_fingerprint_before_post(assets):
    with pytest.raises(ValueError,match='新增状态'):
        assets.save_state('100','prop','props1',{'state_name':'备用','description':'备用道具'},'unused-fp',expected_sha256='unused')
    assert not [c for c in assets.client.calls if c[0]=='POST']


def test_existing_state_accepts_empty_native_ack(assets):
    original=assets.client.request
    def request(method,path,*args,**kw):
        if method=='POST' and path.endswith('/states'):return None
        return original(method,path,*args,**kw)
    assets.client.request=request
    fp=assets.get('100')['_mcp_fingerprints']['props']['props1']['states']['state-props']
    result=assets.save_state('100','prop','props1',{'description':'新描述'},'empty-ack',state_id='state-props',expected_sha256=fp)
    assert result['state_id']=='state-props'


def test_invalid_creation_receipt_id_is_unknown(assets):
    original=assets.client.request
    def request(method,path,*args,**kw):
        if method=='POST':return {'prop':{'prop_id':'invalid/id','states':[{'state_id':'a'}]}}
        return original(method,path,*args,**kw)
    assets.client.request=request
    with pytest.raises(OperationUnknown):assets.create('100','prop','道具','invalid-receipt')
    assert assets.store.get_operation('invalid-receipt')['status']=='UNKNOWN'


@pytest.mark.parametrize('response',[
    {'scene':{'state':{'state_id':'default-state','state_name':'默认'},'states':[{'state_id':'default-state','state_name':'默认'},{'state_id':'created-night','state_name':'夜景'}]}},
    {'scene':{'scene_id':'scenes1'},'state_id':'created-night'},
])
def test_new_variant_receipt_selects_requested_state(assets,response):
    original=assets.client.request
    def request(method,path,*args,**kw):
        if method=='POST' and path.endswith('/states'):return response
        return original(method,path,*args,**kw)
    assets.client.request=request
    result=assets.save_state('100','scene','scenes1',{'state_name':'夜景','description':'月光庭院'},'night')
    assert result['state_id']=='created-night'


@pytest.mark.parametrize('response',[
    {'scene':{'state':{'state_id':'default-existing','state_name':'默认'},'states':[{'state_id':'default-existing','state_name':'默认'}]}},
    {'state_id':'default-existing','state_name':'默认'},
    {'scene':{'state_id':'default-existing','state_name':'默认'}},
])
def test_new_state_never_accepts_explicit_other_state_name(assets,response):
    original=assets.client.request
    def request(method,path,*args,**kw):
        if method=='POST' and path.endswith('/states'):return response
        return original(method,path,*args,**kw)
    assets.client.request=request
    with pytest.raises(OperationUnknown):
        assets.save_state('100','scene','scenes1',{'state_name':'夜景','description':'月光庭院'},'night')
    assert assets.store.get_operation('night')['status']=='UNKNOWN'
