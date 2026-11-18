# immich-tools

Small Python tools for an [Immich](https://immich.app/) photo server:

| Script | What it does |
|---|---|
| `add_image_description.py` | Generates a description for every image in your albums with a local [Ollama](https://ollama.com/) vision model and stores it as the Immich asset description. |
| `sync_images.py` | Checks which images in a local folder are already in Immich (accepted = new, rejected = duplicate). It does not upload anything yet. |
| `list_albums.py` | Lists all albums with their IDs. |

Works with Immich **v2** and **v3** (checked against the v3.4.1 API).

## Configuration

Copy `config.ini.sample` to `config.ini` and fill in your values:

```ini
[immich]
apiurl = https://immich.example.com/api
apikey = YOUR_IMMICH_API_KEY

[ollama]
host = http://ollama.example.com:11434
prompt = Describe the image in English. Do not use any introductory words.
```

- **API key:** create one in Immich under *Account Settings → API Keys*.
  `add_image_description.py` needs permission to read albums, search and
  download assets, and update assets.
- **Prompt:** the sample prompt asks for an English description. Change it to
  get descriptions in another language or style.
- **Model:** `gemma3:27b` (set in `add_image_description.py`). Pull it first
  with `ollama pull gemma3:27b`.

`config.ini` holds your API key and is excluded from git.

## Usage with Docker

The image is published to the GitHub Container Registry for `linux/amd64` and
`linux/arm64`:

```sh
docker pull ghcr.io/linuxrrze/immich-tools:latest
```

The tools read `config.ini` from, and write their cache to, `/data` in the
container. Mount a folder that contains your `config.ini`:

```sh
mkdir -p immich-tools && cp config.ini immich-tools/

# Describe all images in all albums (default command)
docker run --rm -v "$PWD/immich-tools:/data" ghcr.io/linuxrrze/immich-tools

# Only one album
docker run --rm -v "$PWD/immich-tools:/data" ghcr.io/linuxrrze/immich-tools \
  add_image_description.py "My Album"

# List albums
docker run --rm -v "$PWD/immich-tools:/data" ghcr.io/linuxrrze/immich-tools list_albums.py

# Check which local photos are already in Immich
docker run --rm -v "$PWD/immich-tools:/data" -v "$HOME/Pictures:/photos:ro" \
  ghcr.io/linuxrrze/immich-tools sync_images.py /photos
```

The container runs as user ID 1000. The mounted folder must be writable for
that user, because the description cache is stored in `cache/` inside it.

## Usage without Docker

Requires Python 3.10 or newer.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

./add_image_description.py              # all albums
./add_image_description.py "My Album"   # one album
./list_albums.py
./sync_images.py ~/Pictures
```

Run the scripts from the folder that contains `config.ini`.

## How the description tool works

For each image in an album, `add_image_description.py`:

1. downloads the original file and checks it against the checksum Immich has
   stored (files from external libraries are checked by path),
2. sends it to the Ollama model together with the prompt, the album and file
   name, and any description the image already has,
3. writes the result to Immich and to `cache/<sha1>-<model>.description`.

Images that already have a cached description are not sent to the model again.
If the description in Immich differs from the cached one, the cached
description is written back, which overwrites descriptions edited by hand in
Immich.

## Container image and CI

[`.github/workflows/docker.yml`](.github/workflows/docker.yml) builds the image
on every push and pull request to `main`, on `v*` tags and once a week:

1. builds the image,
2. scans it with [Trivy](https://trivy.dev/) for vulnerabilities, secrets and
   misconfigurations. All findings are uploaded to the repository's
   *Security → Code scanning* tab,
3. **fails the pipeline** if there is a HIGH or CRITICAL vulnerability that
   has a fix available,
4. only then builds for amd64 and arm64 and pushes to `ghcr.io` (not for pull
   requests), with SBOM and provenance attestations.

Image tags: `latest` (main branch), `1.2.3` and `1.2` (for a tag `v1.2.3`) and
`sha-<commit>`.

The weekly build picks up security fixes in the base image and dependencies
without any code change.
