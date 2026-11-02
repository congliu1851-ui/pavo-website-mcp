import copy

import pytest

from pavo_website_mcp.script_assets import ScriptAssets
from pavo_website_mcp.client import APIError
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


@pytest.fixture
def image_assets(assets):
    original=assets.client.request
    assets.client.sync_image=True
    assets.client.damage_threeview=False
    assets.client.damage_main_image=False
    assets.client.damage_flat_style=False
    assets.client.image_posted=False
    assets.client.image_readback_failure=None
    assets.client.assets['characters'][0]['states'][0]['three_view_image']={'url':'https://cdn.example/three.png','width':1448,'height':1086,'aspect_ratio':'4:3'}
    def request(method,path,payload=None,params=None,**kw):
        if method=='GET' and path.endswith('/assets') and assets.client.image_posted:
            if assets.client.image_readback_failure=='unauthorized':raise APIError('回读登录失效',status=401,definite=True)
            if assets.client.image_readback_failure=='missing':return {'characters':[],'scenes':[],'props':[]}
        if method=='POST' and path.endswith('/art/art-versions'):
            assets.client.calls.append((method,path,copy.deepcopy(payload),params))
            if assets.client.timeout:raise TimeoutError('主图提交未知')
            assets.client.image_posted=True
            if assets.client.sync_image:
                plural={'character':'characters','scene':'scenes','prop':'props'}[payload['art_type']]
                for state in assets.client.assets[plural][0]['states']:
                    if state['state_name']==payload['variant']:
                        for source,target in [('image','main_image'),('threeview','three_view_image')]:
                            if payload[source]['url']:
                                state[target]=copy.deepcopy(payload[source])
                                state[target+'_status']='success'
                        state['has_art_versions']=True
                        if assets.client.damage_threeview:state['three_view_image']=None
                        if assets.client.damage_main_image:state['main_image']=None
                        if assets.client.damage_flat_style:
                            state.pop('style_id',None);state.pop('custom_style_prompt',None)
            return {'version_id':'real-version-1'}
        return original(method,path,payload,params,**kw)
    assets.client.request=request
    return assets


@pytest.mark.parametrize('kind,plural',[('character','characters'),('scene','scenes'),('prop','props')])
def test_set_main_image_syncs_native_slot_and_replays_once(image_assets,kind,plural):
    assets=image_assets
    before=copy.deepcopy(assets.client.assets[plural][0]['states'][0])
    fp=assets.get('100')['_mcp_fingerprints'][plural][plural+'1']['states']['state-'+plural]
    args=('100',kind,plural+'1','state-'+plural,'https://1.1.1.1/new.png',1086,1448,fp,'main-'+kind)
    result=assets.set_main_image(*args)
    assert result['verified'] is True and result['state_id']=='state-'+plural
    assert result['image']['url']=='https://1.1.1.1/new.png'
    writes=[c for c in assets.client.calls if c[0]=='POST']
    assert len(writes)==1
    body=writes[0][2]
    assert writes[0][1]=='/api/v1/pixa/short-drama/art/art-versions'
    assert body['art_type']==kind and body['entity_id']==plural+'1' and body['variant']=='默认'
    assert body['sync_to_entity'] is True and body['task_id']=='0'
    assert body['image']=={'url':'https://1.1.1.1/new.png','thumbnail_url':'https://1.1.1.1/new.png','width':1086,'height':1448,'aspect_ratio':'3:4'}
    assert body['threeview']['url']=='' and 'state_id' not in body
    for key in ['description','reference_images','episode_nos','model_code','style','three_view_image']:
        assert assets.client.assets[plural][0]['states'][0].get(key)==before.get(key)
    assert assets.set_main_image(*args)==result
    assert len([c for c in assets.client.calls if c[0]=='POST'])==1


@pytest.mark.parametrize('url,width,height',[('http://127.0.0.1/x',1086,1448),('https://127.0.0.1/x',1086,1448),('https://1.1.1.1/x',True,1448),('https://1.1.1.1/x',0,1448)])
def test_main_image_rejects_private_urls_and_invalid_dimensions(image_assets,url,width,height):
    with pytest.raises(ValueError):
        image_assets.set_main_image('100','character','characters1','state-characters',url,width,height,'unused','invalid-main')
    assert not [c for c in image_assets.client.calls if c[0]=='POST']


@pytest.mark.parametrize('failure',['wrong-state','stale','readonly','duplicate-name'])
def test_main_image_requires_owned_current_unique_state(image_assets,failure):
    a=image_assets;fp=a.get('100')['_mcp_fingerprints']['characters']['characters1']['states']['state-characters']
    sid='state-characters'
    if failure=='wrong-state':sid='state-scenes'
    if failure=='stale':a.client.assets['characters'][0]['states'][0]['description']='网页新描述'
    if failure=='readonly':a.client.project['readonly']=True
    if failure=='duplicate-name':a.client.assets['characters'][0]['states'].append({'state_id':'other','state_name':'默认'})
    with pytest.raises(ValueError):
        a.set_main_image('100','character','characters1',sid,'https://1.1.1.1/x',1086,1448,fp,'reject-'+failure)
    assert not [c for c in a.client.calls if c[0]=='POST']


@pytest.mark.parametrize('failure',['timeout','not-synced'])
def test_unverified_main_image_stays_unknown_and_blocks_new_operation(image_assets,failure):
    a=image_assets;fp=a.get('100')['_mcp_fingerprints']['characters']['characters1']['states']['state-characters']
    a.client.timeout=failure=='timeout';a.client.sync_image=failure!='not-synced'
    args=('100','character','characters1','state-characters','https://1.1.1.1/x',1086,1448,fp)
    with pytest.raises(OperationUnknown):a.set_main_image(*args,'main-unknown')
    assert a.store.get_operation('main-unknown')['status']=='UNKNOWN'
    with pytest.raises(OperationUnknown):a.set_main_image(*args,'main-retry')
    assert len([c for c in a.client.calls if c[0]=='POST'])==1


