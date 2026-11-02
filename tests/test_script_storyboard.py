import copy

import pytest

from pavo_website_mcp.script import Script
from pavo_website_mcp.client import APIError
from pavo_website_mcp.script_storyboard import ScriptStoryboard, entities, refs
from pavo_website_mcp.store import Store, OperationUnknown, canonical
from test_script import ScriptClient


SCENE = '@[会议室](sd-asset://scene/scene1)'
ACTOR = '@[小雪](sd-asset://character/char1)'


class StoryboardClient(ScriptClient):
    def __init__(self):
        super().__init__()
        self.board = {'episode_id': '200', 'ratio': '9:16', 'storyboard_phase': 'storyboard_script_ready', 'active_tasks': [], 'segments': [
            {'segment_id': '400', 'display_order': 1, 'title': '原片段', 'summary': '保留概要', 'selected_keyframe_url': 'https://cdn.example/image.png',
             'selected_keyframe_id': 'keep-id', 'selected_video_url': 'https://cdn.example/video.mp4', 'model_code': 'keep-model', 'scenes': [
                 {'scene_id': '500', 'display_order': 1, 'title': '会议室', 'content_rich': SCENE, 'shots': [
                     {'shot_id': '600', 'display_order': 1, 'title': '第一镜', 'content_rich': ACTOR+'停在桌前。', 'duration_sec': 4},
                     {'shot_id': '601', 'display_order': 2, 'title': '第二镜', 'content_rich': ACTOR+'低声回答。', 'duration_sec': 5}]}]}]}
        self.next_id = 700
        self.corrupt_media = False
        self.partial = False
        self.no_save = False
        self.default_shot = True
        self.edited_shots = set()
        self.stale_prompt = False
        self.refresh()

    def refresh(self):
        for segment in self.board['segments']:
            total = 0
            for scene in segment['scenes']:
                inherited = {x for x in refs(scene['content_rich']) if x[0]=='scene'}
                combined = refs(scene['content_rich'])
                scene['content_plain'] = scene['content_rich']
                scene['linked_scene_asset_id'] = next((x[1] for x in inherited), None)
                for shot in scene['shots']:
                    shot['content_plain'] = shot['content_rich']
                    total += shot['duration_sec']
                    pairs = refs(shot['content_rich']) | (set() if shot['shot_id'] in self.edited_shots else inherited)
                    combined |= pairs
                    shot['asset_refs'] = self.make_refs(pairs)
                scene['asset_refs'] = self.make_refs(combined)
            segment['estimated_duration_ms'] = total * 1000
            lines=[];start=0
            for c in segment['scenes']:
                for x in c['shots']:
                    end=start+x['duration_sec'];lines.append(f"【{start} s - {end} s】"+x['content_rich']);start=end
            segment['video_prompt_text'] = '\n'.join(lines) or '空片段'

    @staticmethod
    def make_refs(pairs):
        return [{'relation_type':k, {'scene':'scene_asset_id','character':'character_state_id','prop':'prop_state_id'}[k]:v} for k,v in sorted(pairs)]

    def request(self, method, path, payload=None, params=None, **kw):
        if path.endswith('/storyboard/detail'):
            self.calls.append((method,path,payload,params))
            return copy.deepcopy(self.board)
        if method=='GET' and path.endswith('/assets'):
            return {plural:[{'states':[{'state_id':sid}]}] for plural,sid in [('scenes','scene1'),('characters','char1'),('props','prop1')]}
        if method in {'PATCH','POST','DELETE'} and any(x in path for x in ['/shots','/segments']):
            self.calls.append((method,path,copy.deepcopy(payload),params))
            if self.timeout: raise TimeoutError('提交未知')
            previous_prompt=self.board['segments'][0]['video_prompt_text']
            if path.endswith('/shots') and method=='PATCH':
                _, scenes, shots = entities(self.board)
                if not self.no_save:
                    for p in payload['shots']:
                        shots[p['shot_id']].update(p);self.edited_shots.add(p['shot_id'])
                    for p in payload['scenes']: scenes[p['scene_id']].update(p)
                result = {'results':[{'shot_id':p['shot_id'],'success':not self.partial} for p in payload['shots']],
                          'scene_results':[{'scene_id':p['scene_id'],'success':True} for p in payload['scenes']]}
            elif method=='POST' and path.endswith('/segments'):
                self.next_id+=1; sid=str(self.next_id)
                segment={'segment_id':sid,'display_order':payload['display_order'],'title':'新片段','summary':'','selected_keyframe_url':'','selected_video_url':'','scenes':[
                    {'scene_id':'s'+sid,'title':'新场景','display_order':1,'content_rich':'','shots':[]} ]}
                if self.default_shot: segment['scenes'][0]['shots']=[{'shot_id':'h'+sid,'display_order':1,'title':'新镜','content_rich':'','duration_sec':1}]
                for old in self.board['segments']:
                    if old['display_order']>=payload['display_order']:old['display_order']+=1
                self.board['segments'].append(segment);self.board['segments'].sort(key=lambda x:x['display_order'])
                result={'segment_id':sid}
            elif method=='POST' and path.endswith('/shots'):
                seg=entities(self.board)[0][path.split('/')[-2]]; self.next_id+=1;sid=str(self.next_id)
                for old in seg['scenes'][0]['shots']:
                    if old['display_order']>=payload['display_order']:old['display_order']+=1
                seg['scenes'][0]['shots'].append({'shot_id':sid,'display_order':payload['display_order'],'title':'新镜','content_rich':'','duration_sec':1})
                seg['scenes'][0]['shots'].sort(key=lambda x:x['display_order']);result={'shot_id':sid}
            elif method=='DELETE':
                for seg in self.board['segments']:
                    for scene in seg['scenes']:scene['shots']=[s for s in scene['shots'] if s['shot_id']!=path.split('/')[-1]]
                result=None
            else: raise AssertionError((method,path,payload))
            self.refresh()
            if self.stale_prompt:self.board['segments'][0]['video_prompt_text']=previous_prompt
            if self.corrupt_media:self.board['segments'][0]['selected_video_url']=''
            return result
        return super().request(method,path,payload,params,**kw)


