"""In-process unit tests of immich_client against the fake Immich server."""

import pytest

from fakes.servers import IMMICH_KEY


@pytest.fixture
def immich_env():
    return {'IMMICH_APIURL': 'http://immich.invalid/api', 'IMMICH_APIKEY': 'k'}


# --- merge_description ------------------------------------------------------

@pytest.mark.parametrize('current, addition, prepend, expected', [
    ('AI text', 'Comments', True, 'Comments\n\nAI text'),
    ('Comments', 'AI text', False, 'Comments\n\nAI text'),
    # already contained: unchanged, in either direction
    ('Comments\n\nAI text', 'AI text', False, 'Comments\n\nAI text'),
    ('Comments\n\nAI text', 'Comments', True, 'Comments\n\nAI text'),
    # whitespace around the parts is ignored
    ('  AI text \n', ' AI text ', False, 'AI text'),
    # empty or missing values
    ('', 'new', False, 'new'),
    (None, 'new', True, 'new'),
    ('old', '', True, 'old'),
    ('old', None, False, 'old'),
    (None, None, False, ''),
    ('  ', '  ', True, ''),
])
def test_merge_description(load, immich_env, current, addition, prepend, expected):
    immich, _ = load(immich_env, nextcloud=False)
    assert immich.merge_description(current, addition, prepend=prepend) == expected


def test_merge_description_is_idempotent(load, immich_env):
    immich, _ = load(immich_env, nextcloud=False)
    once = immich.merge_description('AI', 'C1\nC2', prepend=True)
    assert immich.merge_description(once, 'C1\nC2', prepend=True) == once


# --- add_description size guard ----------------------------------------------

