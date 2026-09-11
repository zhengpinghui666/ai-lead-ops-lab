"""Install an explicit, pinned optional HTTP reader runtime. Never logs in/collects."""
import hashlib
from pathlib import Path
import subprocess
import sys
import urllib.request

BASE = Path(__file__).resolve().parents[1]
SHA = 'df8aced70e476ae3330fa913186f3207b4843201'
FILES = {'src/encrypt/aBogus.py': '6a0b5b8b3666522ddb8e14ee760535455e8734e576d4707132d3ff6992e6d355',
         'license': '3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986'}
F2_SHA = '7dab3e2ffffaa2535834d28fca99dbc2e89fa9d3'
F2_FILES = {'f2/utils/abogus.py': '82cc97b63aab2ac80a5c312fe52850ccc74d6fdffe6edaee4e58add14083a3c3',
            'LICENSE': 'c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4'}


def setup():
    target = BASE / '.tools' / 'tiktok-params' / SHA
    for name, expected in FILES.items():
        file = target / name
        raw = file.read_bytes() if file.is_file() else urllib.request.urlopen(
            f'https://raw.githubusercontent.com/JoeanAmier/TikTokDownloader/{SHA}/{name}', timeout=25).read(100001)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Upstream source integrity mismatch')
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(raw)
    (target / 'SOURCE.txt').write_text(
        f'TikTokDownloader by JoeanAmier, commit {SHA}, GPL-3.0; see license.\n'
        'Only aBogus.py is loaded; original bytes retained. ClubOps supplies src.custom.USERAGENT\n'
        'in the isolated worker and invokes ABogus(user_agent=...).get_value(...).\n'
        'The upstream douyin_params.py constant-return wrapper and bundled JS are NOT used.\n', encoding='utf-8')
    f2_target = BASE / '.tools/f2-params' / F2_SHA
    for name, expected in F2_FILES.items():
        file = f2_target / name
        raw = file.read_bytes() if file.is_file() else urllib.request.urlopen(
            f'https://raw.githubusercontent.com/Johnserf-Seed/f2/{F2_SHA}/{name}', timeout=25).read(100001)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('F2 source integrity mismatch')
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(raw)
    (f2_target / 'SOURCE.txt').write_text(f'F2 by JohnserfSeed, {F2_SHA}, Apache-2.0; see LICENSE.\n'
        'Original abogus.py retained. GET options supplied explicitly as [0,1,8].\n', encoding='utf-8')
    environment = BASE / '.tools' / 'douyin-http-venv'
    python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if not python.is_file():
        subprocess.run([sys.executable, '-m', 'venv', str(environment)], check=True)
    subprocess.run([str(python), '-m', 'pip', 'install', '--disable-pip-version-check',
                    '-r', str(BASE / 'config/collection-http-requirements.txt')], check=True)
    print('Optional HTTP runtime installed. No account or platform request was made.')


if __name__ == '__main__':
    setup()
