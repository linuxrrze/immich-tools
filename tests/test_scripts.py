"""add_image_description.py, list_albums.py and sync_images.py as subprocesses."""

import io

import pytest
from PIL import Image

from fakes.llm import ANSWER, MODEL


def png(red):
    buf = io.BytesIO()
    Image.new('RGB', (4, 4), (red, 0, 0)).save(buf, 'PNG')
    return buf.getvalue()


@pytest.fixture
def describe(make_server, llm, script):
    """Album 'Alb' with three images (one with a description, one from an
    external library), a video, and a second album; search pages of 2."""
    def setup(version):
        srv = make_server(version=version, search_page_cap=2)
        ids = [
            srv.add_asset(png(10), filename='f0.png', album='Alb'),
            srv.add_asset(png(20), description='2016-01-18: Oma in Rom', filename='f1.png', album='Alb'),
            srv.add_asset(png(30), filename='lib.png', album='Alb', library_path='/ext/lib.png'),
        ]
        srv.add_asset(b'video', asset_type='VIDEO', filename='v.mp4', album='Alb')
        other = srv.add_asset(png(40), filename='other.png', album='Other')
        script.env = {**srv.env(), 'LLM_BASE_URL': llm.base_url, 'LLM_API_KEY': 'llm-key',
                      'LLM_MODEL': MODEL, 'LLM_PROMPT': 'Describe the image'}
        return srv, ids, other
    return setup


@pytest.mark.parametrize('version', [(2, 0, 0), (3, 1, 0), (3, 4, 1)])
def test_add_image_description(describe, llm, script, tmp_path, version):
    srv, ids, other = describe(version)

    result = script.run('add_image_description.py', 'Alb')
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Available models: ['{MODEL}']" in result.stdout
    assert len(llm.requests) == 3
    assert {r['auth'] for r in llm.requests} == {'Bearer llm-key'}
    assert {r['body']['model'] for r in llm.requests} == {MODEL}
    # The existing description is passed to the model as context
    prompts = llm.prompts()
    assert 'Alb/f1.png' in prompts[1] and 'Oma in Rom' in prompts[1]
    assert 'Additional information' not in prompts[0]

    # Appended after existing text; empty descriptions just get the answer
    assert srv.assets[ids[0]]['description'] == ANSWER
    assert srv.assets[ids[1]]['description'] == f'2016-01-18: Oma in Rom\n\n{ANSWER}'
    assert srv.assets[ids[2]]['description'] == ANSWER
    assert srv.assets[other]['description'] == ''  # other album not selected
    method = 'PATCH' if version >= (3, 0, 0) else 'PUT'
    assert {w[0] for w in srv.description_writes} == {method}
    assert len(srv.description_writes) == 3
    cache = sorted(p.name for p in (tmp_path / 'cache').iterdir())
    assert len(cache) == 3
    assert all(name.endswith('-Qwen_Qwen2.5-VL-7B-Instruct.description') for name in cache)

    # Second run: everything from the cache, nothing written, no duplicates
    result = script.run('add_image_description.py', 'Alb')
    assert result.returncode == 0, result.stderr
    assert len(llm.requests) == 3
    assert len(srv.description_writes) == 3
    assert result.stdout.count('AI description already in Immich') == 3

    # Description removed in Immich: the cached text is added again, no LLM call
    srv.assets[ids[1]]['description'] = '2016-01-18: Oma in Rom'
    result = script.run('add_image_description.py', 'Alb')
    assert result.returncode == 0, result.stderr
    assert len(llm.requests) == 3
    assert srv.assets[ids[1]]['description'] == f'2016-01-18: Oma in Rom\n\n{ANSWER}'
    assert len(srv.description_writes) == 4


def test_add_image_description_all_albums_and_broken_file(describe, llm, script):
    srv, ids, other = describe((3, 4, 1))
    srv.assets[ids[0]]['content'] = png(99)  # does not match the stored checksum
    result = script.run('add_image_description.py')
    assert result.returncode == 0, result.stderr
    assert 'ERROR: Broken file f0.png' in result.stdout
    assert srv.assets[ids[0]]['description'] == ''
    assert srv.assets[other]['description'] == ANSWER
    assert len(llm.requests) == 3


def test_add_image_description_missing_setting(server, script):
    script.env = server.env()
    result = script.run('add_image_description.py')
    assert result.returncode == 1
    assert 'LLM_BASE_URL' in result.stderr


def test_list_albums(server, script):
    a = server.add_album('Urlaub 2019')
    b = server.add_album('Fotos/2020')
    script.env = server.env()
    result = script.run('list_albums.py')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [f'Album: {a}, Urlaub 2019', f'Album: {b}, Fotos/2020']


def test_list_albums_without_config(script):
    result = script.run('list_albums.py')
    assert result.returncode == 1
    assert 'IMMICH_APIURL' in result.stderr


def test_sync_images(server, script, tmp_path):
    photos = tmp_path / 'photos'
    (photos / 'sub').mkdir(parents=True)
    (photos / 'known.jpg').write_bytes(b'known')
    (photos / 'sub' / 'new.PNG').write_bytes(b'new')
    (photos / 'notes.txt').write_bytes(b'known')
    for i in range(105):  # more than one batch of 100
        (photos / f'n{i}.jpg').write_bytes(f'n{i}'.encode())
    server.add_asset(b'known')
    script.env = server.env()
    result = script.run('sync_images.py', str(photos))
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == 'Accepted: 106 / Rejected: 1'
    assert server.uploads == []  # only checks, never uploads


def test_sync_images_usage(server, script):
    script.env = server.env()
    result = script.run('sync_images.py')
    assert result.returncode == 1
    assert 'Usage' in result.stdout
