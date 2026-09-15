"""Deliver local incident metadata through the installed Codex app MCP tool.

Only send_message_to_thread is allowed, always to the explicitly configured
existing task. A successful app response is not a receiver acknowledgement.
No models, permissions, business settings or customer channels are modified.
"""
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

MAX_BYTES=8*1024*1024
PIPE_PREFIX='\\\\.\\pipe\\codex-browser-use-'


def resolve_server(config, *, cache_root=None):
    configured=Path(config['server'])
    if configured.is_file():return configured
    # Desktop upgrades remove versioned WindowsApps paths. Resolve only the
    # same installed bundled plugin, never an arbitrary executable on PATH.
    root=Path(cache_root) if cache_root is not None else Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'plugins/cache/openai-bundled/codex-app-tools'
    candidates=[]
    for path in root.glob('*/server.mjs'):
        version=path.parent.name
        if path.is_file() and re.fullmatch(r'\d+(?:\.\d+){1,3}',version):
            candidates.append((tuple(map(int,version.split('.'))),path))
    if not candidates:raise OSError('Installed app adapter unavailable')
    return max(candidates,key=lambda item:item[0])[1]


def resolve_pipe(config):
    """The per-launch pipe can rotate when the desktop app restarts."""
    candidates=[]
    if os.name=='nt':
        try:candidates=[r'\\.\pipe'+chr(92)+name for name in os.listdir(r'\\.\pipe'+chr(92))
                        if re.fullmatch(r'codex-browser-use-[0-9a-f-]{36}',name)]
        except OSError:pass
    preferred=os.environ.get('CODEX_APP_TOOLS_PIPE_PATH') or config.get('pipe','')
    if preferred in candidates:return preferred
    if len(candidates)==1:return candidates[0]
    if not candidates and preferred.startswith(PIPE_PREFIX):return preferred
    raise OSError('App pipe unavailable or ambiguous')


class Client:
    def __init__(self, config):
        node=Path(config['node']);server=resolve_server(config)
        if not node.is_file() or not server.is_file():raise OSError('App adapter unavailable')
        env={**os.environ,'CODEX_APP_TOOLS_PIPE_PATH':resolve_pipe(config)}
        self.process=subprocess.Popen([str(node),str(server)],stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        self.messages=queue.Queue(maxsize=32)
        self.next_id=0
        threading.Thread(target=self.read,daemon=True).start()

    def read(self):
        try:
            while True:
                line=self.process.stdout.readline(MAX_BYTES+1)
                if not line or len(line)>MAX_BYTES:break
                data=json.loads(line)
                if 'id' in data:self.messages.put_nowait(data)
        except (ValueError,OSError,queue.Full):pass
        try:self.messages.put_nowait(None)
        except queue.Full:pass

    def write(self,data):
        encoded=(json.dumps(data,ensure_ascii=False)+'\n').encode('utf8')
        if len(encoded)>MAX_BYTES:raise ValueError('Request too large')
        self.process.stdin.write(encoded);self.process.stdin.flush()

    def request(self,method,params):
        self.next_id+=1;rid=self.next_id
        self.write(dict(jsonrpc='2.0',id=rid,method=method,params=params))
        deadline=time.monotonic()+15
        while True:
            data=self.messages.get(timeout=max(.01,deadline-time.monotonic()))
            if data is None:raise OSError('App adapter disconnected')
            if data.get('id')==rid:
                if 'error' in data:raise RuntimeError('App request rejected')
                return data['result']
            if time.monotonic()>deadline:raise TimeoutError('App response timed out')

    def close(self):
        try:self.process.stdin.close()
        except OSError:pass
        try:self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:self.process.kill();self.process.wait(timeout=2)
        self.process.stdout.close()


def push(config,thread_id,prompt,*,client_factory=Client):
    client=None;attempted=False
    if not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',thread_id):
        return dict(status='not_sent',error='invalid_target')
    try:
        client=client_factory(config)
        client.request('initialize',dict(protocolVersion='2025-03-26',capabilities={},
            clientInfo=dict(name='clubops-incident-bridge',version='1.0')))
        client.write(dict(jsonrpc='2.0',method='notifications/initialized'))
        catalog=client.request('tools/list',{})
        if not any(t.get('name')=='send_message_to_thread' for t in catalog.get('tools',[])):
            return dict(status='not_sent',error='app_push_tool_unavailable')
        attempted=True
        result=client.request('tools/call',dict(name='send_message_to_thread',
            arguments=dict(threadId=thread_id,prompt=prompt),_meta=dict(threadId=thread_id)))
        if result.get('isError'):return dict(status='unknown',error='app_push_rejected_or_unknown')
        for item in result.get('content',[]):
            if item.get('type')!='text':continue
            try:receipt=json.loads(item.get('text',''))
            except ValueError:continue
            if isinstance(receipt,dict) and receipt.get('threadId')==thread_id:
                return dict(status='pushed',error=None)
        return dict(status='unknown',error='app_push_receipt_unknown')
    except (OSError,ValueError,KeyError,RuntimeError,TimeoutError,queue.Empty):
        return dict(status='unknown' if attempted else 'not_sent',
                    error='app_push_outcome_unknown' if attempted else 'app_push_unavailable')
    finally:
        if client is not None:client.close()
