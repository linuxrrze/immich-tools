"""migrate_nextcloud.py as a subprocess against the fake Nextcloud + Immich.

Test data (tests/fakes/servers.py, configured folder 'Fotos Alt'):
  Fotos Alt/a.jpg            JPEG-A, 2 comments + a reaction; already in Immich with an AI description
  Fotos Alt/many.png         101 comments
  Fotos Alt/Kopie/a-copy.jpg same content as a.jpg, 1 comment
  Fotos Alt/sub/b.heic       already in Immich with an AI description, no comments
  Fotos Alt/sub/c.mp4        1 comment with an ISO date
  Fotos Alt/sub/notes.txt    not a media file
Downloads happen in this order (folder by folder), which the interrupt
tests rely on.
"""

import fcntl
import json
import signal

import pytest

FOLDER = 'Fotos Alt'
A, MANY, B, C = b'JPEG-A', b'PNG-D', b'HEIC-B', b'MP4-C' * 1000
DOWNLOAD_ORDER = ['a.jpg', 'many.png', 'a-copy.jpg', 'b.heic', 'c.mp4']

DESC_A = ('2016-01-20: Kopie: Rom 2016\n\n'
          '2016-01-16: Erster Kommentar\nzweite Zeile\n2016-01-18: Oma in Rom\n\n'
          'AI: two people')
# Unix time 1600000000 + i minutes, all on 2020-09-13 (UTC)
DESC_MANY = '\n'.join(f'2020-09-13: c{i:03d}' for i in range(101))
DESC_C = '2019-07-01: Urlaub'


def seed(srv):
    """Immich already has a.jpg and b.heic (with AI descriptions) and an
    album named like the root folder."""
    srv.add_asset(A, description='AI: two people', filename='a.jpg')
    srv.add_asset(B, description='AI: a cat', filename='b.heic')
    srv.add_album(FOLDER)
    return srv


@pytest.fixture
def srv(server):
    return seed(server)


@pytest.fixture
def mig(srv, script):
    script.env = {**srv.env(), 'NEXTCLOUD_FOLDER': FOLDER}
    return script


def run(script, *args, env=None, expect=0):
    result = script.run('migrate_nextcloud.py', *args, env=env)
    assert result.returncode == expect, result.stdout + result.stderr
    return result


def downloads(srv):
    return [p.rsplit('/', 1)[-1] for m, p in srv.requests if m == 'GET' and p.startswith(srv.nc_root)]


def album(srv, name):
    return sorted(srv.album_by_name(name)['assets'])


def ids(srv, *contents):
    return sorted(srv.asset_by_checksum(c) for c in contents)


def check_complete(srv, tags=('Nextcloud', 'Nextcloud/Kopie', 'Nextcloud/sub'),
                   albums=(FOLDER, 'Kopie', 'sub')):
    """Every file is migrated once, in the right album and tag."""
    root, kopie, sub = albums
    assert len(srv.assets) == 4
    assert srv.album_names() == sorted(albums)
    assert album(srv, root) == ids(srv, A, MANY)
    assert album(srv, kopie) == ids(srv, A)
    assert album(srv, sub) == ids(srv, B, C)
    assert sorted(srv.tagged(tags[0])) == ids(srv, A, MANY)
    assert sorted(srv.tagged(tags[1])) == ids(srv, A)
    assert sorted(srv.tagged(tags[2])) == ids(srv, B, C)
    desc = {c: srv.assets[srv.asset_by_checksum(c)]['description'] for c in (A, MANY, B, C)}
    assert desc == {A: DESC_A, MANY: DESC_MANY, B: 'AI: a cat', C: DESC_C}


# --- complete runs -------------------------------------------------------------

