"""burp-caido-syncer: Sync Burp Suite projects into Caido."""

__version__ = "0.3.0"

from .parsers import parse_request_line, parse_status_code, sha1
from .readers import count_ndjson_lines, read_burp_xml, read_ndjson
from .replay_seeder import seed_replay
from .scope_seeder import seed_scope
from .seeder import seed
from .sitemap import SitemapBuilder


def main():
    from .cli import main as _main
    _main()
