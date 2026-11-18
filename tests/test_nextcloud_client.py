"""In-process unit tests of nextcloud_client against the fake Nextcloud."""

import io
from datetime import datetime, timedelta, timezone

import pytest
from email.utils import formatdate

from fakes.servers import sha1_hex


@pytest.fixture
def nc_env():
    return {'IMMICH_APIURL': 'http://immich.invalid/api', 'IMMICH_APIKEY': 'k',
            'NEXTCLOUD_URL': 'https://cloud.invalid', 'NEXTCLOUD_USER': 'u', 'NEXTCLOUD_PASSWORD': 'p'}


# --- _parse_date / format_comments ----------------------------------------------

@pytest.mark.parametrize('value, expected', [
    ('Mon, 18 Jan 2016 22:01:16 GMT', datetime(2016, 1, 18, 22, 1, 16, tzinfo=timezone.utc)),
    ('Mon, 18 Jan 2016 23:01:16 +0100', datetime(2016, 1, 18, 22, 1, 16, tzinfo=timezone.utc)),
    ('2019-07-01 12:00:00', datetime(2019, 7, 1, 12, 0, 0)),
    ('2019-07-01T12:00:00+02:00', datetime(2019, 7, 1, 10, 0, 0, tzinfo=timezone.utc)),
    (None, None),
    ('', None),
    ('not a date', None),
])
def test_parse_date(load, nc_env, value, expected):
    _, nc = load(nc_env)
    assert nc._parse_date(value) == expected


def test_format_comments(load, nc_env):
    _, nc = load(nc_env)
    comments = [
        {'date': datetime(2016, 1, 16, 10, tzinfo=timezone.utc), 'author': 'a', 'message': 'First\nsecond line'},
        {'date': None, 'author': 'a', 'message': 'undated'},
        {'date': datetime(2019, 7, 1, 12), 'author': 'a', 'message': 'Urlaub'},
    ]
    assert nc.format_comments(comments) == '2016-01-16: First\nsecond line\nundated\n2019-07-01: Urlaub'
    assert nc.format_comments([]) == ''


# --- get_comments ------------------------------------------------------------------

def test_comments_paginate_past_100_and_skip_reactions(load, make_server):
    base = 1600000000
    entries = []
    for i in range(250):
        if i % 10 == 0:
            entries.append((formatdate(base + i, usegmt=True), 'reaction', '\U0001F44D'))
        else:
            entries.append((formatdate(base + i, usegmt=True), 'comment', f'm{i:03d} & <x>'))
    entries.reverse()  # newest first, like Nextcloud
    srv = make_server(files={'/F/x.jpg': dict(content=b'x', type='image/jpeg', fileid='42', comments=entries)})
    _, nc = load(srv.env())

    comments = nc.get_comments('42')
    assert [(o, n) for _, o, n in srv.reports] == [(0, 100), (100, 100), (200, 100)]
    assert len(comments) == 225
    assert comments[0]['message'] == 'm001 & <x>'  # oldest first, XML unescaped
    assert comments[-1]['message'] == 'm249 & <x>'
    assert all(c['author'] == 'alice' for c in comments)
    assert comments == sorted(comments, key=lambda c: c['date'])


def test_comments_exactly_one_page(load, make_server):
    entries = [(formatdate(1600000000 + i, usegmt=True), 'comment', f'm{i}') for i in range(100)]
    srv = make_server(files={'/F/x.jpg': dict(content=b'x', type='image/jpeg', fileid='7', comments=entries)})
    _, nc = load(srv.env())
    assert len(nc.get_comments('7')) == 100
    assert [o for _, o, _ in srv.reports] == [0, 100]


def test_comments_default_data(load, server):
    _, nc = load(server.env())
    comments = nc.get_comments('11')
    assert [c['message'] for c in comments] == ['Erster Kommentar\nzweite Zeile', 'Oma in Rom']
    assert nc.format_comments(comments) == '2016-01-16: Erster Kommentar\nzweite Zeile\n2016-01-18: Oma in Rom'
    assert len(nc.get_comments('15')) == 101
    assert nc.get_comments('999') == []


# --- list_media / download with and without sub-path ---------------------------------

@pytest.mark.parametrize('prefix, url_suffix', [('/nextcloud', ''), ('/nextcloud', '/'), ('', ''), ('', '/')])
def test_list_media_and_download(load, make_server, prefix, url_suffix):
    srv = make_server(nc_prefix=prefix)
    _, nc = load({**srv.env(), 'NEXTCLOUD_URL': srv.nextcloud_url + url_suffix})

    items = list(nc.list_media('Fotos Alt'))
    assert [i['path'] for i in items] == [
        '/Fotos Alt/a.jpg', '/Fotos Alt/many.png', '/Fotos Alt/Kopie/a-copy.jpg',
        '/Fotos Alt/sub/b.heic', '/Fotos Alt/sub/c.mp4']  # folder by folder, no .txt
    a = items[0]
    assert a['href'] == f'{prefix}/remote.php/dav/files/alice/Fotos%20Alt/a.jpg'
    assert (a['fileid'], a['etag'], a['content_type'], a['comments_count'], a['size']) == (
        '11', 'etag11', 'image/jpeg', 2, 6)
    assert a['mtime'] == datetime(2019, 7, 1, 10, tzinfo=timezone.utc).timestamp()

    buf = io.BytesIO()
    assert nc.download(items[-1]['href'], buf) == sha1_hex(b'MP4-C' * 1000)
    assert buf.getvalue() == b'MP4-C' * 1000

    assert len(nc.get_comments('11')) == 2  # comments endpoint below the sub-path too
    assert all(p.startswith(prefix + '/remote.php/dav/') for _, p in srv.requests)


@pytest.mark.parametrize('folder', ['', '/', 'Fotos Alt/sub/', '/Fotos Alt/sub'])
def test_list_media_folder_forms(load, server, folder):
    _, nc = load(server.env())
    paths = [i['path'] for i in nc.list_media(folder)]
    expected = ['/Fotos Alt/sub/b.heic', '/Fotos Alt/sub/c.mp4']
    assert paths == (expected if 'sub' in folder else [
        '/Fotos Alt/a.jpg', '/Fotos Alt/many.png', '/Fotos Alt/Kopie/a-copy.jpg', *expected])


def test_wrong_password_raises(load, server):
    _, nc = load({**server.env(), 'NEXTCLOUD_PASSWORD': 'wrong'})
    with pytest.raises(Exception, match='401'):
        list(nc.list_media(''))


def test_dates_are_comparable(load, nc_env):
    """Mixed aware (RFC 1123) and naive (ISO) dates must not break sorting."""
    _, nc = load(nc_env)
    a, b = nc._parse_date('Mon, 18 Jan 2016 22:01:16 GMT'), nc._parse_date('2019-07-01 12:00:00')
    assert sorted([b, a], key=lambda d: d.timestamp()) == [a, b]
    assert b - timedelta(0) == b