@pytest.mark.parametrize('version', [(2, 0, 0), (3, 4, 1)])
def test_migration_and_rerun(make_server, script, tmp_path, version):
    srv = seed(make_server(version=version))
    script.env = {**srv.env(), 'NEXTCLOUD_FOLDER': FOLDER}
    pre_album = srv.album_by_name(FOLDER)

    out = run(script).stdout
    check_complete(srv)
    assert srv.album_by_name(FOLDER) is pre_album  # existing album reused
    assert len(srv.uploads) == 2  # a.jpg and b.heic were already there
    assert downloads(srv) == DOWNLOAD_ORDER
    method = 'PATCH' if version >= (3, 0, 0) else 'PUT'
    assert {w[0] for w in srv.description_writes} == {method}
    assert set(srv.tags) == {'Nextcloud', 'Nextcloud/Kopie', 'Nextcloud/sub'}
    # Comments: two REPORT pages for 101 comments, none for files without comments
    assert [(f, o) for f, o, _ in srv.reports] == [('11', 0), ('15', 0), ('15', 100), ('16', 0), ('13', 0)]
    assert "FOLDER    Fotos Alt/sub: 2 asset(s) in album 'sub' and tag 'Nextcloud/sub'" in out
    assert 'notes.txt' not in out
    state = json.loads((tmp_path / 'cache' / 'nextcloud-migration.json').read_text())
    assert sorted(state) == ['11', '12', '13', '15', '16']
    assert json.loads((tmp_path / 'cache' / 'nextcloud-layout.json').read_text()) == {
        'albums': 'strip', 'tags': 'strip'}

    # Second run: nothing downloaded, uploaded, written or added twice
    writes, n_requests = len(srv.description_writes), len(srv.requests)
    out = run(script).stdout
    check_complete(srv)
    assert len(srv.uploads) == 2
    assert len(srv.description_writes) == writes
    assert not [p for m, p in srv.requests[n_requests:] if m == 'GET' and p.startswith(srv.nc_root)]
    assert out.count('UNCHANGED') == 5
    assert 'comments: 0' in out and 'failed: 0' in out
    assert srv.album_renames == [] and srv.deleted_tags == []


def test_duplicate_file_is_one_asset_in_two_albums(make_server, script):
    files = {
        '/P/x/one.jpg': dict(content=b'SAME', type='image/jpeg', fileid='1',
                             comments=[('Mon, 18 Jan 2016 22:01:16 GMT', 'comment', 'from x')]),
        '/P/y/two.jpg': dict(content=b'SAME', type='image/jpeg', fileid='2',
                             comments=[('Tue, 19 Jan 2016 22:01:16 GMT', 'comment', 'from y')]),
    }
    srv = make_server(files=files)
    script.env = {**srv.env(), 'NEXTCLOUD_FOLDER': 'P'}
    out = run(script).stdout
    assert 'CREATED   /P/x/one.jpg' in out and 'EXISTING  /P/y/two.jpg' in out
    [asset] = srv.assets
    assert album(srv, 'x') == album(srv, 'y') == [asset]
    assert srv.tagged('Nextcloud/x') == srv.tagged('Nextcloud/y') == [asset]
    assert srv.assets[asset]['description'] == '2016-01-19: from y\n\n2016-01-18: from x'
    run(script)
    assert len(srv.assets) == 1 and len(srv.uploads) == 1
    assert srv.assets[asset]['description'] == '2016-01-19: from y\n\n2016-01-18: from x'


def test_deleted_asset_is_uploaded_again(srv, mig, tmp_path):
    run(mig)
    old_c = srv.asset_by_checksum(C)
    del srv.assets[old_c]  # deleted permanently in Immich
    out = run(mig).stdout
    assert f'GONE      /Fotos Alt/sub/c.mp4: asset {old_c} was deleted in Immich' in out
    assert len(srv.uploads) == 3
    new_c = srv.asset_by_checksum(C)
    assert new_c != old_c
    assert srv.assets[new_c]['description'] == DESC_C
    assert new_c in album(srv, 'sub') and new_c in srv.tagged('Nextcloud/sub')
    state = json.loads((tmp_path / 'cache' / 'nextcloud-migration.json').read_text())
    assert state['13']['asset_id'] == new_c
    assert downloads(srv).count('c.mp4') == 2 and downloads(srv).count('b.heic') == 1


def test_new_comment_is_added_on_rerun(srv, mig):
    run(mig)
    srv.files['/Fotos Alt/sub/c.mp4']['comments'].append(('Fri, 05 Jul 2019 10:00:00 GMT', 'comment', 'Strand'))
    run(mig)
    # The new text block goes in front; the old line stays
    assert srv.assets[srv.asset_by_checksum(C)]['description'] == (
        '2019-07-01: Urlaub\n2019-07-05: Strand\n\n2019-07-01: Urlaub')


# --- assets in the Immich trash ------------------------------------------------------

SUMMARY_RESTORED = 'restored: 1'


def test_known_trashed_asset_is_restored(srv, mig):
    """(A) A migrated asset was moved to the trash: restored, not uploaded again."""
    run(mig)
    c = srv.asset_by_checksum(C)
    srv.assets[c]['trashed'] = True
    out = run(mig).stdout
    assert 'RESTORED  /Fotos Alt/sub/c.mp4' in out and SUMMARY_RESTORED in out
    assert srv.restored == [c] and not srv.assets[c]['trashed']
    assert len(srv.uploads) == 2
    check_complete(srv)


