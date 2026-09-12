import json
import socket
import struct


def request(path, payload, timeout=35, max_bytes=2_000_000):
    def exact(sock, n):
        data=bytearray()
        while len(data)<n:
            chunk=sock.recv(n-len(data))
            if not chunk: raise ConnectionError('worker_closed_connection')
            data.extend(chunk)
        return bytes(data)
    body=json.dumps(payload,ensure_ascii=False).encode()
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(timeout)
        try: sock.connect(str(path))
        except OSError as exc:
            from .execution import Unavailable
            raise Unavailable('worker_connect_failed_before_send:'+str(exc)) from exc
        sock.sendall(struct.pack('!I',len(body))+body)
        n=struct.unpack('!I',exact(sock,4))[0]
        if n>max_bytes:raise ValueError('worker_response_too_large')
        return json.loads(exact(sock,n))
