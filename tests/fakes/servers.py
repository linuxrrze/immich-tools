"""Fake Nextcloud (WebDAV + comments) and Immich API on one HTTP server.

The server runs in a background thread on a free port (port 0). Each
instance has its own state, so tests do not influence each other.

- Nextcloud lives below `<base>/nextcloud` (a sub-path install, like
  https://example.com/nextcloud); `nc_prefix=''` puts it at the root.
- Immich lives below `<base>/api`; `version` selects the v2 or v3 behaviour.
- `gate(kind)` blocks a chosen request until the test releases it, so tests
  can interrupt a client at an exact point without sleeps.
"""

import base64
import hashlib
import json
import re
import sys
import threading
import uuid
from email.parser import BytesParser
from email.policy import default as email_policy
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: F401
from urllib.parse import quote, unquote, urlparse
from xml.sax.saxutils import escape

NC_USER = 'alice'
NC_PASSWORD = 'secret%pass'
IMMICH_KEY = 'test-api-key'

# Comment tuples: (creationDateTime, verb, message)
JAN18 = 'Mon, 18 Jan 2016 22:01:16 GMT'


def default_files():
    """Nextcloud test data: path -> dict(content, type, fileid, comments)."""
    return {
        # Root folder: comments in random order, a reaction, a multi-line comment
        '/Fotos Alt/a.jpg': dict(content=b'JPEG-A', type='image/jpeg', fileid='11', comments=[
            (JAN18, 'comment', 'Oma in Rom'),
            ('Tue, 19 Jan 2016 08:00:00 GMT', 'reaction', '\U0001F44D'),
            ('Sat, 16 Jan 2016 10:00:00 GMT', 'comment', 'Erster Kommentar\nzweite Zeile'),
        ]),
        # 101 comments: needs a second REPORT page
        '/Fotos Alt/many.png': dict(content=b'PNG-D', type='image/png', fileid='15', comments=[
            (formatdate(1600000000 + i * 60, usegmt=True), 'comment', f'c{i:03d}') for i in range(101)
        ]),
        # Same content as a.jpg in another folder
        '/Fotos Alt/Kopie/a-copy.jpg': dict(content=b'JPEG-A', type='image/jpeg', fileid='16', comments=[
            ('Wed, 20 Jan 2016 09:00:00 GMT', 'comment', 'Kopie: Rom 2016'),
        ]),
        '/Fotos Alt/sub/b.heic': dict(content=b'HEIC-B', type='image/heic', fileid='12', comments=[]),
        # ISO date
        '/Fotos Alt/sub/c.mp4': dict(content=b'MP4-C' * 1000, type='video/mp4', fileid='13', comments=[
            ('2019-07-01 12:00:00', 'comment', 'Urlaub'),
        ]),
        # Not a media file: ignored
        '/Fotos Alt/sub/notes.txt': dict(content=b'text', type='text/plain', fileid='14', comments=[
            (JAN18, 'comment', 'ignore me'),
        ]),
    }


def sha1_hex(data):
    return hashlib.sha1(data).hexdigest()


class QuietServer(ThreadingHTTPServer):
    """Doesn't print errors of clients that went away (interrupt tests)."""
    daemon_threads = True

    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


class Gate:
    """Blocks the `nth` matching request after arming until `release()`."""

    def __init__(self, kind, nth):
        self.kind, self.nth, self.count = kind, nth, 0
        self.reached = threading.Event()
        self._release = threading.Event()
        self.request = None
        self.dropped = False

    def hit(self, request):
        self.count += 1
        if self.count != self.nth:
            return
        self.request = request
        self.reached.set()
        self._release.wait(60)
        return self.dropped

    def wait(self, timeout=30):
        if not self.reached.wait(timeout):
            raise AssertionError(f'gate {self.kind} #{self.nth} not reached')
        return self.request

    def release(self, drop=False):
        """Let the request continue; with drop=True it fails with 503
        without any effect, as if the client had gone away."""
        self.dropped = drop
        self._release.set()