def test_trashed_duplicate_is_restored(srv, mig):
    """(B) Unknown to the state file, but Immich has it in the trash."""
    c = srv.add_asset(C, filename='c.mp4', trashed=True)
    out = run(mig).stdout
    assert 'RESTORED  /Fotos Alt/sub/c.mp4' in out and SUMMARY_RESTORED in out
    assert srv.restored == [c] and not srv.assets[c]['trashed']
    assert len(srv.uploads) == 1  # only many.png
    check_complete(srv)
    run(mig)
    assert srv.restored == [c]


def test_restore_trashed_no_skips(srv, mig):
    """(C) restore_trashed = no, for a trashed duplicate and a known asset."""
    c = srv.add_asset(C, filename='c.mp4', trashed=True)
    no = {'NEXTCLOUD_RESTORE_TRASHED': 'no'}
    out = run(mig, env=no).stdout
    assert 'SKIP /Fotos Alt/sub/c.mp4: in the Immich trash (restore_trashed = no)' in out
    assert 'skipped: 1' in out
    assert srv.restored == [] and srv.assets[c]['trashed']
    assert album(srv, 'sub') == ids(srv, B)
    assert c not in srv.tagged('Nextcloud/sub')

    # Known asset (in the state file) trashed later
    run(mig)
    b = srv.asset_by_checksum(B)
    srv.assets[b]['trashed'] = True
    out = run(mig, env=no).stdout
    assert 'SKIP /Fotos Alt/sub/b.heic: in the Immich trash (restore_trashed = no)' in out
    assert srv.restored == [c] and srv.assets[b]['trashed']


def test_failing_restore(srv, mig):
    c = srv.add_asset(C, filename='c.mp4', trashed=True)
    srv.restore_status = 403
    out = run(mig, expect=1).stdout
    assert f'ERROR /Fotos Alt/sub/c.mp4: restoring asset {c} from the Immich trash failed' in out
    assert 'asset.delete' in out and 'failed: 1' in out
    assert srv.assets[c]['trashed'] and album(srv, 'sub') == ids(srv, B)
    srv.restore_status = None
    run(mig)
    check_complete(srv)


# --- album and tag names -----------------------------------------------------------

def test_strip_folder_no_keeps_full_paths(srv, mig, tmp_path):
    run(mig, env={'NEXTCLOUD_STRIP_FOLDER': 'no'})
    full = (FOLDER, f'{FOLDER}/Kopie', f'{FOLDER}/sub')
    check_complete(srv, albums=full, tags=tuple(f'Nextcloud/{f}' for f in full))
    assert json.loads((tmp_path / 'cache' / 'nextcloud-layout.json').read_text()) == {
        'albums': 'full', 'tags': 'full'}


def test_subfolder_argument_strips_configured_folder(srv, mig):
    run(mig, 'Fotos Alt/sub')
    assert srv.album_names() == [FOLDER, 'sub']
    assert album(srv, 'sub') == ids(srv, B, C)
    assert set(srv.tags) == {'Nextcloud', 'Nextcloud/sub'}
    assert sorted(srv.tagged('Nextcloud/sub')) == ids(srv, B, C)


def test_folder_outside_configured_folder_keeps_full_path(srv, mig):
    run(mig, 'Fotos Alt/sub', env={'NEXTCLOUD_FOLDER': 'Other'})
    assert srv.album_names() == [FOLDER, 'Fotos Alt/sub']
    assert sorted(srv.tagged('Nextcloud/Fotos Alt/sub')) == ids(srv, B, C)


def test_empty_configured_folder_keeps_full_paths(srv, mig):
    run(mig, env={'NEXTCLOUD_FOLDER': ''})
    full = (FOLDER, f'{FOLDER}/Kopie', f'{FOLDER}/sub')
    check_complete(srv, albums=full, tags=tuple(f'Nextcloud/{f}' for f in full))


def old_version_run(srv, mig, tmp_path):
    """A complete run with full-path names, as before strip_folder existed."""
    run(mig, env={'NEXTCLOUD_STRIP_FOLDER': 'no'})
    (tmp_path / 'cache' / 'nextcloud-layout.json').unlink()
    return {a['albumName']: i for i, a in srv.albums.items()}


