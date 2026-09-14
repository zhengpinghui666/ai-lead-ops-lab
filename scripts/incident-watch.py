"""Resident file-event listener. It queues fault metadata, never starts collectors or messages customers."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import time
import uuid

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from incident_bridge import Bridge,health


class DirectoryEvents:
    def __init__(self,path):
        if os.name!='nt':
            raise RuntimeError('This resident watcher uses Windows directory notifications')
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.FindFirstChangeNotificationW.argtypes=[wintypes.LPCWSTR,wintypes.BOOL,wintypes.DWORD]
        self.kernel.FindFirstChangeNotificationW.restype=wintypes.HANDLE
        self.kernel.FindNextChangeNotification.argtypes=[wintypes.HANDLE]
        self.kernel.FindCloseChangeNotification.argtypes=[wintypes.HANDLE]
        self.kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        self.handle=self.kernel.FindFirstChangeNotificationW(str(path),False,0x1|0x8|0x10)
        if self.handle==ctypes.c_void_p(-1).value:
            raise OSError('Directory notification unavailable')

    def wait(self):
        result=self.kernel.WaitForSingleObject(self.handle,10000)
        if result==0:
            if not self.kernel.FindNextChangeNotification(self.handle):
                raise OSError('Directory notification rearm failed')
            time.sleep(0.25)  # coalesce SQLite commit writes; no model call
        elif result!=258:
            raise OSError('Directory notification wait failed')

    def close(self):
        self.kernel.FindCloseChangeNotification(self.handle)


def publish_status(directory, state):
    """A brief Windows reader/replace collision must not kill the watcher."""
    target=directory/'status.json'
    temp=directory/('status-'+uuid.uuid4().hex+'.tmp')
    try:
        temp.write_text(json.dumps(state),encoding='utf8')
        for attempt in range(3):
            try:
                temp.replace(target)
                return True
            except PermissionError:
                if attempt<2:time.sleep(0.05)
        return False
    except OSError:
        return False
    finally:
        try:temp.unlink(missing_ok=True)
        except OSError:pass


def pulse(bridge,port):
    error=None
    try:
        bridge.observe(health(bridge.data_dir,port))
        bridge.dispatch()
    except Exception as exc:
        error=type(exc).__name__
    try:state=bridge.status()
    except Exception as exc:
        state=dict(mode='unavailable',enabled=False)
        error=type(exc).__name__
    return publish_status(bridge.directory,dict(state,heartbeat_at=time.time(),pid=os.getpid(),watcher_error=error))


def run(bridge,port):
    import msvcrt
    lock=(bridge.directory/'watcher.lock').open('a+b')
    lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:
        lock.close()
        raise RuntimeError('Incident watcher already running')
    events=DirectoryEvents(bridge.data_dir)
    try:
        while True:
            pulse(bridge,port)
            events.wait()
    finally:
        events.close();lock.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,default=BASE/'data')
    parser.add_argument('--port',type=int,default=8765)
    commands=parser.add_subparsers(dest='command',required=True)
    setup=commands.add_parser('configure');setup.add_argument('--thread',required=True);setup.add_argument('--codex',required=True)
    commands.add_parser('watch');commands.add_parser('status');commands.add_parser('self-test')
    hold=commands.add_parser('hold');hold.add_argument('--seconds',type=int,required=True)
    ack=commands.add_parser('ack');ack.add_argument('--id',required=True);ack.add_argument('--state',choices=['received','resolved','needs_user','failed'],required=True)
    args=parser.parse_args();bridge=Bridge(args.data_dir)
    if args.command=='configure':bridge.configure(args.thread,args.codex)
    elif args.command=='watch':return run(bridge,args.port)
    elif args.command=='hold':bridge.hold(args.seconds)
    elif args.command=='self-test':bridge.self_test();print(json.dumps({'delivery_id':bridge.dispatch()}))
    elif args.command=='ack':
        report=health(args.data_dir,args.port) if args.state=='resolved' else None
        bridge.acknowledge(args.id,args.state,report)
    print(json.dumps(bridge.status()))


if __name__=='__main__':
    main()
