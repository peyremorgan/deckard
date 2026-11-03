"""Local SSH_ASKPASS executable. Credentials travel over private local IPC only."""
import json
import os
import socket
import sys


def main():
    request = {'token': os.environ['DECKARD_AUTH_TOKEN'],
               'prompt': sys.argv[1] if len(sys.argv) > 1 else 'SSH authentication',
               'kind': os.environ.get('SSH_ASKPASS_PROMPT', ''),
               'purpose': os.environ.get('DECKARD_AUTH_PURPOSE', 'scan')}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(os.environ['DECKARD_AUTH_SOCKET'])
        connection.sendall((json.dumps(request) + '\n').encode())
        data = bytearray()
        while b'\n' not in data:
            part = connection.recv(4096)
            if not part or len(data) > 65536:
                return 1
            data.extend(part)
    result = json.loads(bytes(data).split(b'\n', 1)[0])
    if result.get('response') is None:
        return 1
    sys.stdout.write(result['response'] + '\n')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError):
        raise SystemExit(1)
