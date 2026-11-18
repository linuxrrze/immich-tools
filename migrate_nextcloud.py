#!/usr/bin/env python3
"""Migrate photos and videos from Nextcloud into Immich, including comments.

For every image/video below the Nextcloud folder:
  1. download it via WebDAV and compute its SHA1,
  2. upload it to Immich, unless Immich already has it (same checksum),
  3. write the Nextcloud comments ('YYYY-MM-DD: text', one per line) into
     the Immich description, in front of any existing text (e.g. an AI
     description). Text already present is not added again.

As soon as a folder is done, its structure is recreated in Immich:
  - one album per folder, named after the folder path below the configured
    folder (with folder = Photos: 'Photos/2019/Rome' -> album '2019/Rome'),
  - nested tags below 'Nextcloud' (-> tag 'Nextcloud/2019/Rome').
strip_folder = no keeps the full path in both ('Photos/2019/Rome'). Albums
and tags from earlier runs with full paths are renamed/replaced once.
Both are reused on later runs; assets are never added twice.

Progress is stored in cache/nextcloud-migration.json, so files that did not
change are not downloaded again when the tool is re-run; their comments are
still checked and added if new ones exist. If a migrated asset was deleted
permanently in Immich, it is uploaded again; if it is in the Immich trash, it
is restored (restore_trashed = no skips it instead). A lock file prevents two runs
at the same time.

Usage: migrate_nextcloud.py [folder]   (default: [nextcloud] folder / NEXTCLOUD_FOLDER)
"""

import json
import os
import signal
import sys
import tempfile

import immich_client as immich
import nextcloud_client as nextcloud

STATE_FILE = 'cache/nextcloud-migration.json'
LOCK_FILE = 'cache/nextcloud-migration.lock'
LAYOUT_FILE = 'cache/nextcloud-layout.json'

# Root of the tag tree that mirrors the Nextcloud folders
TAG_ROOT = 'Nextcloud'


def acquire_lock():
    """Hold an exclusive lock for the whole run, so two runs can't write the
    same state file. The OS releases it when the process ends, even after a
    crash. Returns the open lock file, or None if another run holds it."""
    lock = open(LOCK_FILE, 'a')
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None
    return lock


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_state(state):
    tmp = STATE_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)


def transfer(item):
    """Make sure the file is in Immich. Returns (asset_id, status) with status
    'created', 'existing' or 'trashed' (in the Immich trash), or (None, reason)."""
    fd, tmp_path = tempfile.mkstemp(prefix='nextcloud-')
    try:
        with os.fdopen(fd, 'wb') as tmp:
            checksum = nextcloud.download(item['href'], tmp)
        result = immich.check_checksums([{'id': item['path'], 'checksum': checksum}])['results'][0]
        if result['action'] == 'reject':
            if result.get('reason') == 'duplicate' and result.get('assetId'):
                return result['assetId'], 'trashed' if result.get('isTrashed') else 'existing'
            return None, result.get('reason', 'rejected')
        uploaded = immich.upload_file(tmp_path, filename=os.path.basename(item['path']), mtime=item['mtime'])
        return uploaded['id'], uploaded.get('status', 'created')
    finally:
        os.remove(tmp_path)


def migrate_comments(item, asset_id):
    """Write the file's comments into the Immich description.

    Returns the number of comments if the description changed, else 0.
    """
    comments = nextcloud.get_comments(item['fileid'])
    text = nextcloud.format_comments(comments)
    if not text:
        return 0
    current = (immich.get_asset(asset_id).get('exifInfo') or {}).get('description') or ''
    merged = immich.merge_description(current, text, prepend=True)
    if merged == current.strip():
        return 0
    immich.add_description(asset_id, merged)
    return len(comments)


_albums = None     # album name -> id, loaded once per run
_used_tags = set()  # tags assigned in this run


def _is_true(value):
    return str(value).strip().lower() in ('1', 'yes', 'true', 'on')


def base_folder():
    """The configured [nextcloud] folder, e.g. 'Photos' ('' = everything)."""
    return immich.setting('nextcloud', 'folder', '').strip('/')


def naming_mode():
    """'strip' if album and tag names leave out the configured folder."""
    strip = _is_true(immich.setting('nextcloud', 'strip_folder', 'yes'))
    return 'strip' if strip and base_folder() else 'full'


def relative_folder(folder):
    """Path below the configured folder ('' for the folder itself), or None
    if names keep the full path (strip_folder = no, or a folder outside it).

    The configured folder is used, not a folder given on the command line,
    so names don't depend on which subfolder a run starts in.
    """
    base = base_folder()
    if naming_mode() != 'strip':
        return None
    if folder == base:
        return ''
    if folder.startswith(base + '/'):
        return folder[len(base) + 1:]
    return None


