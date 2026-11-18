#!/usr/bin/env python3
"""Migrate photos and videos from Nextcloud into Immich, including comments.

For every image/video below the Nextcloud folder:
  1. download it via WebDAV and compute its SHA1,
  2. upload it to Immich, unless Immich already has it (same checksum),
  3. write the Nextcloud comments ('YYYY-MM-DD: text', one per line) into
     the Immich description, in front of any existing text (e.g. an AI
     description). Text already present is not added again.

As soon as a folder is done, its structure is recreated in Immich:
  - one album per folder, named after the full folder path
    (e.g. 'Photos/2019/Rome'),
  - nested tags below 'Nextcloud' (e.g. 'Nextcloud/Photos/2019/Rome').
Both are reused on later runs; assets are never added twice.

Progress is stored in cache/nextcloud-migration.json, so files that did not
change are not downloaded again when the tool is re-run; their comments are
still checked and added if new ones exist. If a migrated asset was deleted
permanently in Immich, it is uploaded again. A lock file prevents two runs
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
    """Make sure the file is in Immich. Returns (asset_id, status) or (None, reason)."""
    fd, tmp_path = tempfile.mkstemp(prefix='nextcloud-')
    try:
        with os.fdopen(fd, 'wb') as tmp:
            checksum = nextcloud.download(item['href'], tmp)
        result = immich.check_checksums([{'id': item['path'], 'checksum': checksum}])['results'][0]
        if result['action'] == 'reject':
            if result.get('reason') == 'duplicate' and result.get('assetId'):
                status = 'existing (in trash)' if result.get('isTrashed') else 'existing'
                return result['assetId'], status
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


_albums = None  # album name -> id, loaded once per run


def organize(folder, asset_ids):
    """Put the assets of one Nextcloud folder (e.g. 'Photos/2019') into an
    album and a nested tag of the same name."""
    global _albums
    if not folder or not asset_ids:
        return
    if _albums is None:
        _albums = {}
        for album in immich.list_albums():
            _albums.setdefault(album['albumName'], album['id'])
    if folder not in _albums:
        _albums[folder] = immich.create_album(folder)['id']
        print(f"ALBUM     {folder} (created)")
    tag = f'{TAG_ROOT}/{folder}'
    immich.add_to_album(_albums[folder], asset_ids)
    immich.tag_assets(immich.upsert_tags([tag])[tag], asset_ids)
    print(f"FOLDER    {folder}: {len(asset_ids)} asset(s) in album and tag")


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
    stats = {'uploaded': 0, 'existing': 0, 'unchanged': 0, 'skipped': 0, 'failed': 0, 'comments': 0}
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
                if known and known['etag'] == item['etag'] and immich.asset_exists(known['asset_id']):
                    asset_id, status = known['asset_id'], 'unchanged'
                else:
                    if known and known['etag'] == item['etag']:
                        print(f"GONE      {item['path']}: asset {known['asset_id']} was deleted in Immich, uploading again")
                    asset_id, status = transfer(item)
                    if asset_id is None:
                        print(f"SKIP {item['path']}: {status}")
                        stats['skipped'] += 1
                        continue
                    state[item['fileid']] = {'etag': item['etag'], 'asset_id': asset_id, 'path': item['path']}
                    save_state(state)
                current_ids.add(asset_id)

                line = f"{status.upper():9} {item['path']}"
                added = migrate_comments(item, asset_id) if item['comments_count'] else 0
                if added:
                    line += f" (+{added} comment(s))"
                    stats['comments'] += 1
                print(line)
                stats['uploaded' if status == 'created' else
                      'unchanged' if status == 'unchanged' else 'existing'] += 1
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

    print('\nSummary: ' + ', '.join(f'{k}: {v}' for k, v in stats.items()))
    if interrupted:
        return 130  # conventional exit code for SIGINT
    return 1 if stats['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