class FakeServer:
    def __init__(self, version=(3, 4, 1), files=None, nc_prefix='/nextcloud',
                 search_page_cap=1000):
        self.version = tuple(version)
        self.files = default_files() if files is None else files
        self.nc_prefix = nc_prefix
        self.search_page_cap = search_page_cap
        self.lock = threading.RLock()
        self.gates = []
        # Immich state
        self.assets = {}      # id -> dict(checksum, content, description, ...)
        self.albums = {}      # id -> dict(albumName, assets=[...])
        self.tags = {}        # value -> dict(id, parentId)
        self.tag_assets = {}  # tag id -> [asset ids]
        self.uploads = []     # form fields of every upload (without the data)
        self.description_writes = []  # (method, asset id, description)
        self.searches = []    # /search/metadata bodies
        self.reports = []     # (fileid, offset, limit) of comment REPORTs
        self.requests = []    # (method, path)
        self.album_renames = []  # (album id, new name)
        self.deleted_tags = []   # values of deleted tags (children go with them)
        self._tag_counter = 0
        self.restored = []  # asset ids of POST /trash/restore/assets
        self.restore_status = None  # e.g. 403: POST /trash/restore/assets fails
        self.album_rename_status = None  # e.g. 403: PATCH /albums/{id} fails
        self._httpd = QuietServer(('127.0.0.1', 0), _make_handler(self))
        self._thread = threading.Thread(target=self._httpd.serve_forever, args=(0.05,), daemon=True)

    # --- lifecycle -------------------------------------------------------
    @property
    def base_url(self):
        return f'http://127.0.0.1:{self._httpd.server_address[1]}'

    @property
    def immich_url(self):
        return self.base_url + '/api'

    @property
    def nextcloud_url(self):
        return self.base_url + self.nc_prefix

    @property
    def nc_root(self):
        return f'{self.nc_prefix}/remote.php/dav/files/{NC_USER}'

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        for gate in self.gates:
            gate.release()
        self._httpd.shutdown()
        self._httpd.server_close()

    def env(self):
        """Environment variables that point the tools at this server."""
        return {
            'IMMICH_APIURL': self.immich_url,
            'IMMICH_APIKEY': IMMICH_KEY,
            'NEXTCLOUD_URL': self.nextcloud_url,
            'NEXTCLOUD_USER': NC_USER,
            'NEXTCLOUD_PASSWORD': NC_PASSWORD,
        }

    # --- test helpers ----------------------------------------------------
    def gate(self, kind, nth=1):
        """Block the nth request of `kind` ('download' of a Nextcloud file,
        'album_put' = PUT /albums/{id}/assets) counted from now."""
        g = Gate(kind, nth)
        with self.lock:
            self.gates.append(g)
        return g

    def add_asset(self, content, description='', filename='x.jpg', asset_type='IMAGE',
                  album=None, library_path=None, trashed=False):
        aid = str(uuid.uuid4())
        checksum = sha1_hex(f'path:{library_path}'.encode() if library_path else content)
        self.assets[aid] = dict(checksum=checksum, content=content, description=description,
                                filename=filename, type=asset_type,
                                libraryId='lib-1' if library_path else None,
                                originalPath=library_path or f'/upload/{filename}', trashed=trashed)
        if album:
            self.album_by_name(album, create=True)['assets'].append(aid)
        return aid

    def add_album(self, name):
        aid = f'alb-{len(self.albums)}'
        self.albums[aid] = dict(albumName=name, assets=[])
        return aid

    def album_by_name(self, name, create=False):
        found = [a for a in self.albums.values() if a['albumName'] == name]
        if not found and create:
            return self.albums[self.add_album(name)]
        assert len(found) == 1, f'{len(found)} albums named {name!r}'
        return found[0]

    def album_names(self):
        return sorted(a['albumName'] for a in self.albums.values())

    def asset_by_checksum(self, content):
        found = [i for i, a in self.assets.items() if a['checksum'] == sha1_hex(content)]
        assert len(found) == 1, found
        return found[0]

    def add_tag(self, value, asset_ids=()):
        parent, path = None, ''
        for part in value.split('/'):
            path = f'{path}/{part}' if path else part
            if path not in self.tags:
                self.tags[path] = {'id': self._new_tag_id(), 'parentId': parent}
            parent = self.tags[path]['id']
        self.tag_assets.setdefault(parent, []).extend(asset_ids)
        return parent

    def _new_tag_id(self):
        self._tag_counter += 1
        return f'tag-{self._tag_counter}'

    def tagged(self, value):
        tag = self.tags.get(value)
        return self.tag_assets.get(tag['id'], []) if tag else []

    def _check_gates(self, kind, request):
        with self.lock:
            gates = [g for g in self.gates if g.kind == kind and not g.reached.is_set()]
        return any([g.hit(request) for g in gates])


