#!/usr/bin/env python3
"""Migrate photos and videos from Nextcloud into Immich, including comments.

For every image/video below the Nextcloud folder:
  1. download it via WebDAV and compute its SHA1,
  2. upload it to Immich, unless Immich already has it (same checksum),
  3. write the Nextcloud comments ('YYYY-MM-DD: text', one per line) into
     the Immich description, in front of any existing text (e.g. an AI
     description). Text already present is not added again.

At the end, the Nextcloud folder structure is recreated in Immich:
  - one album per folder, named after the full folder path
    (e.g. 'Photos/2019/Rome'),
  - nested tags below 'Nextcloud' (e.g. 'Nextcloud/Photos/2019/Rome').
Both are reused on later runs; assets are never added twice.

Progress is stored in cache/nextcloud-migration.json, so files that did not
change are not downloaded again when the tool is re-run; their comments are
still checked and added if new ones exist.

Usage: migrate_nextcloud.py [folder]   (default: [nextcloud] folder in config.ini)
"""

import json
import os
import sys
import tempfile

import immich_client as immich
import nextcloud_client as nextcloud

STATE_FILE = 'cache/nextcloud-migration.json'

# Root of the tag tree that mirrors the Nextcloud folders
TAG_ROOT = 'Nextcloud'


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


def organize(folders):
    """Put assets into one album and one nested tag per Nextcloud folder.

    `folders` maps folder path (e.g. 'Photos/2019') to a set of asset IDs.
    """
    folders = {f: ids for f, ids in folders.items() if f and ids}
    if not folders:
        return
    albums = {}
    for album in immich.list_albums():
        albums.setdefault(album['albumName'], album['id'])
    tags = immich.upsert_tags(f'{TAG_ROOT}/{f}' for f in folders)

    for folder, asset_ids in sorted(folders.items()):
        if folder not in albums:
            albums[folder] = immich.create_album(folder)['id']
            print(f"ALBUM     {folder} (created)")
        immich.add_to_album(albums[folder], asset_ids)
        immich.tag_assets(tags[f'{TAG_ROOT}/{folder}'], asset_ids)
        print(f"FOLDER    {folder}: {len(asset_ids)} asset(s) in album and tag")


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else immich.config['nextcloud'].get('folder', '')
    os.makedirs('cache', exist_ok=True)
    state = load_state()
    stats = {'uploaded': 0, 'existing': 0, 'unchanged': 0, 'skipped': 0, 'failed': 0, 'comments': 0}
    folders = {}

    for item in nextcloud.list_media(folder):
        known = state.get(item['fileid'])
        try:
            if known and known['etag'] == item['etag']:
                asset_id, status = known['asset_id'], 'unchanged'
            else:
                asset_id, status = transfer(item)
                if asset_id is None:
                    print(f"SKIP {item['path']}: {status}")
                    stats['skipped'] += 1
                    continue
                state[item['fileid']] = {'etag': item['etag'], 'asset_id': asset_id, 'path': item['path']}
                save_state(state)
            folders.setdefault(os.path.dirname(item['path']).strip('/'), set()).add(asset_id)

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

    try:
        organize(folders)
    except Exception as e:
        print(f"ERROR creating albums/tags: {e}")
        stats['failed'] += 1

    print('\nSummary: ' + ', '.join(f'{k}: {v}' for k, v in stats.items()))
    return 1 if stats['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
