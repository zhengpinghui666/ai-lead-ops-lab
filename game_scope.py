"""One game vocabulary for discovery, assets, groups and intent evidence."""
import json
from pathlib import Path
import re

CONFIG = json.loads(Path(__file__).with_name('game_scope.json').read_text('utf-8'))
ALIASES = CONFIG['aliases']
GAME_PATTERN = re.compile('|'.join(map(re.escape, ALIASES))+'|'+CONFIG['isolated_pattern'], re.I)


def game_quote(quote, original):
    """The quote must enclose a full game match in its actual source context.

    A standalone 瓦 or #瓦 is valid; a substring cut from 瓦片/瓦工 is not.
    Do not re-run the boundary regex on a stripped single-character quote.
    """
    start = original.find(quote)
    return bool(quote and start >= 0 and any(start <= m.start() and m.end() <= start+len(quote)
                                           for m in GAME_PATTERN.finditer(original)))