def test_switch_from_full_path_names(srv, mig, tmp_path):
    old_albums = old_version_run(srv, mig, tmp_path)
    layout = tmp_path / 'cache' / 'nextcloud-layout.json'

    # A run on a subfolder renames the albums but keeps the old tags
    out = run(mig, 'Fotos Alt/sub').stdout
    assert 'ALBUM     Fotos Alt/Kopie renamed to Kopie' in out
    assert 'ALBUM     Fotos Alt/sub renamed to sub' in out
    assert srv.album_names() == [FOLDER, 'Kopie', 'sub']
    assert {a['albumName']: i for i, a in srv.albums.items()} == {
        FOLDER: old_albums[FOLDER], 'Kopie': old_albums['Fotos Alt/Kopie'], 'sub': old_albums['Fotos Alt/sub']}
    assert "Old tags 'Nextcloud/Fotos Alt/...' are removed after a complete run" in out
    assert srv.deleted_tags == []
    assert 'Nextcloud/Fotos Alt/Kopie' in srv.tags
    assert json.loads(layout.read_text()) == {'albums': 'strip', 'tags': 'full'}

    # A complete run replaces the old tags
    out = run(mig).stdout
    assert 'TAG       Nextcloud/Fotos Alt removed' in out
    check_complete(srv)
    assert set(srv.tags) == {'Nextcloud', 'Nextcloud/Kopie', 'Nextcloud/sub'}
    assert len(srv.album_renames) == 2
    assert json.loads(layout.read_text()) == {'albums': 'strip', 'tags': 'strip'}

    # Nothing more to do
    renames, deleted, writes = len(srv.album_renames), len(srv.deleted_tags), len(srv.description_writes)
    out = run(mig).stdout
    check_complete(srv)
    assert (len(srv.album_renames), len(srv.deleted_tags), len(srv.description_writes)) == (renames, deleted, writes)
    assert 'renamed' not in out and 'removed' not in out


def test_switch_rename_collision(srv, mig, tmp_path):
    old_version_run(srv, mig, tmp_path)
    srv.add_album('sub')
    out = run(mig).stdout
    assert "ALBUM     Fotos Alt/sub: not renamed, an album 'sub' already exists" in out
    assert 'ALBUM     Fotos Alt/Kopie renamed to Kopie' in out
    assert srv.album_names() == [FOLDER, 'Fotos Alt/sub', 'Kopie', 'sub']
    assert album(srv, 'sub') == album(srv, 'Fotos Alt/sub') == ids(srv, B, C)


def test_switch_aborts_if_rename_fails(srv, mig, tmp_path):
    old_version_run(srv, mig, tmp_path)
    srv.album_rename_status = 403
    n_requests = len(srv.requests)
    result = run(mig, expect=1)
    assert 'ERROR renaming albums' in result.stderr and 'album.update' in result.stderr
    assert not [p for m, p in srv.requests[n_requests:] if p.startswith(srv.nextcloud_url[len(srv.base_url):])]
    assert not (tmp_path / 'cache' / 'nextcloud-layout.json').exists()
    # Fixed permissions: the next run does the switch
    srv.album_rename_status = None
    run(mig)
    check_complete(srv)


# --- lock --------------------------------------------------------------------------

def test_lock_blocks_second_run(srv, mig, tmp_path):
    gate = srv.gate('download', 2)
    first = mig.start('migrate_nextcloud.py')
    try:
        gate.wait()
        second = run(mig, expect=2)
        assert 'Another migration is already running' in second.stderr
    finally:
        gate.release()
        assert first.wait(30) == 0, mig.output()
    check_complete(srv)
    # The lock is gone with the process
    run(mig)


def test_lock_held_by_other_process(srv, mig, tmp_path):
    (tmp_path / 'cache').mkdir()
    with open(tmp_path / 'cache' / 'nextcloud-migration.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run(mig, expect=2)
    assert srv.uploads == []
    run(mig)


# --- interrupts ---------------------------------------------------------------------

@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM])
def test_interrupt_finishes_current_folder(srv, mig, tmp_path, sig):
    gate = srv.gate('download', 5)  # c.mp4, the second file in 'sub'
    proc = mig.start('migrate_nextcloud.py')
    try:
        assert gate.wait().endswith('/sub/c.mp4')
        proc.send_signal(sig)
        assert proc.wait(30) == 130, mig.output()
    finally:
        gate.release(drop=True)
        proc.kill()
    out = mig.output()
    assert 'Interrupted. Progress is saved' in out
    assert 'press Ctrl+C again to skip this' in out
    assert 'Traceback' not in out
    # b.heic, finished before the interrupt, is in its album and tag
    assert album(srv, 'sub') == ids(srv, B)
    assert srv.tagged('Nextcloud/sub') == ids(srv, B)
    state = json.loads((tmp_path / 'cache' / 'nextcloud-migration.json').read_text())
    assert sorted(state) == ['11', '12', '15', '16']

    # Resume
    out = run(mig).stdout
    check_complete(srv)
    assert len(srv.uploads) == 2
    assert out.count('UNCHANGED') == 4 and 'CREATED   /Fotos Alt/sub/c.mp4' in out