def test_add_description_size_guard(load, server):
    immich, _ = load(server.env(), nextcloud=False)
    aid = server.add_asset(b'x')
    limit = immich.MAX_DESCRIPTION_BYTES
    assert limit < 10 * 1024 * 1024
    with pytest.raises(ValueError, match='larger than'):
        immich.add_description(aid, 'a' * (limit + 1))
    # The limit counts UTF-8 bytes, not characters
    with pytest.raises(ValueError):
        immich.add_description(aid, 'ä' * (limit // 2 + 1))
    assert server.description_writes == []
    # Exactly at the limit is fine (and is sent)
    immich.add_description(aid, 'a' * limit)
    assert len(server.assets[aid]['description']) == limit


# --- v2 / v3 branching -------------------------------------------------------

@pytest.mark.parametrize('version, method', [
    ((2, 0, 0), 'PUT'),
    ((2, 7, 5), 'PUT'),
    ((3, 0, 0), 'PATCH'),
    ((3, 4, 1), 'PATCH'),
])
def test_add_description_method(load, make_server, version, method):
    srv = make_server(version=version)
    immich, _ = load(srv.env(), nextcloud=False)
    aid = srv.add_asset(b'x')
    immich.add_description(aid, 'hello')
    assert srv.description_writes == [(method, aid, 'hello')]


def test_server_version_is_cached(load, server):
    immich, _ = load(server.env(), nextcloud=False)
    assert immich.server_version() == (3, 4, 1)
    assert immich.server_version() == (3, 4, 1)
    assert server.requests.count(('GET', '/api/server/version')) == 1


@pytest.mark.parametrize('version', [(2, 0, 0), (3, 1, 0), (3, 2, 0), (3, 4, 1)])
def test_list_images_pagination(load, make_server, version):
    srv = make_server(version=version, search_page_cap=2)
    immich, _ = load(srv.env(), nextcloud=False)
    ids = [srv.add_asset(bytes([i]), filename=f'{i}.jpg', album='A') for i in range(5)]
    srv.add_asset(b'video', asset_type='VIDEO', album='A')
    srv.add_asset(b'other album', album='B')
    album_id = [i for i, a in srv.albums.items() if a['albumName'] == 'A'][0]

    assert [a['id'] for a in immich.list_images(album_id)] == ids
    assert len(srv.searches) == 3  # 2 + 2 + 1
    first, second = srv.searches[0], srv.searches[1]
    if version >= (3, 2, 0):
        assert first == {'filter': {'albumIds': {'any': [album_id]}, 'type': {'eq': 'IMAGE'}},
                         'withExif': True, 'size': 1000}
        assert second['cursor'] == '2' and 'page' not in second
    else:
        assert first == {'albumIds': [album_id], 'type': 'IMAGE', 'withExif': True,
                         'size': 1000, 'page': 1}
        assert second['page'] == 2 and 'cursor' not in second
    assert [s.get('cursor', s.get('page')) for s in srv.searches[1:]] == (
        ['2', '4'] if version >= (3, 2, 0) else [2, 3])


@pytest.mark.parametrize('version, device_fields', [((2, 0, 0), True), ((3, 0, 0), False), ((3, 4, 1), False)])
def test_upload_fields(load, make_server, tmp_path, version, device_fields):
    srv = make_server(version=version)
    immich, _ = load(srv.env(), nextcloud=False)
    f = tmp_path / 'local.jpg'
    f.write_bytes(b'JPEGDATA')
    result = immich.upload_file(str(f), filename='photo.jpg', mtime=1262304000)
    assert result['status'] == 'created'
    [fields] = srv.uploads
    assert fields['filename'] == 'photo.jpg'
    assert fields['fileCreatedAt'] == fields['fileModifiedAt'] == '2010-01-01T00:00:00+00:00'
    assert ('deviceAssetId' in fields) == device_fields
    assert ('deviceId' in fields) == device_fields
    if device_fields:
        assert fields['deviceAssetId'] == 'photo.jpg-1262304000'
    assert srv.assets[result['id']]['content'] == b'JPEGDATA'


def test_asset_status(load, server):
    immich, _ = load(server.env(), nextcloud=False)
    active = server.add_asset(b'x')
    trashed = server.add_asset(b'y', trashed=True)
    assert immich.asset_status(active) == 'active'
    assert immich.asset_status(trashed) == 'trashed'
    assert immich.asset_status('00000000-0000-0000-0000-000000000000') is None


def test_restore_assets(load, server, monkeypatch):
    immich, _ = load(server.env(), nextcloud=False)
    monkeypatch.setattr(immich._chunks, '__defaults__', (2,))
    ids = [server.add_asset(bytes([i]), trashed=True) for i in range(3)]
    immich.restore_assets(ids)
    assert server.restored == ids
    assert [r for r in server.requests if r[1] == '/api/trash/restore/assets'] == [
        ('POST', '/api/trash/restore/assets')] * 2  # chunked
    assert all(immich.asset_status(i) == 'active' for i in ids)
    server.restore_status = 403
    with pytest.raises(Exception, match='403'):
        immich.restore_assets(ids)


def test_bulk_upload_check(load, server, tmp_path):
    immich, _ = load(server.env(), nextcloud=False)
    server.add_asset(b'known')
    (tmp_path / 'a.jpg').write_bytes(b'known')
    (tmp_path / 'b.jpg').write_bytes(b'new')
    results = immich.bulk_upload_check([str(tmp_path / 'a.jpg'), str(tmp_path / 'b.jpg')])['results']
    assert [(r['id'], r['action']) for r in results] == [
        (str(tmp_path / 'a.jpg'), 'reject'), (str(tmp_path / 'b.jpg'), 'accept')]


def test_api_key_header(load, server):
    immich, _ = load({**server.env(), 'IMMICH_APIKEY': 'wrong'}, nextcloud=False)
    with pytest.raises(Exception, match='401'):
        immich.list_albums()
    immich, _ = load({**server.env(), 'IMMICH_APIKEY': IMMICH_KEY}, nextcloud=False)
    assert immich.list_albums() == []


# --- setting() -----------------------------------------------------------------

CONFIG = """\
[immich]
apiurl = http://from-config/api
apikey = config-key

[nextcloud]
url = https://cloud.example.com
user = bob
password = p%40ss%word
"""


def test_setting_from_config_ini(load):
    immich, _ = load({}, config_ini=CONFIG, nextcloud=False)
    assert immich.immich_apiurl == 'http://from-config/api'
    assert immich.setting('nextcloud', 'password') == 'p%40ss%word'  # no interpolation
    assert immich.setting('nextcloud', 'folder', '') == ''
    assert immich.setting('llm', 'model', None) is None  # missing section


def test_setting_env_overrides_config_ini(load):
    immich, nc = load({'IMMICH_APIKEY': 'env%key', 'NEXTCLOUD_PASSWORD': '100%',
                       'LLM_MODEL': 'gemma3:27b', 'NEXTCLOUD_FOLDER': 'Photos/2019'}, config_ini=CONFIG)
    assert immich.immich_apikey == 'env%key'
    assert immich.immich_apiurl == 'http://from-config/api'  # not overridden
    assert immich.setting('nextcloud', 'password') == '100%'
    assert immich.setting('nextcloud', 'user') == 'bob'
    assert immich.setting('llm', 'model') == 'gemma3:27b'  # section only in env
    assert immich.setting('nextcloud', 'folder') == 'Photos/2019'
    assert nc.nextcloud_password == '100%'


def test_setting_without_config_ini(load):
    immich, _ = load({'IMMICH_APIURL': 'http://env/api', 'IMMICH_APIKEY': 'k'}, nextcloud=False)
    assert immich.immich_apiurl == 'http://env/api'


def test_setting_ignores_bare_prefix(load):
    immich, _ = load({'IMMICH_APIURL': 'u', 'IMMICH_APIKEY': 'k', 'LLM_': 'x'}, nextcloud=False)
    assert not immich.config.has_section('llm')


def test_missing_setting_exits(load):
    immich, _ = load({'IMMICH_APIURL': 'u', 'IMMICH_APIKEY': 'k'}, nextcloud=False)
    with pytest.raises(SystemExit) as exc:
        immich.setting('llm', 'model')
    assert 'model' in str(exc.value.code) and 'LLM_MODEL' in str(exc.value.code)


def test_missing_setting_at_import_exits(load):
    with pytest.raises(SystemExit) as exc:
        load({'IMMICH_APIURL': 'u'}, nextcloud=False)
    assert 'IMMICH_APIKEY' in str(exc.value.code)
