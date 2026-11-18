#!/usr/bin/env python3
# s. https://immich.app/docs/guides/python-file-upload

import os

from immich_client import bulk_upload_check, upload_file

### MAIN

#file_list = ['test1.png', 'test2.png', 'test3.png', 'test4.png']
#ret = bulk_upload_check(file_list)
#print(ret)
#for file in ret['results']:
#    if file['action'] == 'accept':
#        res = upload_file(file['id'])
#        print(file, res)
#    else:
#        print(f"INFO: {file['id']}: {file['action']} {file['reason']}")

#print(bulk_upload(['test1.png', 'test2.png']))

accepted = 0
rejected = 0


def check_batch(file_list):
    """Run the bulk upload check for one batch and update the counters."""
    global accepted, rejected
    if not file_list:
        return
    ret = bulk_upload_check(file_list)
    for result in ret['results']:
        if result['action'] == 'accept':
            accepted += 1
        elif result['action'] == 'reject':
            rejected += 1
    print(f"Accepted: {accepted} / Rejected: {rejected}")


file_list = []
for root, dirs, files in os.walk("/home/marcel/Nextcloud/"):
    print(f"{root}")
    for file in files:
        if file.lower().endswith((".jpg", ".jpeg", ".png", ".bmp", ".gif", ".heic", ".webp")):
            file_list.append(os.path.join(root, file))
            if len(file_list) > 100:
                check_batch(file_list)
                file_list = []

# Check the remaining files of the last (partial) batch
check_batch(file_list)
