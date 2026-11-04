"""Portable SQLite report generation and bounded, on-demand matrix queries."""
import base64
import collections
import contextlib
import json
import math
import os
from pathlib import Path
import sqlite3

from . import FORMAT_VERSION


def encoded(value):
    return base64.b64encode(value).decode('ascii')


def label(value):
    return value.decode('utf-8', 'replace')


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def parent(path):
    return path.rpartition(b'/')[0]


def contains(folder, path):
    return not folder or path == folder or path.startswith(folder + b'/')


def common_folder(a, b):
    if a == b:
        return a
    common = os.path.commonprefix((a + b'/', b + b'/'))
    return common.rstrip(b'/') if common.endswith(b'/') else parent(common)


def build(index, output, progress=print):
    """Export one consistent index snapshot; do not access the source volume.

    Output ordering, IDs and serialization depend only on input data. There is
    no wall-clock generation timestamp, random ID, CDN, or external dependency.
    SQLite is an artifact format; its bytes are reproducible on the same SQLite
    version. JSON exports and API results have canonical ordering/serialization.
    """
    output = Path(output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    temporary = output / 'report.sqlite3.building'
    if temporary.exists():
        raise ValueError('unfinished report exists: ' + str(temporary))
    with contextlib.closing(readonly(index)) as source:
        source.execute('BEGIN')
        metadata = dict(source.execute('SELECT key,value FROM meta'))
        if metadata.get('phase') != 'complete':
            raise ValueError('finish the scan before generating a report')
        if metadata.get('report_inventory_version') != '1':
            raise ValueError('allocated sizes / skipped-entry coverage missing; run scan --refresh')
        if metadata.get('last_min_size', '0') != '0':
            raise ValueError('full-folder coverage requires a scan with --min-size 0')
        if source.execute('SELECT 1 FROM files WHERE allocated IS NULL LIMIT 1').fetchone():
            raise ValueError('allocated sizes missing; run scan --refresh on the source device')
        progress('Loading directory hierarchy')
        paths = [bytes(row[0]) for row in source.execute('SELECT path FROM dirs ORDER BY path')]
        if not paths or paths[0] != b'':
            raise ValueError('index has no root directory')
        # Directory maps only: five million file paths are streamed, never held.
        ids = {path: i + 1 for i, path in enumerate(paths)}
        # Direct totals, then one bottom-up pass. Exclusive counts are placed
        # at the LCA of each content group's paths. A subtree with exclusive=0
        # has a retained occurrence of EVERY content outside that subtree.
        totals = [[0, 0, 0, 0, 0] for _ in paths]  # logical, allocated, files, exclusive, blocked
        parents = [ids.get(parent(path), 0) if path else 0 for path in paths]
        if any(path and parent(path) not in ids for path in paths):
            raise ValueError('incomplete directory hierarchy; refresh the scan')
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        db = sqlite3.connect(temporary)
        try:
            db.executescript('''
                PRAGMA journal_mode=OFF;
                PRAGMA synchronous=OFF;
                PRAGMA cache_size=-262144;
                CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT) WITHOUT ROWID;
                CREATE TABLE nodes(id INTEGER PRIMARY KEY,path BLOB NOT NULL UNIQUE,parent INTEGER,
                    logical INTEGER,allocated INTEGER,files INTEGER,exclusive INTEGER,blocked INTEGER);
                CREATE TABLE entries(id INTEGER PRIMARY KEY,parent INTEGER,name BLOB,
                    logical INTEGER,allocated INTEGER,content INTEGER);
                CREATE TABLE contents(id INTEGER PRIMARY KEY,occurrences INTEGER,
                    logical INTEGER,sha256 BLOB,source_identity TEXT);
            ''')
            db.executemany('INSERT INTO nodes(id,path,parent) VALUES (?,?,?)',
                           ((i + 1, path, parents[i]) for i, path in enumerate(paths)))
            progress('Streaming file contents and allocated sizes')
            content_id = file_id = content_count = 0
            previous_key = None
            lca = b''
            batch, groups = [], []
            hardlink_paths = 0
            def finish_group():
                if content_count:
                    groups.append((content_id, content_count, previous_key[0], bytes.fromhex(previous_key[1]), None))
                    totals[ids[lca] - 1][3] += content_count
            query = '''SELECT path,size,allocated,full,error,nlink FROM files
                       ORDER BY size,full,path'''
            for path, size, allocated, digest, error, links in source.execute(query):
                path = bytes(path)
                directory, _, name = path.rpartition(b'/')
                if directory not in ids:
                    raise ValueError('file parent absent from inventory')
                size, allocated = int(size), int(allocated)
                if size < 0 or allocated < 0:
                    raise ValueError('negative file size in index')
                key = (size, digest) if digest is not None and error is None else None
                if key is not None and key != previous_key:
                    finish_group()
                    content_count = 0
                    if key is not None:
                        content_id += 1
                        lca = directory
                    previous_key = key
                if key is not None:
                    lca = common_folder(lca, directory)
                    content_count += 1
                    group_id = content_id
                else:
                    group_id = 0
                    totals[ids[directory] - 1][3] += 1
                t = totals[ids[directory] - 1]
                t[0] += size
                t[1] += allocated
                t[2] += 1
                t[4] += int(error is not None)
                hardlink_paths += int(links > 1)
                file_id += 1
                batch.append((file_id, ids[directory], name, size, allocated, group_id))
                if len(batch) >= 8192:
                    db.executemany('INSERT INTO entries VALUES (?,?,?,?,?,?)', batch)
                    db.executemany('INSERT INTO contents VALUES (?,?,?,?,?)', groups)
                    batch.clear(); groups.clear()
                    if file_id % 262144 == 0:
                        progress('Exported {:,} files'.format(file_id))
            finish_group()
            db.executemany('INSERT INTO entries VALUES (?,?,?,?,?,?)', batch)
            db.executemany('INSERT INTO contents VALUES (?,?,?,?,?)', groups)
            # Aliases of one native object need no additional full-file read.
            # The scanner intentionally leaves hardlink-only objects unhashed.
            db.execute('CREATE INDEX entries_parent ON entries(parent,name)')
            aliases = source.execute('''SELECT path,size,mtime,ctime,dev,ino FROM files
                WHERE nlink>1 AND ino<>'0' AND full IS NULL AND error IS NULL
                ORDER BY dev,ino,size,mtime,ctime,path''')
            alias_key, alias_count, alias_lca = None, 0, b''
            def finish_alias():
                if alias_count:
                    db.execute('INSERT INTO contents VALUES (?,?,?,?,?)',
                               (content_id, alias_count, alias_key[0], None, json.dumps(alias_key)))
                    totals[ids[alias_lca] - 1][3] += alias_count
            for path, size, mtime, ctime, dev, ino in aliases:
                path = bytes(path)
                directory, _, name = path.rpartition(b'/')
                key = (size, mtime, ctime, dev, ino)
                if key != alias_key:
                    finish_alias()
                    content_id += 1
                    alias_key, alias_count, alias_lca = key, 0, directory
                alias_count += 1
                alias_lca = common_folder(alias_lca, directory)
                directory_id = ids[directory]
                changed = db.execute('UPDATE entries SET content=? WHERE parent=? AND name=?',
                                     (content_id, directory_id, name))
                if changed.rowcount != 1:
                    raise ValueError('hardlink alias absent from exported entries')
                totals[directory_id - 1][3] -= 1
            finish_alias()
            skipped_count = issue_count = 0
            for query in ('SELECT path FROM skipped', 'SELECT path FROM issues'):
                for (raw_path,) in source.execute(query):
                    p = bytes(raw_path)
                    if query.endswith('skipped'):
                        skipped_count += 1
                    else:
                        issue_count += 1
                    # Directory errors can name the directory itself.
                    p = p if p in ids else parent(p)
                    while p not in ids and p:
                        p = parent(p)
                    totals[ids[p] - 1][4] += 1
            for path, state in source.execute('SELECT path,state FROM dirs WHERE state<>1'):
                totals[ids[bytes(path)] - 1][4] += 1
            progress('Aggregating folder coverage')
            for i in sorted(range(len(paths)), key=lambda i: (paths[i].count(b'/') + bool(paths[i]), paths[i]), reverse=True):
                if parents[i]:
                    p = totals[parents[i] - 1]
                    for j in range(5):
                        p[j] += totals[i][j]
            if totals[0][2] != file_id:
                raise ValueError('directory totals do not cover the file inventory')
            db.executemany('UPDATE nodes SET logical=?,allocated=?,files=?,exclusive=?,blocked=? WHERE id=?',
                           ((*(totals[i]), i + 1) for i in range(len(paths))))
            manifest = {
                'format_version': FORMAT_VERSION,
                'source_root_base64': metadata['root'],
                'source_root': label(base64.b64decode(metadata['root'])),
                'inventory_time_unix': float(metadata['inventory_time']),
                'scan_epoch': int(metadata['epoch']),
                'files': file_id, 'directories': len(paths),
                'logical_bytes': totals[0][0], 'allocated_bytes': totals[0][1],
                'skipped_entries': skipped_count, 'inventory_errors': issue_count,
                'hardlink_paths': hardlink_paths,
                'blocked_entries': totals[0][4],
                'size_scope': 'Regular-file allocated bytes per path (POSIX st_blocks * 512 or Windows compressed-file size); directory metadata excluded.',
                'coverage_scope': 'SHA-256 file-content coverage, or native identity for hardlink aliases; names, layout, ACLs and xattrs are not compared.',
                'shared_bytes': 'Sum over matching contents of min(bytes in X, bytes in Y).',
                'similarity': 'shared_bytes / sqrt(total_bytes_X * total_bytes_Y)',
            }
            db.executemany('INSERT INTO metadata VALUES (?,?)',
                           ((key, json.dumps(value, sort_keys=True)) for key, value in sorted(manifest.items())))
            progress('Indexing report data')
            db.executescript('''
                CREATE INDEX entries_content ON entries(content);
                CREATE INDEX nodes_parent ON nodes(parent);
            ''')
            db.commit()
            if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('report integrity check failed')
        except BaseException:
            db.close()
            temporary.unlink(missing_ok=True)
            raise
        else:
            db.close()
    progress('Preparing the initial folder matrix')
    try:
        view = Report(temporary).view()
        # The DB is authoritative; keep its initial view in the same snapshot.
        with contextlib.closing(sqlite3.connect(temporary)) as db:
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('PRAGMA synchronous=OFF')
            db.execute('INSERT INTO metadata VALUES (?,?)', ('_root_view', json_bytes(view).decode('ascii')))
            db.commit()
        artifacts = {'manifest.json': json_bytes(manifest), 'root.json': json_bytes(view)}
        for name in ('index.html', 'app.js', 'theme.js', 'style.css'):
            artifacts[name] = (Path(__file__).parent / 'web' / name).read_bytes()
        for name, data in artifacts.items():
            (output / (name + '.building')).write_bytes(data)
        os.chmod(temporary, 0o600)
        with temporary.open('r+b') as stream:
            os.fsync(stream.fileno())
        os.replace(temporary, output / 'report.sqlite3')
        for name in artifacts:
            os.replace(output / (name + '.building'), output / name)
    except BaseException:
        temporary.unlink(missing_ok=True)
        for name in ('index.html', 'app.js', 'theme.js', 'style.css', 'root.json', 'manifest.json'):
            (output / (name + '.building')).unlink(missing_ok=True)
        raise
    progress('Report ready: ' + str(output))
    return manifest


class Report:
    """UI-independent JSON query API; does not open the source scan or NAS."""
    def __init__(self, database, max_items=64):
        self.database = Path(database)
        self.max_items = max_items
        self._node_paths = None

    @staticmethod
    def node(row):
        ident, path, _, logical, allocated, files, exclusive, blocked = row
        path = bytes(path)
        return {'token': 'd:' + str(ident), 'kind': 'folder', 'path_base64': encoded(path),
                'name': label(path.rsplit(b'/', 1)[-1]) if path else 'Root',
                'path': label(path), 'logical': logical, 'allocated': allocated,
                'files': files, 'blocked': blocked,
                'covered_elsewhere': bool(files and exclusive == 0 and blocked == 0)}

    def axis(self, db, token, offset, metric):
        kind, value = token.split(':', 1)
        value = int(value)
        if kind == 'd':
            row = db.execute('SELECT * FROM nodes WHERE id=?', (value,)).fetchone()
            if row is None:
                raise ValueError('folder does not exist')
            scope = self.node(row)
            items = [self.node(row) for row in db.execute('SELECT * FROM nodes WHERE parent=?', (value,))]
            files = db.execute('''SELECT e.id,e.name,e.logical,e.allocated,c.occurrences
                FROM entries e LEFT JOIN contents c ON c.id=e.content WHERE e.parent=?''', (value,))
            directory = base64.b64decode(scope['path_base64'])
            for ident, name, logical, allocated, occurrences in files:
                path = directory + (b'/' if directory else b'') + bytes(name)
                items.append({'token': 'f:' + str(ident), 'kind': 'file', 'name': label(bytes(name)),
                              'path': label(path), 'path_base64': encoded(path), 'logical': logical,
                              'allocated': allocated, 'files': 1, 'blocked': 0,
                              'covered_elsewhere': bool(occurrences and occurrences > 1)})
        elif kind == 'f':
            row = db.execute('''SELECT e.id,n.path,e.name,e.logical,e.allocated,c.occurrences
                FROM entries e JOIN nodes n ON n.id=e.parent LEFT JOIN contents c ON c.id=e.content
                WHERE e.id=?''', (value,)).fetchone()
            if row is None:
                raise ValueError('file does not exist')
            ident, directory, name, logical, allocated, occurrences = row
            path = bytes(directory) + (b'/' if directory else b'') + bytes(name)
            scope = {'token': token, 'kind': 'file', 'path': label(path), 'name': label(bytes(name)),
                     'path_base64': encoded(path), 'logical': logical, 'allocated': allocated,
                     'files': 1, 'blocked': 0, 'covered_elsewhere': bool(occurrences and occurrences > 1)}
            items = [scope.copy()]
        else:
            raise ValueError('invalid axis token')
        items.sort(key=lambda item: (-item[metric], base64.b64decode(item['path_base64']), item['token']))
        if offset < 0 or offset > len(items):
            raise ValueError('invalid axis offset')
        # Deterministic level of detail: never send an unbounded square matrix.
        # The remainder retains its EXACT summed weight and can itself be opened.
        remaining = items[offset:]
        visible = remaining[:self.max_items]
        hidden = []
        if len(remaining) > self.max_items:
            visible = remaining[:self.max_items - 1]
            hidden = remaining[self.max_items - 1:]
            visible.append({'token': token, 'offset': offset + self.max_items - 1, 'kind': 'other',
                            'name': 'Other {:,} items'.format(len(hidden)), 'path': scope['path'],
                            'logical': sum(i['logical'] for i in hidden),
                            'allocated': sum(i['allocated'] for i in hidden),
                            'files': sum(i['files'] for i in hidden), 'blocked': sum(i['blocked'] for i in hidden),
                            'covered_elsewhere': False})
        mapping = {base64.b64decode(item['path_base64']): i for i, item in enumerate(visible) if item['kind'] != 'other'}
        if hidden:
            mapping.update((base64.b64decode(item['path_base64']), len(visible) - 1) for item in hidden)
        scope_path = base64.b64decode(scope['path_base64'])
        def partition(path):
            if scope['kind'] == 'file':
                return 0 if path == scope_path else None
            prefix = scope_path + b'/' if scope_path else b''
            if not path.startswith(prefix):
                return None
            child = path[len(prefix):].split(b'/', 1)[0]
            return mapping.get(prefix + child)
        for item in visible:
            item['weight'] = item[metric]
        result = {'scope': scope, 'offset': offset, 'items': visible,
                  'total': sum(i['weight'] for i in visible), 'item_count': len(remaining),
                  'zero_size_items': sum(i[metric] == 0 for i in remaining)}
        return result, partition

    def view(self, x='d:1', y='d:1', xo=0, yo=0, metric='allocated'):
        if metric not in ('allocated', 'logical'):
            raise ValueError('invalid size metric')
        with contextlib.closing(readonly(self.database)) as db:
            db.execute('PRAGMA cache_size=-131072')
            if (self.max_items, x, y, xo, yo, metric) == (64, 'd:1', 'd:1', 0, 0, 'allocated'):
                cached = db.execute("SELECT value FROM metadata WHERE key='_root_view'").fetchone()
                if cached:
                    return json.loads(cached[0])
            ax, px = self.axis(db, x, xo, metric)
            ay, py = self.axis(db, y, yo, metric)
            xs, ys = ax['items'], ay['items']
            cells = [[{'shared': 0, 'x_covered_files': 0, 'y_covered_files': 0} for _ in ys] for _ in xs]
            # Restrict deep queries to directory IDs, then drive from the
            # content index. Root generation reads the compact report once.
            if self._node_paths is None:
                self._node_paths = {row[0]: bytes(row[1]) for row in db.execute('SELECT id,path FROM nodes')}
            node_paths = self._node_paths
            # Prefix-to-partition mapping is per DIRECTORY, not per file.
            # This avoids millions of repeated path splits and decodes.
            parent_parts = {}
            for ident, path in node_paths.items():
                probe = path + (b'/' if path else b'') + b'\x00'
                a, b = px(probe), py(probe)
                if a is not None or b is not None:
                    parent_parts[ident] = (a, b)
            current = None
            gx, gy = {}, {}
            def flush():
                for a, (abytes, acount) in gx.items():
                    for b, (bbytes, bcount) in gy.items():
                        cell = cells[a][b]
                        cell['shared'] += min(abytes, bbytes)
                        cell['x_covered_files'] += acount
                        cell['y_covered_files'] += bcount
            query = '''SELECT e.content,e.parent,e.name,e.logical,e.allocated
                FROM entries e JOIN contents c ON c.id=e.content
                WHERE c.occurrences>1 ORDER BY e.content,e.id'''
            selected = set(parent_parts)
            boundaries = []
            for token in (x, y):
                if token.startswith('d:'):
                    boundary = int(token[2:])
                else:
                    boundary = db.execute('SELECT parent FROM entries WHERE id=?', (int(token[2:]),)).fetchone()[0]
                selected.add(boundary)
                boundaries.append(boundary)
            if len(selected) < len(node_paths) // 3:
                db.execute('CREATE TEMP TABLE selected_parent(id INTEGER PRIMARY KEY)')
                db.executemany('INSERT INTO selected_parent VALUES (?)', ((ident,) for ident in sorted(selected)))
                query = '''SELECT e.content,e.parent,e.name,e.logical,e.allocated
                    FROM selected_parent p CROSS JOIN entries e ON e.parent=p.id
                    JOIN contents c ON c.id=e.content WHERE c.occurrences>1
                    ORDER BY e.content,e.id'''
            for content, directory, name, logical, allocated in db.execute(query):
                if content != current:
                    flush(); gx, gy = {}, {}; current = content
                ap, bp = parent_parts.get(directory, (None, None))
                # For a direct file or file axis the directory alone cannot
                # determine the partition. Resolve only these boundary cases.
                path = None
                if ap is None and directory == boundaries[0]:
                    base = node_paths[directory]
                    path = base + (b'/' if base else b'') + bytes(name)
                    ap = px(path)
                if bp is None and directory == boundaries[1]:
                    if path is None:
                        base = node_paths[directory]
                        path = base + (b'/' if base else b'') + bytes(name)
                    bp = py(path)
                weight = allocated if metric == 'allocated' else logical
                for bucket, part in ((gx, ap), (gy, bp)):
                    if part is not None:
                        value = bucket.setdefault(part, [0, 0])
                        value[0] += weight; value[1] += 1
            flush()
            for a, row in enumerate(cells):
                for b, cell in enumerate(row):
                    xi, yi = xs[a], ys[b]
                    # Overlapping scopes include a file matching itself. They
                    # are navigation tiles, never evidence of duplication.
                    xp = base64.b64decode(xi.get('path_base64', ''))
                    yp = base64.b64decode(yi.get('path_base64', ''))
                    overlap = (xi['kind'] != 'other' and yi['kind'] != 'other' and
                               (contains(xp, yp) or contains(yp, xp)))
                    # Remainders on the same axis overlap themselves; a
                    # remainder against any concrete sibling is disjoint.
                    if xi['kind'] == 'other' and yi['kind'] == 'other':
                        xscope = base64.b64decode(ax['scope']['path_base64'])
                        yscope = base64.b64decode(ay['scope']['path_base64'])
                        overlap = (x == y or px(yscope + b'/\x00') == a or
                                   py(xscope + b'/\x00') == b)
                    elif xi['kind'] == 'other' or yi['kind'] == 'other':
                        concrete, scope, partition, index = (yi, ax, px, a) if xi['kind'] == 'other' else (xi, ay, py, b)
                        raw = base64.b64decode(concrete['path_base64'])
                        overlap = (partition(raw + (b'/\x00' if concrete['kind'] == 'folder' else b'')) == index or
                                   (concrete['kind'] == 'folder' and contains(raw, base64.b64decode(scope['scope']['path_base64']))))
                    denominator = math.sqrt(xi['weight'] * yi['weight'])
                    cell['overlap'] = overlap
                    cell['similarity'] = 0 if overlap or not denominator else min(1., cell['shared'] / denominator)
                    cell['x_in_y'] = bool(not overlap and xi['files'] and not xi['blocked'] and
                                          cell['x_covered_files'] == xi['files'])
                    cell['y_in_x'] = bool(not overlap and yi['files'] and not yi['blocked'] and
                                          cell['y_covered_files'] == yi['files'])
                    cell['x_in_single'] = cell['x_in_y'] and yi['kind'] != 'other'
                    cell['y_in_single'] = cell['y_in_x'] and xi['kind'] != 'other'
                    if overlap:
                        cell['shared'] = 0
            return {'format_version': FORMAT_VERSION, 'metric': metric, 'x': ax, 'y': ay, 'cells': cells}

    def manifest(self):
        with contextlib.closing(readonly(self.database)) as db:
            return {key: json.loads(value) for key, value in db.execute('SELECT key,value FROM metadata ORDER BY key')
                    if not key.startswith('_')}

    def covered(self, offset=0, limit=50, metric='allocated'):
        """Largest maximal covered folders, so the user needn't hunt by hand."""
        if offset < 0 or not 1 <= limit <= 100 or metric not in ('allocated', 'logical'):
            raise ValueError('invalid covered-folder query')
        with contextlib.closing(readonly(self.database)) as db:
            predicate = '''n.files>0 AND n.exclusive=0 AND n.blocked=0
                AND (p.id IS NULL OR p.files=0 OR p.exclusive<>0 OR p.blocked<>0)'''
            total = db.execute('SELECT COUNT(*) FROM nodes n LEFT JOIN nodes p ON p.id=n.parent WHERE ' + predicate).fetchone()[0]
            rows = db.execute('''SELECT n.* FROM nodes n LEFT JOIN nodes p ON p.id=n.parent
                WHERE ''' + predicate + ' ORDER BY n.' + metric + ' DESC,n.path LIMIT ? OFFSET ?', (limit, offset))
            return {'items': [self.node(row) for row in rows], 'total': total, 'offset': offset, 'limit': limit}

    def keepers(self, token):
        """Find every maximal disjoint item covering ALL of this folder.

        Siblings along the folder's ancestor chain partition its complement.
        If any disjoint folder covers all contents, one such maximal branch
        also covers all contents. This proves the single/distributed distinction
        without comparing a folder to an ancestor that already contains itself.
        """
        if not token.startswith('d:'):
            raise ValueError('select a folder')
        with contextlib.closing(readonly(self.database)) as db:
            row = db.execute('SELECT * FROM nodes WHERE id=?', (int(token[2:]),)).fetchone()
            if row is None:
                raise ValueError('folder does not exist')
            scope = self.node(row)
            if not scope['covered_elsewhere']:
                return {'folder': scope, 'keepers': [], 'distributed_only': False}
            folder = base64.b64decode(scope['path_base64'])
            paths = {ident: bytes(path) for ident, path in db.execute('SELECT id,path FROM nodes')}
            ids = {path: ident for ident, path in paths.items()}
            db.execute('CREATE TEMP TABLE selected_parent(id INTEGER PRIMARY KEY)')
            db.executemany('INSERT INTO selected_parent VALUES (?)',
                           ((ident,) for ident, path in paths.items() if contains(folder, path)))
            db.execute('CREATE TEMP TABLE wanted(content INTEGER PRIMARY KEY,n INTEGER)')
            db.execute('''INSERT INTO wanted SELECT e.content,COUNT(*)
                FROM selected_parent p CROSS JOIN entries e ON e.parent=p.id GROUP BY e.content''')
            coverage = collections.Counter()
            branches, files = {}, {}
            previous, group_branches, occurrences = None, set(), 0
            def flush():
                for branch in group_branches:
                    coverage[branch] += occurrences
            query = '''SELECT w.content,w.n,e.id,e.parent,e.name FROM wanted w
                CROSS JOIN entries e ON e.content=w.content ORDER BY w.content,e.id'''
            for content, occurrences_in_scope, ident, directory, name in db.execute(query):
                if content != previous:
                    flush(); group_branches = set(); previous = content
                    occurrences = occurrences_in_scope
                p = paths[directory]
                if contains(folder, p):
                    continue
                if directory not in branches:
                    common = common_folder(folder, p)
                    if p == common:
                        branches[directory] = None  # direct file of an ancestor
                    else:
                        prefix = common + b'/' if common else b''
                        branch_path = prefix + p[len(prefix):].split(b'/', 1)[0]
                        branches[directory] = 'd:' + str(ids[branch_path])
                branch = branches[directory]
                if branch is None:
                    branch = 'f:' + str(ident)
                    path = p + (b'/' if p else b'') + bytes(name)
                    files[branch] = {'token': branch, 'kind': 'file', 'path': label(path),
                                     'path_base64': encoded(path), 'name': label(bytes(name))}
                group_branches.add(branch)
            flush()
            keepers = []
            for token, matched in coverage.items():
                if matched != scope['files']:
                    continue
                if token.startswith('d:'):
                    keepers.append(self.node(db.execute('SELECT * FROM nodes WHERE id=?', (int(token[2:]),)).fetchone()))
                else:
                    keepers.append(files[token])
            keepers.sort(key=lambda item: (base64.b64decode(item['path_base64']), item['token']))
            return {'folder': scope, 'keepers': keepers, 'distributed_only': not bool(keepers)}
