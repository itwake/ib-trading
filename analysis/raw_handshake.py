import socket
import struct
import time

s = socket.create_connection(("127.0.0.1", 4001), timeout=10)
print("tcp connected")

# v100+ 握手: b"API\0" + 长度前缀的版本范围字符串
payload = b"v100..187"
msg = b"API\0" + struct.pack(">I", len(payload)) + payload
s.sendall(msg)
print("handshake sent, waiting for reply...")

s.settimeout(20)
try:
    data = s.recv(4096)
    print("recv %d bytes: %r" % (len(data), data[:200]))
except socket.timeout:
    print("NO REPLY within 20s")
s.close()
