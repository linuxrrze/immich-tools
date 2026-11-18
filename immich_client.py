"""Shared Immich API helpers and config loading for the immich-tools scripts.

Checked against the Immich OpenAPI spec of v2.0.0 and v3.4.1
(https://docs.immich.app/api, https://api.immich.app/).
"""

import os
import configparser
from datetime import datetime, timezone
from hashlib import sha1

import requests

config = configparser.ConfigParser()
config.read('config.ini')

immich_apiurl = config['immich']['apiurl']
immich_apikey = config['immich']['apikey']

# Page size for /search/metadata (server maximum is 1000)
SEARCH_PAGE_SIZE = 1000

# Stay clearly below the server's 10 MB JSON body limit
MAX_DESCRIPTION_BYTES = 9 * 1024 * 1024

# Asset IDs per bulk request (albums, tags)
BULK_CHUNK_SIZE = 1000


def _headers():
    return {
        'Accept': 'application/json',
        'x-api-key': immich_apikey
    }


def _get(path, **kwargs):
    response = requests.get(f'{immich_apiurl}{path}', headers=_headers(), **kwargs)
    response.raise_for_status()
    return response


def _put(path, **kwargs):
    response = requests.put(f'{immich_apiurl}{path}', headers=_headers(), **kwargs)
    response.raise_for_status()
    return response.json()


def _chunks(items, size=BULK_CHUNK_SIZE):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _post(path, **kwargs):
    response = requests.post(f'{immich_apiurl}{path}', headers=_headers(), **kwargs)
    response.raise_for_status()
    return response.json()


_server_version = None


def server_version():
    """(major, minor, patch) of the Immich server, fetched once."""
    global _server_version
    if _server_version is None:
        v = _get('/server/version').json()
        _server_version = (v['major'], v['minor'], v['patch'])
    return _server_version


def add_description(image_id, description):
    # Immich stores the description as `text` (no length limit) and the API
    # sets no maximum, but the server rejects JSON bodies over 10 MB
    # (s. server/src/app.common.ts). Fail with a clear message instead.
    if len(description.encode('utf-8')) > MAX_DESCRIPTION_BYTES:
        raise ValueError(f'Description of asset {image_id} is larger than '
                         f'{MAX_DESCRIPTION_BYTES} bytes')
    # PATCH /assets/{id} exists since v3.0.0 and becomes the default in v4;
    # PUT is dropped in v5. Older servers only support PUT.
    method = requests.patch if server_version() >= (3, 0, 0) else requests.put
    response = method(
        f'{immich_apiurl}/assets/{image_id}', headers=_headers(), json={'description': description})
    response.raise_for_status()
    return response.json()


def get_asset(asset_id):
    """Asset details including exifInfo.description."""
    return _get(f'/assets/{asset_id}').json()


def merge_description(current, addition, prepend=False):
    """Combine an existing description with new text without losing either.

    Returns `current` unchanged if `addition` is already part of it, so
    running a tool again does not duplicate text.
    """
    current = (current or '').strip()
    addition = (addition or '').strip()
    if not addition or addition in current:
        return current
    if not current:
        return addition
    return f'{addition}\n\n{current}' if prepend else f'{current}\n\n{addition}'


def list_albums():
    return _get('/albums').json()


def create_album(name):
    return _post('/albums', json={'albumName': name})


def add_to_album(album_id, asset_ids):
    """Add assets to an album. Assets already in it are reported by the
    server as duplicates and left alone, so this is safe to repeat."""
    for chunk in _chunks(list(asset_ids)):
        _put(f'/albums/{album_id}/assets', json={'ids': chunk})


def upsert_tags(names):
    """Create tags if missing and return {name: tag id}.

    A '/' in a name creates nested tags ('Nextcloud/2019/Rome' creates
    'Nextcloud', 'Nextcloud/2019' and 'Nextcloud/2019/Rome'); existing tags
    are reused (s. server/src/utils/tag.ts).
    """
    names = list(dict.fromkeys(names))
    if not names:
        return {}
    return {tag['value']: tag['id'] for tag in _put('/tags', json={'tags': names})}


def tag_assets(tag_id, asset_ids):
    """Assign a tag to assets; repeating it does not create duplicates."""
    for chunk in _chunks(list(asset_ids)):
        _put('/tags/assets', json={'tagIds': [tag_id], 'assetIds': chunk})


def list_images(album_id):
    """Yield all image assets (with EXIF info) of an album.

    Albums no longer include their assets since Immich v3 (the `withoutAssets`
    parameter was removed), so the assets are fetched via POST /search/metadata.
    The structured `filter`/`cursor` syntax exists since v3.2.0; older servers
    get the (now deprecated) flat `albumIds`/`page` fields.
    """
    if server_version() >= (3, 2, 0):
        body = {
            'filter': {'albumIds': {'any': [album_id]}, 'type': {'eq': 'IMAGE'}},
            'withExif': True,
            'size': SEARCH_PAGE_SIZE,
        }
        while True:
            assets = _post('/search/metadata', json=body)['assets']
            yield from assets['items']
            if not assets.get('nextCursor'):
                break
            body['cursor'] = assets['nextCursor']
    else:
        body = {
            'albumIds': [album_id],
            'type': 'IMAGE',
            'withExif': True,
            'size': SEARCH_PAGE_SIZE,
            'page': 1,
        }
        while True:
            assets = _post('/search/metadata', json=body)['assets']
            yield from assets['items']
            if not assets.get('nextPage'):
                break
            body['page'] = int(assets['nextPage'])


def download_image(image_id):
    response = _get(f'/assets/{image_id}/original', stream=True)
    image_bytes = bytearray()
    for chunk in response.iter_content(16384):
        image_bytes += chunk
    return image_bytes


def is_library_asset(asset):
    """True for assets from an external library.

    Their checksum is sha1("path:<originalPath>") instead of the file hash
    (s. server/src/services/library.service.ts).
    """
    return bool(asset.get('libraryId'))


def check_checksums(assets):
    """POST /assets/bulk-upload-check for [{'id': <client id>, 'checksum': <sha1 hex>}].

    Each result has `action` (accept/reject); rejected duplicates carry the
    existing `assetId`.
    """
    return _post('/assets/bulk-upload-check', json={'assets': assets})


def bulk_upload_check(files):
    """Ask the server which of the given local files are already uploaded.

    The full path is used as client-side id so files with the same name in
    different folders can be told apart.
    """
    assets = []
    for file in files:
        with open(file, 'rb') as f:
            sha1sum = sha1(f.read()).hexdigest()
        assets.append({'id': file, 'checksum': sha1sum})
    return check_checksums(assets)


def upload_file(file, filename=None, mtime=None):
    """Upload a local file. `filename` and `mtime` (Unix time) default to the
    file's own name and modification time."""
    if mtime is None:
        mtime = os.stat(file).st_mtime
    filename = filename or os.path.basename(file)
    data = {
        'fileCreatedAt': datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
        'fileModifiedAt': datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
        'filename': filename,
        'isFavorite': 'false',
    }
    # deviceAssetId/deviceId were required before v3 and removed in v3
    if server_version() < (3, 0, 0):
        data['deviceAssetId'] = f'{filename}-{mtime}'
        data['deviceId'] = 'python'
    with open(file, 'rb') as f:
        return _post('/assets', data=data, files={'assetData': f})
