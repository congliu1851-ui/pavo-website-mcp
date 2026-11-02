import httpx
import pytest

from pavo_website_mcp.client import APIError, Client, business_error_hint
from pavo_website_mcp.store import Store


@pytest.mark.parametrize('message,expected',[
    ('请求过于频繁，请稍后重试','请求限流'),
    ('任务仍在运行中，请稍后重试','任务占用或繁忙'),
    ('All shots must belong to the same segment','片段或归属范围约束'),
    ('当前状态不允许此操作','状态或版本冲突'),
    ('视频duration超限','时长校验'),
    ('maximum length exceeded','内容长度限制'),
    ('permission denied','权限限制'),
    ('invalid argument','参数或资源校验'),
    ('Please try again later','暂时性业务错误'),
    ('未识别的业务失败',''),
    ({'secret':'不得回显'},''),
])
def test_business_hint_only_returns_fixed_categories(message,expected):
    assert business_error_hint(message)==expected


@pytest.mark.parametrize('field',['message','error_message'])
def test_business_error_diagnostic_drops_credentials_and_sensitive_body(tmp_path,field):
    message='任务正在处理中。Authorization: Bearer private-token；敏感剧本对白：我的账户号码123456。https://example.invalid/private?key=secret'
    store=Store(tmp_path);store.save_session('test-session',{})
    transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'code':'067012',field:message}))
    client=Client(store,transport=transport)
    with pytest.raises(APIError) as err:client.request('GET','/api/test')
    text=str(err.value)
    assert 'HTTP 200' in text and '067012' in text and '任务占用或繁忙' in text
    assert all(secret not in text for secret in ['private-token','敏感剧本','账户号码','123456','example.invalid','secret','Authorization'])
    assert err.value.definite and err.value.status==200
    client.close()