@pytest.fixture
def board(tmp_path):
    return ScriptStoryboard(Script(StoryboardClient(),Store(tmp_path)))


def fp(board): return board.get('100','200')['_mcp_fingerprints']['storyboard']


def test_read_has_native_structure_and_entity_fingerprints(board):
    r=board.get('100','200')
    assert r['segments'][0]['selected_video_url']=='https://cdn.example/video.mp4'
    assert set(r['_mcp_fingerprints'])=={'storyboard','segments','scenes','shots'}
    assert set(r['_mcp_fingerprints']['shots'])=={'600','601'}


def test_update_content_duration_scene_refs_derived_prompt_preserves_media(board):
    original=copy.deepcopy(board.client.board['segments'][0])
    r=board.update('100','200',[{'shot_id':'600','content_rich':ACTOR+'抬眼，低声说："原台词。"','duration_sec':7}],[{'scene_id':'500','content_rich':SCENE}],fp(board),'update')
    write=[c for c in board.client.calls if c[0]=='PATCH'][0]
    assert write[1]=='/api/v1/pixa/short-drama/shots'
    assert write[2]['shots'][0]['duration_sec']==7
    seg=r['storyboard']['segments'][0]
    assert seg['estimated_duration_ms']==12000 and '低声说' in seg['video_prompt_text']
    assert all(seg[k]==original[k] for k in ['selected_video_url','selected_keyframe_url','selected_keyframe_id','model_code','summary'])
    assert r['verified'] and not r['generation_submitted']


def test_replay_does_not_repeat_write(board):
    args=('100','200',[{'shot_id':'600','duration_sec':6}],[],fp(board),'same')
    first=board.update(*args);assert board.update(*args)==first
    assert len([c for c in board.client.calls if c[0]=='PATCH'])==1


@pytest.mark.parametrize('duration',[0,16,True,1.5,'5'])
def test_invalid_durations_do_not_write(board,duration):
    with pytest.raises(ValueError,match='时长'):board.update('100','200',[{'shot_id':'600','duration_sec':duration}],[],fp(board),'bad')
    assert not [c for c in board.client.calls if c[0]!='GET']


@pytest.mark.parametrize('patch',[{'shot_id':'600','title':'不支持'},{'shot_id':'600','selected_video_url':''},{'shot_id':'999','duration_sec':3}])
def test_unsupported_fields_and_wrong_owner_rejected(board,patch):
    with pytest.raises(ValueError):board.update('100','200',[patch],[],fp(board),'bad')
    assert not [c for c in board.client.calls if c[0]!='GET']


@pytest.mark.parametrize('text',['@[外人](sd-asset://character/other)','错误sd-asset://character/char1',''])
def test_bad_or_foreign_asset_mentions_rejected(board,text):
    with pytest.raises(ValueError):board.update('100','200',[{'shot_id':'600','content_rich':text}],[],fp(board),'bad-ref')
    assert not [c for c in board.client.calls if c[0]!='GET']