@pytest.mark.parametrize('sig', [signal.SIGINT, signal.SIGTERM])
def test_second_signal_skips_album(srv, mig, sig):
    gate = srv.gate('download', 5)
    proc = mig.start('migrate_nextcloud.py')
    try:
        gate.wait()
        # Armed while the client is blocked, so the next album PUT is the
        # one for 'sub' after the interrupt
        album_gate = srv.gate('album_put')
        proc.send_signal(sig)
        request = album_gate.wait()
        sub_id = [i for i, a in srv.albums.items() if a['albumName'] == 'sub'][0]
        assert request == f'/albums/{sub_id}/assets'
        proc.send_signal(sig)
        assert proc.wait(30) == 130, mig.output()
    finally:
        gate.release(drop=True)
        for g in srv.gates:
            g.release(drop=True)
        proc.kill()
    out = mig.output()
    assert 'Skipped; the album and tag are completed on the next run.' in out
    assert 'Traceback' not in out
    assert album(srv, 'sub') == []
    assert 'Nextcloud/sub' not in srv.tags

    run(mig)
    check_complete(srv)
    assert srv.album_names().count('sub') == 1


def test_interrupt_before_first_folder_done(srv, mig, tmp_path):
    gate = srv.gate('download', 1)
    proc = mig.start('migrate_nextcloud.py')
    try:
        gate.wait()
        proc.send_signal(signal.SIGINT)
        assert proc.wait(30) == 130, mig.output()
    finally:
        gate.release(drop=True)
        proc.kill()
    assert 'press Ctrl+C again' not in mig.output()
    assert srv.album_by_name(FOLDER)['assets'] == []
    run(mig)
    check_complete(srv)



# --- --report-deleted ------------------------------------------------------

def test_report_deleted(srv, mig):
    run(mig)
    c_id = srv.asset_by_checksum(C)
    # c.mp4 deleted, b.heic moved within the folder (same file ID),
    # a-copy.jpg deleted while a.jpg (same photo) is still there
    del srv.files['/Fotos Alt/sub/c.mp4']
    srv.files['/Fotos Alt/moved/b.heic'] = srv.files.pop('/Fotos Alt/sub/b.heic')
    del srv.files['/Fotos Alt/Kopie/a-copy.jpg']
    before = len(srv.requests)
    assets_before = {i: dict(a) for i, a in srv.assets.items()}

    out = run(mig, '--report-deleted').stdout
    assert 'DELETED   /Fotos Alt/sub/c.mp4' in out
    assert f'in Immich: {srv.base_url}/photos/{c_id}' in out
    assert 'KEPT      /Fotos Alt/Kopie/a-copy.jpg' in out
    assert 'same photo still in Nextcloud: /Fotos Alt/a.jpg' in out
    assert 'b.heic' not in out
    assert '5 migrated file(s) checked, 3 still in Nextcloud, 1 deleted, 1 deleted but' in out
    # Read-only: nothing changed, nothing downloaded
    assert not [r for r in srv.requests[before:] if r[0] not in ('GET', 'PROPFIND', 'REPORT')]
    assert not [p for m, p in srv.requests[before:] if m == 'GET' and p.startswith(srv.nc_root)]
    assert srv.assets == assets_before


def test_report_deleted_immich_status(srv, mig):
    run(mig)
    c_id, many_id = srv.asset_by_checksum(C), srv.asset_by_checksum(MANY)
    srv.assets[c_id]['trashed'] = True
    del srv.assets[many_id]
    del srv.files['/Fotos Alt/sub/c.mp4']
    del srv.files['/Fotos Alt/many.png']
    out = run(mig, '--report-deleted').stdout
    assert f'in the Immich trash: {srv.base_url}/photos/{c_id}' in out
    assert f'already deleted in Immich: {srv.base_url}/photos/{many_id}' in out


def test_report_deleted_subfolder_and_outside_copies(srv, mig):
    run(mig)
    # Only 'Fotos Alt/Kopie' is checked; a.jpg (same photo) is outside it
    del srv.files['/Fotos Alt/Kopie/a-copy.jpg']
    out = run(mig, '--report-deleted', 'Fotos Alt/Kopie').stdout
    assert 'DELETED   /Fotos Alt/Kopie/a-copy.jpg' in out
    assert "copy outside 'Fotos Alt/Kopie', not checked: /Fotos Alt/a.jpg" in out
    assert '1 migrated file(s) checked' in out
    assert '4 migrated file(s) outside' in out


def test_report_deleted_without_state(srv, mig):
    result = run(mig, '--report-deleted', expect=1)
    assert 'run a migration first' in result.stderr
