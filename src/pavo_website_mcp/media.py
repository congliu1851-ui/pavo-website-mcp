"""固定公网IP连接，逐跳检查，防止媒体URL造成内网请求或DNS重绑定。"""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit

import httpx


def safe_url(url):
    if not isinstance(url, str) or any(ord(c) < 32 for c in url):
        raise ValueError("媒体地址无效")
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("媒体地址须为不含账号和片段的HTTP(S)地址")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if port not in {80, 443} or "%" in parsed.hostname:
            raise ValueError("媒体地址端口或主机无效")
        try:
            addresses = [str(ipaddress.ip_address(parsed.hostname))]
        except ValueError:
            addresses = list(dict.fromkeys(info[4][0] for info in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)))
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise ValueError("媒体地址指向本机或非公网网络，已拒绝")
    except (OSError, UnicodeError) as e:
        raise ValueError("无法安全解析媒体主机") from None
    return parsed, addresses, port


class ResponseStream(httpx.SyncByteStream):
    def __init__(self, response, conn):
        self.response, self.conn = response, conn
    def __iter__(self):
        while block := self.response.read(256 * 1024):
            yield block
    def close(self):
        self.response.close()
        self.conn.close()


class PublicTransport(httpx.BaseTransport):
    def handle_request(self, request):
        parsed, addresses, port = safe_url(str(request.url))
        conn = http.client.HTTPConnection(parsed.hostname, port, timeout=180)
        try:
            sock = socket.create_connection((addresses[0], port), timeout=180)
            if parsed.scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
            conn.sock = sock
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            conn.putrequest(request.method, path, skip_host=True, skip_accept_encoding=True)
            for key, value in request.headers.items():
                if key.lower() in {"authorization", "cookie", "proxy-authorization"}:
                    raise ValueError("媒体请求不得携带登录凭证")
                conn.putheader(key, value)
            conn.endheaders()
            for block in request.stream:
                conn.send(block)
            response = conn.getresponse()
            return httpx.Response(response.status, headers=response.getheaders(), stream=ResponseStream(response, conn))
        except Exception:
            conn.close()
            raise httpx.TransportError("媒体公网连接未完成") from None
