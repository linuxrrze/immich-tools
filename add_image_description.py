#!/usr/bin/env python3
# s. https://immich.app/docs/guides/python-file-upload

import requests
import os
import io
import sys
import base64
import binascii
import ollama
from datetime import datetime
from PIL import Image
from pillow_heif import register_heif_opener
from hashlib import sha1

from immich_client import config, add_description, list_albums, list_images, download_image, is_library_asset

register_heif_opener()

### OLLAMA CONFIG

ollama_host = config['ollama']['host']
default_prompt = config['ollama']['prompt']

### OLLAMA FUNCTIONS

def get_available_models(ollama_host):
    response = requests.get(f'{ollama_host}/api/tags')
    if response.status_code == 200:
        # Parse the JSON response
        data = response.json()
        # Extract the list of model names
        model_names = [model['name'] for model in data['models']]
        # Print the list of model names
        return(model_names)
    else:
        return(None)

def get_streaming_response(encoded_image_list, model, prompt):
    client = ollama.Client(host=ollama_host)
    stream = client.chat(
        model=model,
        messages=[
            {
                "role": "user",
                "content": prompt,
                "images": encoded_image_list,
            }
        ],
        stream=True,
    )

    for chunk in stream:
        yield chunk["message"]["content"]

def generate_response(encoded_image_string, model, prompt):
    response = get_streaming_response(encoded_image_string, model, prompt)
    output_text = ""
    for i, r in enumerate(response):
        output_text += r
        print(r, end='')
    print()
    return(output_text)

### MAIN

models = get_available_models(ollama_host)
print(models)

model = "gemma3:27b"
# qwen3 via ollama currently not working:
# s. https://github.com/langgenius/dify/issues/23010
#model = "qwen3:32b"

os.makedirs("cache", exist_ok=True)

albums = list_albums()
for album in albums:

    album_info = f"Album: {album['id']}, {album['albumName']}"
    print(f"{'='*len(album_info)}\n{album_info}\n{'='*len(album_info)}")

    start_time = datetime.now()
    processed_images = 0

    if len(sys.argv)>1 and sys.argv[1] != album['albumName']:
        continue

    for asset in list_images(album['id']):
        if asset['type'] == 'IMAGE':
            info = f"- {asset['id']}, {album['albumName']}/{asset['originalFileName']}"
            print(f"{'-'*len(info)}\n{info}\n{'-'*len(info)}")
            immich_sha1_csum = binascii.hexlify(base64.b64decode(asset['checksum'])).decode('utf-8')

            desc = ''
            # Existing EXIF description (may be missing or None); passed to the model as extra context
            exif_desc = ((asset.get('exifInfo') or {}).get('description') or '').strip()
            desc_filename = f"cache/{immich_sha1_csum}-{model}.description"

            # Description created and cached
            if os.path.isfile(desc_filename):
                with open(desc_filename, 'r') as f:
                    desc = f.read()
                print(f"INFO: Image {asset['originalFileName']} already processed")
                # The description set via PUT /assets/{id} is returned as exifInfo.description
                if exif_desc != '':
                    print(f"INFO: EXIF description available", end='')
                    # Make sure to remove trailing newlines etc.
                    if desc.strip() == exif_desc:
                        print(' and matching cached data.')
                        continue
                    else:
                        print(' and not matching cached data.')
                else:
                    print("No description yet")
                    print(desc)

            # Description needs to be created 
            else:
                image_bytes = download_image(asset['id'])
                try:
                    image = Image.open(io.BytesIO(image_bytes))
                    if image.mode in ("RGBA", "P"):
                        image = image.convert("RGB")
                    print(f"- Image size {image.width}x{image.height}")
                except Exception as e:
                    print(f"ERROR: Failed to load image {asset['originalFileName']}")
                    print(e)
                    continue

                # Get checksum
                if is_library_asset(asset):
                    download_sha1_csum = sha1(f"path:{asset['originalPath']}".encode("utf-8")).hexdigest()
                else:
                    download_sha1_csum = sha1(image_bytes).hexdigest()
                if immich_sha1_csum != download_sha1_csum:
                    print(f"ERROR: Broken file {asset['originalFileName']}")
                    print(f"Immich: {immich_sha1_csum} != Download: {download_sha1_csum}")
                    continue

                try:
                    with io.BytesIO() as byte_arr:
                        image.save(byte_arr, format="JPEG", quality=100)
                        encoded_image = base64.b64encode(byte_arr.getbuffer()).decode("utf-8")
                except Exception as e:
                    print(f"ERROR: Failed to convert {asset['originalFileName']} to image format")
                    print(e)
                    continue

                prompt = f"""{default_prompt}.
                             The file name of the image is {album['albumName']}/{asset['originalFileName']} and may give further hints about the image.
                """
                if exif_desc != '':
                    prompt += f"Additional information about the image:\n{exif_desc}"
                desc = generate_response([encoded_image], model, prompt)

            try:
                if desc and desc != '':
                    ret = add_description(asset['id'], desc)
                    #print(ret)

                    with open(desc_filename, "wb") as f:
                        f.write(desc.encode('utf-8'))
            except Exception as e:
                print("ERROR: Unable to write description")
                continue

            processed_images += 1
            current_time = datetime.now()
            time_spent = current_time - start_time
            time_per_image = time_spent / processed_images
            print(f"INFO: Average processing time {time_per_image.total_seconds():.02f}s")