def album_name(folder):
    """With folder = Photos: 'Photos/2019/Rome' -> '2019/Rome'; files directly
    in 'Photos' go to the album 'Photos'."""
    rel = relative_folder(folder)
    if rel is None:
        return folder
    return rel or folder.rsplit('/', 1)[-1]


def tag_name(folder):
    """With folder = Photos: 'Photos/2019/Rome' -> 'Nextcloud/2019/Rome';
    files directly in 'Photos' get the tag 'Nextcloud'."""
    rel = relative_folder(folder)
    if rel is None:
        return f'{TAG_ROOT}/{folder}'
    return f'{TAG_ROOT}/{rel}' if rel else TAG_ROOT


def organize(folder, asset_ids):
    """Put the assets of one Nextcloud folder (e.g. 'Photos/2019') into an
    album (see album_name()) and a nested tag (see tag_name())."""
    global _albums
    if not folder or not asset_ids:
        return
    if _albums is None:
        _albums = {}
        for album in immich.list_albums():
            _albums.setdefault(album['albumName'], album['id'])
    name = album_name(folder)
    if name not in _albums:
        _albums[name] = immich.create_album(name)['id']
        print(f"ALBUM     {name} (created)")
    tag = tag_name(folder)
    immich.add_to_album(_albums[name], asset_ids)
    immich.tag_assets(immich.upsert_tags([tag])[tag], asset_ids)
    _used_tags.add(tag)
    print(f"FOLDER    {folder}: {len(asset_ids)} asset(s) in album '{name}' and tag '{tag}'")


# Switching from full-path names (all versions before strip_folder existed,
# or strip_folder = no) to names without the configured folder.
# cache/nextcloud-layout.json records which naming albums and tags use.