def _make_handler(srv):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        disable_nagle_algorithm = True  # headers and body are separate writes

        def log_message(self, *args):
            pass

        # --- helpers ---
        def send(self, code, body=b'', content_type='application/xml; charset=utf-8'):
            if isinstance(body, (dict, list)):
                body, content_type = json.dumps(body).encode(), 'application/json'
            elif isinstance(body, str):
                body = body.encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def read_body(self):
            return self.rfile.read(int(self.headers.get('Content-Length') or 0))

        def json_body(self):
            return json.loads(self.read_body() or b'null')

        @property
        def p(self):
            return unquote(urlparse(self.path).path)

        def dispatch(self, method):
            with srv.lock:
                srv.requests.append((method, self.p))
            if self.p.startswith('/api/'):
                if self.headers.get('x-api-key') != IMMICH_KEY:
                    self.read_body()
                    return self.send(401, {'message': 'Invalid API key'})
                return getattr(self, f'immich_{method}', self.not_found)()
            if self.p.startswith(srv.nc_prefix + '/remote.php/dav/'):
                expected = 'Basic ' + base64.b64encode(f'{NC_USER}:{NC_PASSWORD}'.encode()).decode()
                if self.headers.get('Authorization') != expected:
                    self.read_body()
                    return self.send(401)
                return getattr(self, f'nc_{method}', self.not_found)()
            self.not_found()

        def not_found(self):
            self.read_body()
            self.send(404, {'message': f'no route {self.command} {self.p}'})

        do_GET = lambda self: self.dispatch('GET')            # noqa: E731
        do_POST = lambda self: self.dispatch('POST')          # noqa: E731
        do_PUT = lambda self: self.dispatch('PUT')            # noqa: E731
        do_PATCH = lambda self: self.dispatch('PATCH')        # noqa: E731
        do_PROPFIND = lambda self: self.dispatch('PROPFIND')  # noqa: E731
        do_REPORT = lambda self: self.dispatch('REPORT')      # noqa: E731
        do_DELETE = lambda self: self.dispatch('DELETE')      # noqa: E731

        # --- Nextcloud ---
        def _prop_xml(self, path, f=None):
            href = quote(srv.nc_root + path) + ('' if f else '/')
            if f is None:
                return (f'<d:response><d:href>{href}</d:href><d:propstat><d:prop>'
                        '<d:resourcetype><d:collection/></d:resourcetype></d:prop></d:propstat></d:response>')
            return (f'<d:response><d:href>{href}</d:href><d:propstat><d:prop><d:resourcetype/>'
                    f"<d:getcontenttype>{f['type']}</d:getcontenttype>"
                    '<d:getlastmodified>Mon, 01 Jul 2019 10:00:00 GMT</d:getlastmodified>'
                    f"<d:getetag>\"etag{f['fileid']}{f.get('etag', '')}\"</d:getetag>"
                    f"<d:getcontentlength>{len(f['content'])}</d:getcontentlength>"
                    f"<oc:fileid>{f['fileid']}</oc:fileid>"
                    f"<oc:comments-count>{len([c for c in f['comments'] if c[1] == 'comment'])}</oc:comments-count>"
                    '</d:prop></d:propstat></d:response>')

        def nc_PROPFIND(self):
            self.read_body()
            assert self.headers['Depth'] == '1'
            if not self.p.startswith(srv.nc_root):
                return self.send(404)
            folder = self.p[len(srv.nc_root):].rstrip('/')
            out, subdirs = [self._prop_xml(folder)], set()
            for path, f in srv.files.items():
                if not path.startswith(folder + '/'):
                    continue
                rest = path[len(folder) + 1:]
                if '/' in rest:
                    subdirs.add(folder + '/' + rest.split('/')[0])
                else:
                    out.append(self._prop_xml(path, f))
            out += [self._prop_xml(d) for d in sorted(subdirs)]
            self.send(207, '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
                           'xmlns:oc="http://owncloud.org/ns">' + ''.join(out) + '</d:multistatus>')

        def nc_GET(self):
            f = srv.files.get(self.p[len(srv.nc_root):]) if self.p.startswith(srv.nc_root) else None
            if f is None:
                return self.send(404)
            if srv._check_gates('download', self.p):
                return self.send(503)
            self.send(200, f['content'], 'application/octet-stream')

        def nc_REPORT(self):
            body = self.read_body().decode()
            m = re.fullmatch(re.escape(srv.nc_prefix) + r'/remote\.php/dav/comments/files/(\d+)/?', self.p)
            if not m:
                return self.send(404)
            limit = min(int(re.search(r'<oc:limit>(\d+)', body).group(1)), 100)  # server maximum
            offset = int(re.search(r'<oc:offset>(\d+)', body).group(1))
            srv.reports.append((m.group(1), offset, limit))
            comments = [c for f in srv.files.values() if f['fileid'] == m.group(1) for c in f['comments']]
            items = ''.join(
                f'<d:response><d:href>/c/{offset + i}</d:href><d:propstat><d:prop>'
                f'<oc:verb>{v}</oc:verb><oc:message>{escape(msg)}</oc:message>'
                f'<oc:actorId>{NC_USER}</oc:actorId><oc:creationDateTime>{d}</oc:creationDateTime>'
                '</d:prop></d:propstat></d:response>'
                for i, (d, v, msg) in enumerate(comments[offset:offset + limit]))
            self.send(207, '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
                           'xmlns:oc="http://owncloud.org/ns">' + items + '</d:multistatus>')

        # --- Immich ---
        def _asset_json(self, aid):
            a = srv.assets[aid]
            return {
                'id': aid, 'type': a['type'], 'originalFileName': a['filename'],
                'originalPath': a['originalPath'], 'libraryId': a['libraryId'],
                'checksum': base64.b64encode(bytes.fromhex(a['checksum'])).decode(),
                'exifInfo': {'description': a['description'] or None},
                'isTrashed': a['trashed'],
            }

        def immich_GET(self):
            p = self.p[len('/api'):]
            if p == '/server/version':
                major, minor, patch = srv.version
                return self.send(200, {'major': major, 'minor': minor, 'patch': patch})
            if p == '/albums':
                return self.send(200, [{'id': i, 'albumName': a['albumName']} for i, a in srv.albums.items()])
            if p == '/tags':
                return self.send(200, [{'id': t['id'], 'value': v, 'name': v.split('/')[-1],
                                        'parentId': t['parentId']} for v, t in srv.tags.items()])
            m = re.fullmatch(r'/assets/([^/]+)(/original)?', p)
            if m:
                if m.group(1) not in srv.assets:
                    return self.send(400, {'message': 'Not found or no asset.read access'})
                if m.group(2):
                    return self.send(200, srv.assets[m.group(1)]['content'], 'application/octet-stream')
                return self.send(200, self._asset_json(m.group(1)))
            self.send(404)

        def immich_POST(self):
            p = self.p[len('/api'):]
            if p == '/assets/bulk-upload-check':
                results = []
                for a in self.json_body()['assets']:
                    hits = [i for i, x in srv.assets.items() if x['checksum'] == a['checksum']]
                    results.append({'id': a['id'], 'action': 'reject', 'reason': 'duplicate',
                                    'assetId': hits[0], 'isTrashed': srv.assets[hits[0]]['trashed']} if hits else
                                   {'id': a['id'], 'action': 'accept'})
                return self.send(200, {'results': results})
            if p == '/assets':
                return self._upload()
            if p == '/albums':
                body = self.json_body()
                with srv.lock:
                    aid = srv.add_album(body['albumName'])
                return self.send(201, {'id': aid, 'albumName': body['albumName']})
            if p == '/search/metadata':
                return self._search(self.json_body())
            if p == '/trash/restore/assets':
                body = self.json_body()
                if srv.restore_status:
                    return self.send(srv.restore_status, {'message': 'Missing permission: asset.delete'})
                with srv.lock:
                    srv.restored.extend(body['ids'])
                    for i in body['ids']:
                        if i in srv.assets:
                            srv.assets[i]['trashed'] = False
                return self.send(200, {'count': len(body['ids'])})
            self.not_found()

        def _upload(self):
            raw = self.read_body()
            msg = BytesParser(policy=email_policy).parsebytes(
                b'Content-Type: ' + self.headers['Content-Type'].encode() + b'\r\n\r\n' + raw)
            form = {part.get_param('name', header='content-disposition'): part.get_payload(decode=True)
                    for part in msg.iter_parts()}
            data = form.pop('assetData')
            fields = {k: v.decode() for k, v in form.items()}
            device = {'deviceAssetId', 'deviceId'} & fields.keys()
            if srv.version < (3, 0, 0) and len(device) != 2:
                return self.send(400, {'message': 'deviceAssetId and deviceId are required'})
            if srv.version >= (3, 0, 0) and device:
                return self.send(400, {'message': f'property {sorted(device)} should not exist'})
            with srv.lock:
                srv.uploads.append(fields)
                aid = srv.add_asset(data, filename=fields['filename'])
            self.send(201, {'id': aid, 'status': 'created'})

        def _search(self, body):
            srv.searches.append(json.loads(json.dumps(body)))
            if srv.version >= (3, 2, 0):
                if 'albumIds' in body or 'page' in body or 'filter' not in body:
                    return self.send(400, {'message': 'use filter/cursor'})
                album_ids = body['filter']['albumIds']['any']
                asset_type = body['filter'].get('type', {}).get('eq')
                start = int(body.get('cursor') or 0)
            else:
                if 'filter' in body or 'cursor' in body:
                    return self.send(400, {'message': 'property filter should not exist'})
                album_ids, asset_type = body['albumIds'], body.get('type')
                start = (int(body.get('page', 1)) - 1) * min(body['size'], srv.search_page_cap)
            size = min(body['size'], srv.search_page_cap)
            # Trashed assets are not found (withDeleted is not set)
            ids = [i for alb in album_ids for i in srv.albums[alb]['assets']
                   if (asset_type is None or srv.assets[i]['type'] == asset_type)
                   and not srv.assets[i]['trashed']]
            page = ids[start:start + size]
            more = start + size < len(ids)
            assets = {'items': [self._asset_json(i) for i in page], 'count': len(page)}
            if srv.version >= (3, 2, 0):
                assets['nextCursor'] = str(start + size) if more else None
            else:
                assets['nextPage'] = str(start // size + 2) if more else None
            self.send(200, {'assets': assets})

        def _set_description(self, method):
            aid = self.p.rsplit('/', 1)[-1]
            body = self.json_body()
            if aid not in srv.assets:
                return self.send(400, {'message': 'Not found or no asset.update access'})
            with srv.lock:
                srv.description_writes.append((method, aid, body['description']))
                srv.assets[aid]['description'] = body['description']
            self.send(200, self._asset_json(aid))

        def immich_PATCH(self):
            # PATCH /assets/{id} exists since v3.0.0
            if srv.version >= (3, 0, 0) and re.fullmatch(r'/api/assets/[^/]+', self.p):
                return self._set_description('PATCH')
            m = re.fullmatch(r'/api/albums/([^/]+)', self.p)
            if m:
                body = self.json_body()
                if srv.album_rename_status:
                    return self.send(srv.album_rename_status, {'message': 'Missing permission: album.update'})
                if m.group(1) not in srv.albums:
                    return self.send(400, {'message': 'Not found or no album.update access'})
                with srv.lock:
                    srv.album_renames.append((m.group(1), body['albumName']))
                    srv.albums[m.group(1)]['albumName'] = body['albumName']
                return self.send(200, {'id': m.group(1), 'albumName': body['albumName']})
            self.not_found()

        def immich_DELETE(self):
            m = re.fullmatch(r'/api/tags/([^/]+)', self.p)
            if not m:
                return self.not_found()
            with srv.lock:
                values = [v for v, t in srv.tags.items() if t['id'] == m.group(1)]
                if not values:
                    return self.send(400, {'message': 'Not found or no tag.delete access'})
                srv.deleted_tags.append(values[0])
                # Children are deleted with their parent
                for v in [v for v in srv.tags if v == values[0] or v.startswith(values[0] + '/')]:
                    srv.tag_assets.pop(srv.tags.pop(v)['id'], None)
            self.send(204)

        def immich_PUT(self):
            p = self.p[len('/api'):]
            if re.fullmatch(r'/assets/[^/]+', p):
                return self._set_description('PUT')
            m = re.fullmatch(r'/albums/([^/]+)/assets', p)
            if m:
                body = self.json_body()
                if srv._check_gates('album_put', p):
                    return self.send(503)
                with srv.lock:
                    album = srv.albums[m.group(1)]['assets']
                    results = []
                    for i in body['ids']:
                        if i in album:
                            results.append({'id': i, 'success': False, 'error': 'duplicate'})
                        else:
                            album.append(i)
                            results.append({'id': i, 'success': True})
                return self.send(200, results)
            if p == '/tags':
                out = []
                with srv.lock:
                    for name in self.json_body()['tags']:
                        parent, value = None, ''
                        for part in [x for x in name.split('/') if x]:
                            value = f'{value}/{part}' if value else part
                            if value not in srv.tags:
                                srv.tags[value] = {'id': srv._new_tag_id(), 'parentId': parent}
                            parent = srv.tags[value]['id']
                        out.append({'id': srv.tags[value]['id'], 'value': value, 'name': value.split('/')[-1]})
                return self.send(200, out)
            if p == '/tags/assets':
                body = self.json_body()
                with srv.lock:
                    for t in body['tagIds']:
                        current = srv.tag_assets.setdefault(t, [])
                        current += [a for a in body['assetIds'] if a not in current]
                return self.send(200, {'count': len(body['assetIds'])})
            self.not_found()

    return Handler
