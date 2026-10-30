"""让用户直接在本机终端导入官网Authorization，不回显凭证。"""

from getpass import getpass
import json

from .client import APIError, Client
from .store import Store


def main():
    store = Store()
    client = Client(store)
    try:
        print("导入Pavo官网Authorization。请粘贴Bearer加空格加凭证，输入不会回显。")
        authorization = getpass("Authorization：").strip()
        result = client.import_authorization(authorization)
        print("认证完成：" + json.dumps(result, ensure_ascii=False))
    except (ValueError, APIError):
        print("认证未完成。请核对官网Authorization、有效期和网络后重试；原有会话未被替换。")
        raise SystemExit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
