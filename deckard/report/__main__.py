import argparse
from pathlib import Path
import sqlite3
import sys

from .model import build
from .server import serve


def main(argv=None):
    parser = argparse.ArgumentParser(description='Generate and explore a deterministic duplicate-file matrix.')
    commands = parser.add_subparsers(dest='command', required=True)
    generate = commands.add_parser('build', help='export a finished scan to a portable report')
    generate.add_argument('--index', type=Path, required=True)
    generate.add_argument('--output', type=Path, required=True)
    viewer = commands.add_parser('serve', help='view a generated report locally')
    viewer.add_argument('--report-dir', type=Path, required=True)
    viewer.add_argument('--port', type=int, default=8765)
    viewer.add_argument('--open', action='store_true')
    args = parser.parse_args(argv)
    if args.command == 'serve' and not 0 <= args.port <= 65535:
        parser.error('--port must be 0..65535')
    if args.command == 'build':
        build(args.index, args.output)
    else:
        serve(args.report_dir, args.port, args.open)
    return 0


def cli(argv=None):
    try:
        return main(argv)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, sqlite3.Error) as exc:
        print('Report: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(cli())