def test_explicit_reference_change_is_allowed_and_verified(board):
    r=board.update('100','200',[{'shot_id':'600','content_rich':'@[资料](sd-asset://prop/prop1)放在桌上。'}],[],fp(board),'ref-change')
    refs_after=entities(r['storyboard'])[2]['600']['asset_refs']
    assert {x['relation_type'] for x in refs_after}=={'prop'}


def test_edit_rebuilds_own_refs_without_requiring_generated_inherited_scene(board):
    assert {x['relation_type'] for x in entities(board.client.board)[2]['600']['asset_refs']}=={'character','scene'}
    r=board.update('100','200',[{'shot_id':'600','content_rich':ACTOR+'收回目光。'}],[],fp(board),'own-refs')
    assert {x['relation_type'] for x in entities(r['storyboard'])[2]['600']['asset_refs']}=={'character'}


@pytest.mark.parametrize('duration',[4,6])
def test_nonempty_but_stale_video_prompt_body_or_cutpoints_is_unknown(board,duration):
    board.client.stale_prompt=True
    with pytest.raises(OperationUnknown):
        board.update('100','200',[{'shot_id':'600','content_rich':ACTOR+'换成新的明确动作。','duration_sec':duration}],[],fp(board),'stale-prompt')
    assert board.store.get_operation('stale-prompt')['status']=='UNKNOWN'


def test_stale_fingerprint_readonly_and_busy_stop_before_write(board):
    old=fp(board);board.client.board['segments'][0]['summary']='网页改动'
    with pytest.raises(ValueError,match='改变'):board.update('100','200',[{'shot_id':'600','duration_sec':3}],[],old,'stale')
    board.client.project['readonly']=True
    with pytest.raises(ValueError,match='只读'):board.update('100','200',[{'shot_id':'600','duration_sec':3}],[],fp(board),'readonly')
    board.client.project['readonly']=False;board.client.project['active_tasks']=[{'task_id':'busy'}]
    with pytest.raises(ValueError,match='运行'):board.update('100','200',[{'shot_id':'600','duration_sec':3}],[],fp(board),'busy')
    assert not [c for c in board.client.calls if c[0]!='GET']


@pytest.mark.parametrize('failure',['timeout','partial','no_save','corrupt_media'])
def test_uncertain_partial_or_protected_field_failure_stays_unknown(board,failure):
    setattr(board.client,'timeout' if failure=='timeout' else failure,True)
    with pytest.raises(OperationUnknown):board.update('100','200',[{'shot_id':'600','duration_sec':3}],[],fp(board),'unknown')
    assert board.store.get_operation('unknown')['status']=='UNKNOWN'
    with pytest.raises(OperationUnknown):board.update('100','200',[{'shot_id':'600','duration_sec':4}],[],fp(board),'other')
    assert len([c for c in board.client.calls if c[0]=='PATCH'])==1


@pytest.mark.parametrize('defaults',[False,True])
def test_create_segment_returns_actual_defaults_and_preserves_existing_media(board,defaults):
    board.client.default_shot=defaults
    r=board.create('100','200',1,fp(board),'segment')
    seg=r['storyboard']['segments'][0]
    assert seg['segment_id']=='701' and len(seg['scenes'][0]['shots'])==int(defaults)
    assert r['storyboard']['segments'][1]['selected_video_url']=='https://cdn.example/video.mp4'


def test_add_shot_and_delete_only_target_keep_other_fields(board):
    r=board.create('100','200',2,fp(board),'shot','400')
    assert [s['shot_id'] for s in r['storyboard']['segments'][0]['scenes'][0]['shots']]==['600','701','601']
    r=board.delete_shot('100','200','701',fp(board),'delete')
    assert set(entities(r['storyboard'])[2])=={'600','601'}
    assert r['storyboard']['segments'][0]['selected_video_url']=='https://cdn.example/video.mp4'


def test_delete_last_shot_and_cross_episode_edit_rejected(board):
    board.delete_shot('100','200','601',fp(board),'delete-one')
    with pytest.raises(ValueError,match='最后'):board.delete_shot('100','200','600',fp(board),'delete-last')
    with pytest.raises(ValueError,match='属于'):board.get('100','999')


def test_invalid_structure_position_does_not_create(board):
    with pytest.raises(ValueError):board.create('100','200',99,fp(board),'bad-order')
    with pytest.raises(ValueError):board.create('100','200',1,fp(board),'wrong-segment','999')
    assert not [c for c in board.client.calls if c[0]!='GET']


