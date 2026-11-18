#!/usr/bin/env python3

from immich_client import list_albums

### MAIN

albums = list_albums()
for album in albums:

    album_info = f"Album: {album['id']}, {album['albumName']}"
    print(f"{album_info}")
