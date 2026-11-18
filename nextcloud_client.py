"""Nextcloud WebDAV helpers: list media files, download them, read comments.

Comments are stored in the Nextcloud database, not in the files, and are read
via the comments WebDAV endpoint
(https://docs.nextcloud.com/server/latest/developer_manual/client_apis/WebDAV/comments.html).
"""

import email.utils
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import quote, unquote, urljoin, urlparse

import requests

from immich_client import config

NS = {'d': 'DAV:', 'oc': 'http://owncloud.org/ns'}

# Nextcloud returns at most 100 comments per REPORT request
# (s. apps/dav/lib/Comments/CommentsPlugin.php)
COMMENTS_PAGE_SIZE = 100

nextcloud_url = config['nextcloud']['url'].rstrip('/')
nextcloud_user = config['nextcloud']['user']
nextcloud_password = config['nextcloud']['password']

_session = requests.Session()
_session.auth = (nextcloud_user, nextcloud_password)

_files_root = f'/remote.php/dav/files/{nextcloud_user}'  # path below the server URL

_PROPFIND_BODY = '''<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:prop>
    <d:resourcetype/>
    <d:getcontenttype/>
    <d:getlastmodified/>
    <d:getetag/>
    <d:getcontentlength/>
    <oc:fileid/>
    <oc:comments-count/>
  </d:prop>
</d:propfind>'''


def _url(path):
    """Full URL for a server-relative path (as in WebDAV hrefs, which already
    contain a possible sub-path like /nextcloud/remote.php/...)."""
    return urljoin(nextcloud_url + '/', path)


def _request(method, url, **kwargs):
    response = _session.request(method, url, **kwargs)
    response.raise_for_status()
    return response


def _prop(response, name):
    el = response.find(f'd:propstat/d:prop/{name}', NS)
    return el.text if el is not None else None


def _propfind(url):
    """PROPFIND with Depth 1: the folder itself and its direct children."""
    response = _request('PROPFIND', url, data=_PROPFIND_BODY.encode('utf-8'),
                        headers={'Depth': '1', 'Content-Type': 'application/xml'})
    return ET.fromstring(response.content).findall('d:response', NS)


def list_media(folder):
    """Yield all image and video files below `folder` (relative to the
    user's Nextcloud root), recursively.

    Each item is a dict with path, href, fileid, etag, mtime (Unix time),
    size, content_type and comments_count.
    """
    folder = folder.strip('/')
    start = f"{nextcloud_url}{_files_root}/{quote(folder) + '/' if folder else ''}"
    pending = [start]
    while pending:
        current = pending.pop(0)
        current_path = unquote(urlparse(current).path).rstrip('/')
        for entry in _propfind(current):
            href = entry.find('d:href', NS).text
            href_path = unquote(href)
            if href_path.rstrip('/') == current_path:
                continue  # the folder itself
            if entry.find('d:propstat/d:prop/d:resourcetype/d:collection', NS) is not None:
                pending.append(_url(href))
                continue
            content_type = (_prop(entry, 'd:getcontenttype') or '').split(';')[0]
            if not content_type.startswith(('image/', 'video/')):
                continue
            modified = _prop(entry, 'd:getlastmodified')
            yield {
                'path': href_path[href_path.find(_files_root) + len(_files_root):],
                'href': href,
                'fileid': _prop(entry, 'oc:fileid'),
                'etag': (_prop(entry, 'd:getetag') or '').strip('"'),
                'mtime': email.utils.parsedate_to_datetime(modified).timestamp() if modified else None,
                'size': int(_prop(entry, 'd:getcontentlength') or 0),
                'content_type': content_type,
                'comments_count': int(_prop(entry, 'oc:comments-count') or 0),
            }


def download(href, target):
    """Stream a file into the open binary file `target`; returns its SHA1 hex digest."""
    digest = hashlib.sha1()
    with _session.get(_url(href), stream=True) as response:
        response.raise_for_status()
        for chunk in response.iter_content(1024 * 1024):
            digest.update(chunk)
            target.write(chunk)
    return digest.hexdigest()


def _parse_date(value):
    """Comment dates come as RFC 1123 ('Mon, 18 Jan 2016 22:01:16 GMT') or ISO-like strings."""
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def get_comments(fileid):
    """All user comments of a file, oldest first: [{'date': datetime|None, 'author': str, 'message': str}].

    Other entries on the same endpoint (e.g. emoji reactions) are skipped.
    """
    comments = []
    offset = 0
    while True:
        body = ('<?xml version="1.0" encoding="utf-8"?>'
                '<oc:filter-comments xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
                f'<oc:limit>{COMMENTS_PAGE_SIZE}</oc:limit><oc:offset>{offset}</oc:offset>'
                '</oc:filter-comments>')
        response = _request('REPORT', f'{nextcloud_url}/remote.php/dav/comments/files/{fileid}',
                            data=body.encode('utf-8'), headers={'Content-Type': 'application/xml'})
        entries = ET.fromstring(response.content).findall('d:response', NS)
        for entry in entries:
            if _prop(entry, 'oc:verb') != 'comment':
                continue
            message = (_prop(entry, 'oc:message') or '').strip()
            if message:
                comments.append({
                    'date': _parse_date(_prop(entry, 'oc:creationDateTime')),
                    'author': _prop(entry, 'oc:actorDisplayName') or _prop(entry, 'oc:actorId'),
                    'message': message,
                })
        if len(entries) < COMMENTS_PAGE_SIZE:
            break
        offset += COMMENTS_PAGE_SIZE
    comments.sort(key=lambda c: c['date'].timestamp() if c['date'] else 0)
    return comments


def format_comments(comments):
    """One line per comment: 'YYYY-MM-DD: message'."""
    return '\n'.join(
        f"{c['date'].strftime('%Y-%m-%d')}: {c['message']}" if c['date'] else c['message']
        for c in comments
    )