def test_main_image_never_reports_success_if_existing_threeview_is_lost(image_assets):
    a=image_assets;a.client.damage_threeview=True
    fp=a.get('100')['_mcp_fingerprints']['characters']['characters1']['states']['state-characters']
    with pytest.raises(OperationUnknown):
        a.set_main_image('100','character','characters1','state-characters','https://1.1.1.1/x',1086,1448,fp,'lost-threeview')
    assert a.store.get_operation('lost-threeview')['status']=='UNKNOWN'
    fresh=a.get('100')['_mcp_fingerprints']['characters']['characters1']['states']['state-characters']
    with pytest.raises(OperationUnknown):
        a.set_main_image('100','character','characters1','state-characters','https://1.1.1.1/x',1086,1448,fresh,'cannot-retry-threeview')
    assert len([c for c in a.client.calls if c[0]=='POST'])==1


@pytest.mark.parametrize('style',[{'style_id':'flat-style'},{'custom_style_prompt':'都市真人'}])
def test_main_image_checks_flat_style_during_readback(image_assets,style):
    a=image_assets;state=a.client.assets['characters'][0]['states'][0]
    state.pop('style');state.update(style);a.client.damage_flat_style=True
    fp=a.get('100')['_mcp_fingerprints']['characters']['characters1']['states']['state-characters']
    with pytest.raises(OperationUnknown):
        a.set_main_image('100','character','characters1','state-characters','https://1.1.1.1/x',1086,1448,fp,'lost-flat-style')
    assert a.store.get_operation('lost-flat-style')['status']=='UNKNOWN'


@pytest.mark.parametrize('failure',['unauthorized','missing'])
def test_post_success_readback_failure_cannot_be_marked_failed_or_reposted(image_assets,failure):
    a=image_assets;a.client.image_readback_failure=failure
    fp=a.get('100')['_mcp_fingerprints']['characters']['characters1']['states']['state-characters']
    args=('100','character','characters1','state-characters','https://1.1.1.1/x',1086,1448)
    with pytest.raises(OperationUnknown):a.set_main_image(*args,fp,'readback-failure')
    assert a.store.get_operation('readback-failure')['status']=='UNKNOWN'
    a.client.image_readback_failure=None
    fresh=a.get('100')['_mcp_fingerprints']['characters']['characters1']['states']['state-characters']
    with pytest.raises(OperationUnknown):a.set_main_image(*args,fresh,'readback-retry')
    assert len([c for c in a.client.calls if c[0]=='POST'])==1


def test_four_view_image_uses_separate_slot_and_keeps_main_image(image_assets):
    a=image_assets;before=copy.deepcopy(a.client.assets['characters'][0]['states'][0])
    fp=a.get('100')['_mcp_fingerprints']['characters']['characters1']['states']['state-characters']
    args=('100','characters1','state-characters','https://1.1.1.1/four.png',1448,1086,fp,'four-view')
    result=a.set_four_view_image(*args)
    assert result['verified'] and result['image_field']=='three_view_image'
    assert result['image']['url']=='https://1.1.1.1/four.png'
    writes=[c for c in a.client.calls if c[0]=='POST'];assert len(writes)==1
    body=writes[0][2];assert body['art_type']=='character' and body['image']['url']==''
    assert body['threeview']=={'url':'https://1.1.1.1/four.png','thumbnail_url':'https://1.1.1.1/four.png','width':1448,'height':1086,'aspect_ratio':'4:3'}
    saved=a.client.assets['characters'][0]['states'][0]
    assert saved['main_image']==before['main_image'] and saved['main_image_status']==before['main_image_status']
    assert saved['description']==before['description'] and saved['reference_images']==before['reference_images']
    assert a.set_four_view_image(*args)==result
    assert len([c for c in a.client.calls if c[0]=='POST'])==1


def test_four_view_never_reports_success_if_main_image_is_lost(image_assets):
    a=image_assets;a.client.damage_main_image=True
    fp=a.get('100')['_mcp_fingerprints']['characters']['characters1']['states']['state-characters']
    with pytest.raises(OperationUnknown):
        a.set_four_view_image('100','characters1','state-characters','https://1.1.1.1/four.png',1448,1086,fp,'lost-main')
    assert a.store.get_operation('lost-main')['status']=='UNKNOWN'


@pytest.mark.parametrize('failure',['timeout','not-synced','unauthorized','missing'])
def test_four_view_unknown_cannot_be_reposted(image_assets,failure):
    a=image_assets;fp=a.get('100')['_mcp_fingerprints']['characters']['characters1']['states']['state-characters']
    a.client.timeout=failure=='timeout';a.client.sync_image=failure!='not-synced'
    a.client.image_readback_failure=failure if failure in ['unauthorized','missing'] else None
    args=('100','characters1','state-characters','https://1.1.1.1/four.png',1448,1086)
    with pytest.raises(OperationUnknown):a.set_four_view_image(*args,fp,'four-unknown')
    assert a.store.get_operation('four-unknown')['status']=='UNKNOWN'
    a.client.image_readback_failure=None
    fresh=a.get('100')['_mcp_fingerprints']['characters']['characters1']['states']['state-characters']
    with pytest.raises(OperationUnknown):a.set_four_view_image(*args,fresh,'four-retry')
    assert len([c for c in a.client.calls if c[0]=='POST'])==1
