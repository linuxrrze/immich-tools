#!/usr/bin/env python3
# s. https://immich.app/docs/guides/python-file-upload

import os
import io
import sys
import base64
import binascii
from datetime import datetime
from PIL import Image
from pillow_heif import register_heif_opener
from hashlib import sha1
from openai import OpenAI

from immich_client import config, add_description, list_albums, list_images, download_image, is_library_asset

register_heif_opener()

### LLM CONFIG
# Any OpenAI-compatible API: Ollama (http://host:11434/v1), OpenAI, vLLM,
# LM Studio, llama.cpp server, ...

llm_config = config['llm']
base_url = llm_config['base_url']
# Ollama ignores the API key, but the client requires one
api_key = llm_config.get('api_key', 'none')
model = llm_config['model']
default_prompt = llm_config['prompt']

client = OpenAI(base_url=base_url, api_key=api_key)

### LLM FUNCTIONS

def get_available_models():
    try:
        return [m.id for m in client.models.list()]
    except Exception:
        return None

def get_streaming_response(encoded_image_list, model, prompt):
    content = [{"type": "text", "text": prompt}] + [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}}
        for image in encoded_image_list
    ]
    stream = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        stream=True,
    )
    for chunk in stream:
        # Some chunks carry no text (e.g. role or usage information)
        if chunk.choices and chunk.choices[0].delta.content:
            yield chunk.choices[0].delta.content

def generate_response(encoded_image_string, model, prompt):
    response = get_streaming_response(encoded_image_string, model, prompt)
    output_text = ""
    for i, r in enumerate(response):
        output_text += r
        print(r, end='')
    print()
    return(output_text)

### MAIN

print(f"Model: {model} at {base_url}")
models = get_available_models()
print(f"Available models: {models}")

# Model names of some servers contain '/' (e.g. Qwen/Qwen2.5-VL-7B-Instruct).
# Ollama names like gemma3:27b are kept as they are.
cache_model_name = model.replace('/', '_')

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
            desc_filename = f"cache/{immich_sha1_csum}-{cache_model_name}.description"

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

