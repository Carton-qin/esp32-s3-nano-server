import socket
import ssl
import json
import gc

class HttpResponse:
    def __init__(self, status_code, headers, text, set_cookies=None):
        self.status_code = status_code
        self.headers = headers
        self.text = text
        self.set_cookies = set_cookies or []

    def json(self):
        try:
            return json.loads(self.text)
        except Exception:
            return {}

    def close(self):
        pass

def request(method, url, headers=None, data=None, timeout=15, max_redirects=3):
    current_url = url
    current_method = method.upper()
    redirects_left = max_redirects

    while True:
        gc.collect()
        proto, _, host_path = current_url.partition("://")
        if not host_path:
            proto, host_path = "http", proto

        is_ssl = proto.lower() == "https"
        default_port = 443 if is_ssl else 80

        host_part, _, path_part = host_path.partition("/")
        path = "/" + path_part

        if ":" in host_part:
            host, port_str = host_part.split(":", 1)
            port = int(port_str)
        else:
            host = host_part
            port = default_port

        s = socket.socket()
        s.settimeout(timeout)
        try:
            ai = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)[0][-1]
            s.connect(ai)
            if is_ssl:
                s = ssl.wrap_socket(s, server_hostname=host)
        except Exception as conn_err:
            try:
                s.close()
            except Exception:
                pass
            raise conn_err

        # Prepare body
        req_headers = dict(headers) if headers else {}
        if data is None:
            body_bytes = b""
        elif isinstance(data, str):
            body_bytes = data.encode("utf-8")
        elif isinstance(data, bytes):
            body_bytes = data
        else:
            body_bytes = json.dumps(data).encode("utf-8")
            if "Content-Type" not in req_headers and "content-type" not in req_headers:
                req_headers["Content-Type"] = "application/json"

        # Build HTTP/1.1 headers
        lines = [
            "{} {} HTTP/1.1".format(current_method, path),
            "Host: {}".format(host)
        ]

        hdr_lower = {k.lower(): k for k in req_headers.keys()}
        if "connection" not in hdr_lower:
            lines.append("Connection: close")
        if "content-length" not in hdr_lower and (current_method in ("POST", "PUT", "PATCH") or len(body_bytes) > 0):
            lines.append("Content-Length: {}".format(len(body_bytes)))
        if "user-agent" not in hdr_lower:
            lines.append("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
        if "accept" not in hdr_lower:
            lines.append("Accept: application/json, text/plain, text/html, */*")
        if "accept-encoding" not in hdr_lower:
            lines.append("Accept-Encoding: identity")

        for k, v in req_headers.items():
            if v:
                lines.append("{}: {}".format(k, v))

        header_bytes = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")
        s.write(header_bytes + body_bytes)

        # Parse Status Line
        line = s.readline()
        if not line:
            s.close()
            raise Exception("服务端未返回有效响应")

        status_parts = line.decode("utf-8", "ignore").split(None, 2)
        status_code = int(status_parts[1]) if len(status_parts) > 1 else 0

        # Parse Response Headers
        resp_headers = {}
        set_cookies = []
        while True:
            line = s.readline()
            if not line or line in (b"\r\n", b"\n"):
                break
            h_str = line.decode("utf-8", "ignore").strip()
            if ":" in h_str:
                hk, hv = h_str.split(":", 1)
                hk_lower = hk.strip().lower()
                hv_clean = hv.strip()
                resp_headers[hk_lower] = hv_clean
                if hk_lower == "set-cookie":
                    set_cookies.append(hv_clean)

        # Handle Redirects (301, 302, 303, 307, 308)
        if status_code in (301, 302, 303, 307, 308) and "location" in resp_headers and redirects_left > 0:
            s.close()
            new_location = resp_headers["location"]
            if new_location.startswith("/"):
                new_location = "{}://{}{}".format(proto, host_part, new_location)
            current_url = new_location
            redirects_left -= 1
            if status_code in (302, 303) and current_method != "GET":
                current_method = "GET"
                data = None
            continue

        # Read Body (limit to 12KB to prevent MicroPython OOM)
        content_len = int(resp_headers.get("content-length", -1))
        body = b""
        max_bytes = 12288
        if content_len >= 0:
            bytes_to_read = min(content_len, max_bytes)
            while len(body) < bytes_to_read:
                chunk = s.read(min(1024, bytes_to_read - len(body)))
                if not chunk:
                    break
                body += chunk
        else:
            while len(body) < max_bytes:
                chunk = s.read(1024)
                if not chunk:
                    break
                body += chunk

        s.close()
        return HttpResponse(status_code, resp_headers, body.decode("utf-8", "ignore"), set_cookies=set_cookies)

def get(url, headers=None, **kwargs):
    return request("GET", url, headers=headers, **kwargs)

def post(url, headers=None, data=None, **kwargs):
    return request("POST", url, headers=headers, data=data, **kwargs)
