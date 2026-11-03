"""Exercise the OpenSSH lifecycle with an executable transport double."""
import json
import os
from pathlib import Path
import sys
import threading

import pytest

from deckard import engine
from deckard.remote import AgentlessSource
from deckard.ssh import SSHTransport, validate_target


@pytest.fixture
def ssh_environment(tmp_path):
    ssh = tmp_path / 'ssh'
    ssh.write_text('#!' + sys.executable + '\n' + r'''
import json, os, pathlib, subprocess, sys, time
args = sys.argv[1:]
if '-N' in args:
    batch = 'BatchMode=yes' in args
    with open(os.environ['TEST_SSH_LOG'], 'a') as f:
        f.write(json.dumps({'batch': batch}) + '\n')
    if os.environ.get('TEST_PASSWORD'):
        if batch:
            print('Permission denied (publickey,password).', file=sys.stderr)
            sys.exit(255)
        answer = subprocess.check_output([os.environ['SSH_ASKPASS'], 'Password:'], env=os.environ)
        if answer.strip() != b'fixture-password':
            sys.exit(255)
    pathlib.Path(args[args.index('-S') + 1]).touch()
    while True:
        time.sleep(1)
else:
    assert 'DECKARD_AUTH_TOKEN' not in os.environ
    assert 'SSH_ASKPASS' not in os.environ
    os.execv('/bin/sh', ['sh', '-c', args[-1]])
''')
    ssh.chmod(0o700)
    helper = tmp_path / 'askpass'
    helper.write_text('#!/bin/sh\nprintf "fixture-password\\n"\n')
    helper.chmod(0o700)
    env = dict(os.environ, PATH=str(tmp_path) + os.pathsep + os.environ['PATH'],
               TEST_SSH_LOG=str(tmp_path / 'calls'), SSH_ASKPASS=str(helper),
               DECKARD_AUTH_SOCKET='test-only', DECKARD_AUTH_TOKEN='test-only-token')
    return env


@pytest.mark.parametrize('password', [False, True])
def test_key_first_then_interactive_fallback_and_owned_cleanup(tmp_path, ssh_environment, password):
    env = ssh_environment
    if password:
        env['TEST_PASSWORD'] = '1'
    source_root = tmp_path / 'remote'; source_root.mkdir()
    (source_root / 'data').write_bytes(b'read-only')
    transport = SSHTransport('host', environment=env)
    control = Path(transport.control); master = transport.process
    source = AgentlessSource(str(source_root), transport)
    assert source.info['agentless'] and source.root == os.fsencode(source_root)
    session = source.main.process
    source.close()
    attempts = [json.loads(line)['batch'] for line in Path(env['TEST_SSH_LOG']).read_text().splitlines()]
    assert attempts == ([True, False] if password else [True])
    assert not control.parent.exists()
    assert master.poll() is not None and session.poll() is not None
    assert [p.name for p in source_root.iterdir()] == ['data']


def test_borrowed_master_is_not_removed(tmp_path, ssh_environment):
    socket = tmp_path / 'existing-control'; socket.touch()
    transport = SSHTransport('host', environment=ssh_environment, existing_control=socket)
    source = AgentlessSource(str(tmp_path), transport)
    source.close()
    assert socket.exists()
    assert not Path(ssh_environment['TEST_SSH_LOG']).exists()


def test_cancelled_authentication_cleans_owned_runtime(ssh_environment):
    cancelled = threading.Event(); cancelled.set()
    with pytest.raises(KeyboardInterrupt):
        SSHTransport('host', environment=ssh_environment, cancelled=cancelled)


def test_username_host_split_cannot_create_ssh_option():
    with pytest.raises(ValueError):
        validate_target('user@-oProxyCommand=bad')


def test_cancelled_bootstrap_terminates_worker(tmp_path, monkeypatch):
    cancelled = threading.Event(); cancelled.set()
    monkeypatch.setattr(engine, 'CANCEL_EVENT', cancelled)
    with pytest.raises(KeyboardInterrupt):
        AgentlessSource(str(tmp_path), local_python=sys.executable)
    assert not list(tmp_path.iterdir())