def test_batch_edit_cannot_cross_native_editor_segment_scope(board):
    created=board.create('100','200',2,fp(board),'new-segment')
    new_shot=created['storyboard']['segments'][1]['scenes'][0]['shots'][0]['shot_id']
    with pytest.raises(ValueError,match='同一片段'):
        board.update('100','200',[{'shot_id':'600','duration_sec':3},{'shot_id':new_shot,'duration_sec':3}],[],fp(board),'cross-segment')
    assert not [c for c in board.client.calls if c[0]=='PATCH']


@pytest.mark.parametrize('content',['普通病床说明',SCENE+'补文字',ACTOR,SCENE+SCENE])
def test_scene_field_requires_one_scene_token_not_free_text(board,content):
    with pytest.raises(ValueError,match='场景素材字段'):
        board.update('100','200',[],[{'scene_id':'500','content_rich':content}],fp(board),'bad-scene')
    assert not [c for c in board.client.calls if c[0]=='PATCH']


@pytest.mark.parametrize('endpoint,action',[
    ('/progress','create'),
    ('/episodes/storyboard','create'),
    ('/storyboard/detail','create'),
    ('/assets','update'),
])
def test_read_failure_before_storyboard_submission_is_failed_and_keeps_prior_claim(board,monkeypatch,endpoint,action):
    board.create('100','200',2,fp(board),'prior-success')
    current=fp(board);original_board=copy.deepcopy(board.client.board)
    prior_writes=len([c for c in board.client.calls if c[0]!='GET'])
    request=board.client.request
    def fail_read(method,path,*args,**kwargs):
        if method=='GET' and path.endswith(endpoint):
            raise APIError('官网请求返回 HTTP 567',status=567)
        return request(method,path,*args,**kwargs)
    monkeypatch.setattr(board.client,'request',fail_read)
    with pytest.raises(APIError,match='未提交写请求') as caught:
        if action=='create':board.create('100','200',3,current,'preflight-failed')
        else:board.update('100','200',[{'shot_id':'600','content_rich':ACTOR+'回头。'}],[],current,'preflight-failed')
    assert caught.value.definite and caught.value.status==567
    assert board.store.get_operation('preflight-failed')['status']=='FAILED'
    resource=canonical([board.store.account_key(),'script-storyboard','100','200'])
    assert board.store.claim_owner(resource)=='prior-success'
    assert board.client.board==original_board
    assert len([c for c in board.client.calls if c[0]!='GET'])==prior_writes
    monkeypatch.setattr(board.client,'request',request)
    result=board.create('100','200',3,current,'controlled-recovery')
    assert result['verified'] and len(result['storyboard']['segments'])==3


@pytest.mark.parametrize('endpoint',['/progress','/episodes/storyboard','/storyboard/detail'])
def test_storyboard_read_failure_after_post_remains_unknown_and_blocks_reposting(board,monkeypatch,endpoint):
    current=fp(board);request=board.client.request
    def fail_after_post(method,path,*args,**kwargs):
        if method=='GET' and path.endswith(endpoint) and board.client.next_id>700:
            raise APIError('官网请求返回 HTTP 567',status=567)
        return request(method,path,*args,**kwargs)
    monkeypatch.setattr(board.client,'request',fail_after_post)
    with pytest.raises(OperationUnknown):board.create('100','200',2,current,'post-read-unknown')
    assert board.store.get_operation('post-read-unknown')['status']=='UNKNOWN'
    resource=canonical([board.store.account_key(),'script-storyboard','100','200'])
    assert board.store.claim_owner(resource)=='post-read-unknown'
    assert len(board.client.board['segments'])==2
    monkeypatch.setattr(board.client,'request',request)
    with pytest.raises(OperationUnknown):board.create('100','200',3,fp(board),'must-not-repost')
    assert len([c for c in board.client.calls if c[0]=='POST'])==1


def test_storyboard_preflight_classification_does_not_change_other_script_operations(board,monkeypatch):
    request=board.client.request
    def fail_project_read(method,path,*args,**kwargs):
        if method=='GET' and path.endswith('/progress'):
            raise APIError('官网请求返回 HTTP 567',status=567)
        return request(method,path,*args,**kwargs)
    monkeypatch.setattr(board.client,'request',fail_project_read)
    with pytest.raises(OperationUnknown):
        board.script.update_outline('100',{'story_summary':'新概要'},'unused','outline-read-unknown')
    assert board.store.get_operation('outline-read-unknown')['status']=='UNKNOWN'
    assert not [c for c in board.client.calls if c[0]!='GET']