def load_layout(state):
    try:
        with open(LAYOUT_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        # Without the file, earlier runs (if any) used full-path names
        mode = 'full' if state else naming_mode()
        return {'albums': mode, 'tags': mode}


def save_layout(layout):
    tmp = LAYOUT_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(layout, f, indent=1)
    os.replace(tmp, LAYOUT_FILE)


def rename_old_albums():
    """Rename albums 'Photos/<path>' to '<path>'. Their assets stay in them.
    An album is not renamed if one with the new name already exists."""
    base = base_folder()
    albums = immich.list_albums()
    names = {a['albumName'] for a in albums}
    for album in albums:
        old = album['albumName']
        if not old.startswith(base + '/'):
            continue
        new = album_name(old)
        if new in names:
            print(f"ALBUM     {old}: not renamed, an album '{new}' already exists")
            continue
        immich.rename_album(album['id'], new)
        names.discard(old)
        names.add(new)
        print(f"ALBUM     {old} renamed to {new}")


def remove_old_tags():
    """Delete the old tags 'Nextcloud/Photos/...' after a complete run has
    assigned the new ones. Immich can't move a tag to another parent, so
    they can't be renamed. Tags used in this run and their parents are kept
    (a subfolder may itself be called like the configured folder)."""
    keep = set()
    for tag in _used_tags:
        parts = tag.split('/')
        keep.update('/'.join(parts[:i]) for i in range(1, len(parts) + 1))
    prefix = f'{TAG_ROOT}/{base_folder()}'
    old = [t for t in immich.list_tags()
           if (t['value'] == prefix or t['value'].startswith(prefix + '/')) and t['value'] not in keep]
    for tag in sorted(old, key=lambda t: t['value'].count('/'), reverse=True):
        immich.delete_tag(tag['id'])
        print(f"TAG       {tag['value']} removed (replaced by tags without '{base_folder()}/')")


def _terminate(signum, frame):
    raise KeyboardInterrupt


def main():
    # Treat SIGTERM (docker stop, docker compose down) like Ctrl+C. Without a
    # handler, Python running as PID 1 in a container would ignore it.
    signal.signal(signal.SIGTERM, _terminate)
    folder = sys.argv[1] if len(sys.argv) > 1 else immich.setting('nextcloud', 'folder', '')
    os.makedirs('cache', exist_ok=True)
    lock = acquire_lock()
    if lock is None:
        print(f"Another migration is already running (lock: {LOCK_FILE}).", file=sys.stderr)
        return 2
    state = load_state()

    layout = load_layout(state)
    mode = naming_mode()
    if mode == 'strip' and layout['albums'] == 'full':
        print(f"Album names no longer include '{base_folder()}/'; renaming existing albums ...")
        try:
            rename_old_albums()
        except Exception as e:
            print(f"ERROR renaming albums: {e}\n"
                  "The API key needs the album.update permission. "
                  "Set strip_folder = no to keep the old names.", file=sys.stderr)
            return 1
    layout['albums'] = mode
    if mode == 'full':
        layout['tags'] = 'full'
    save_layout(layout)

    stats = {'uploaded': 0, 'existing': 0, 'unchanged': 0, 'restored': 0, 'skipped': 0,
             'failed': 0, 'comments': 0}
    restore_trashed = _is_true(immich.setting('nextcloud', 'restore_trashed', 'yes'))
    # list_media() returns the files folder by folder, so a folder is
    # complete when the first file of another one arrives. Its album and
    # tag are created right then, not only at the end of the run.
    current_folder, current_ids = None, set()

    def finish_folder():
        try:
            organize(current_folder, current_ids)
        except Exception as e:
            print(f"ERROR creating album/tag for {current_folder}: {e}")
            stats['failed'] += 1

    interrupted = False
    try:
        for item in nextcloud.list_media(folder):
            item_folder = os.path.dirname(item['path']).strip('/')
            if item_folder != current_folder:
                finish_folder()
                current_folder, current_ids = item_folder, set()
            known = state.get(item['fileid'])
            try:
                unchanged = known and known['etag'] == item['etag']
                in_immich = immich.asset_status(known['asset_id']) if unchanged else None
                if in_immich:
                    asset_id, status = known['asset_id'], 'unchanged'
                    trashed = in_immich == 'trashed'
                else:
                    if unchanged:
                        print(f"GONE      {item['path']}: asset {known['asset_id']} was deleted in Immich, uploading again")
                    asset_id, status = transfer(item)
                    if asset_id is None:
                        print(f"SKIP {item['path']}: {status}")
                        stats['skipped'] += 1
                        continue
                    trashed = status == 'trashed'
                    state[item['fileid']] = {'etag': item['etag'], 'asset_id': asset_id, 'path': item['path']}
                    save_state(state)

                # A trashed asset is hidden in albums and can't be uploaded
                # again (Immich reports it as duplicate), so restore it.
                if trashed:
                    if not restore_trashed:
                        print(f"SKIP {item['path']}: in the Immich trash (restore_trashed = no)")
                        stats['skipped'] += 1
                        continue
                    try:
                        immich.restore_assets([asset_id])
                    except Exception as e:
                        raise RuntimeError(f"restoring asset {asset_id} from the Immich trash failed "
                                           f"(needs the asset.delete permission): {e}") from e
                    status = 'restored'
                current_ids.add(asset_id)

                line = f"{status.upper():9} {item['path']}"
                added = migrate_comments(item, asset_id) if item['comments_count'] else 0
                if added:
                    line += f" (+{added} comment(s))"
                    stats['comments'] += 1
                print(line)
                stats[{'created': 'uploaded', 'unchanged': 'unchanged',
                       'restored': 'restored'}.get(status, 'existing')] += 1
            except Exception as e:
                print(f"ERROR {item['path']}: {e}")
                stats['failed'] += 1
    except KeyboardInterrupt:
        # Ctrl+C or SIGTERM (docker stop). Every finished file is already
        # in the state file; the file in progress is redone next time.
        interrupted = True
        print('\nInterrupted. Progress is saved; run the tool again to continue.')
        if current_folder and current_ids:
            print('Adding the finished files to their album and tag '
                  '(press Ctrl+C again to skip this) ...')

    try:
        finish_folder()
    except KeyboardInterrupt:
        interrupted = True
        print('\nSkipped; the album and tag are completed on the next run.')

    if mode == 'strip' and layout['tags'] == 'full':
        # Only after a complete run: every folder must have its new tag first
        if interrupted or stats['failed'] or folder.strip('/') != base_folder():
            print(f"\nOld tags '{TAG_ROOT}/{base_folder()}/...' are removed after a complete "
                  f"run of '{base_folder()}' without errors.")
        else:
            try:
                remove_old_tags()
                layout['tags'] = mode
                save_layout(layout)
            except Exception as e:
                print(f"ERROR removing old tags: {e} (needs the tag.read and tag.delete "
                      "permissions; retried on the next complete run)")
                stats['failed'] += 1

    print('\nSummary: ' + ', '.join(f'{k}: {v}' for k, v in stats.items()))
    if interrupted:
        return 130  # conventional exit code for SIGINT
    return 1 if stats['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
