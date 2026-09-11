"""Build the public, credential-free Scriptable script deterministically."""
from pathlib import Path
import json

BASE = Path(__file__).resolve().parent.parent

def bundle():
    core = (BASE / 'integrations/iphone/forwarder-core.cjs').read_text(encoding='utf-8')
    core = core.replace("if (typeof module!=='undefined') module.exports=PhoneForwarder;", '')
    runtime = (BASE / 'integrations/iphone/scriptable-runtime.js').read_text(encoding='utf-8')
    return '// ClubOps 登录转发 v1 — Scriptable on iPhone\n// No credentials are embedded. Configure in the app after import.\n' + core + '\n' + runtime

if __name__ == '__main__':
    target = BASE / 'static/clubops-iphone.js'
    target.write_text(bundle(), encoding='utf-8', newline='\n')
    (BASE / 'integrations/login-relay/iphone-client.mjs').write_text(
        '// Generated public client, no pairing credentials.\nexport default ' + json.dumps(bundle(), ensure_ascii=False) + ';\n',
        encoding='utf-8', newline='\n')
    print('Built public Scriptable client: static/clubops-iphone.js')
