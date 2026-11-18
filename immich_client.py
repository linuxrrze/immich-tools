"""Shared Immich API helpers and config loading for the immich-tools scripts.

Checked against the Immich OpenAPI spec of v2.0.0 and v3.4.1
(https://docs.immich.app/api, https://api.immich.app/).
"""

import os
import configparser
from datetime import datetime
from hashlib import sha1

import requests

config = configparser.ConfigParser()
config.read('config.ini')

immich_apiurl = config['immich']['apiurl']
immich_apikey = config['immich']['apikey']

# Page size for /search/metadata (server maximum is 1000)
SEARCH_PAGE_SIZE = 1000


def _headers():
    return {
        'Accept': 'application/json',
        'x-api-key': immich_apikey
    }


def _get(path, **kwargs):
    response = requests.get(f'{immich_apiurl}{path}', headers=_headers(), **kwargs)
    response.raise_for_status()
    return response


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
    # PATCH /assets/{id} exists since v3.0.0 and becomes the default in v4;
    # PUT is dropped in v5. Older servers only support PUT.
    method = requests.patch if server_version() >= (3, 0, 0) else requests.put
    response = method(
        f'{immich_apiurl}/assets/{image_id}', headers=_headers(), json={'description': description})
    response.raise_for_status()
    return response.json()


def list_albums():
    return _get('/albums').json()


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
    return _post('/assets/bulk-upload-check', json={'assets': assets})


def upload_file(file):
    stats = os.stat(file)
    data = {
        'fileCreatedAt': datetime.fromtimestamp(stats.st_mtime).isoformat(),
        'fileModifiedAt': datetime.fromtimestamp(stats.st_mtime).isoformat(),
        'filename': os.path.basename(file),
        'isFavorite': 'false',
    }
    # deviceAssetId/deviceId were required before v3 and removed in v3
    if server_version() < (3, 0, 0):
        data['deviceAssetId'] = f'{file}-{stats.st_mtime}'
        data['deviceId'] = 'python'
    with open(file, 'rb') as f:
        return _post('/assets', data=data, files={'assetData': f})
