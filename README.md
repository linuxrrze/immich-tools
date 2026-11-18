# immich-tools

Small Python tools for an [Immich](https://immich.app/) photo server:

| Script | What it does |
|---|---|
| `add_image_description.py` | Generates a description for every image in your albums with a vision model behind any OpenAI-compatible API (e.g. a local [Ollama](https://ollama.com/)) and stores it as the Immich asset description. |
| `migrate_nextcloud.py` | Migrates photos and videos from Nextcloud to Immich, including the comments stored in Nextcloud (they become the Immich description) and the folder structure (as albums and tags). |
| `sync_images.py` | Checks which images in a local folder are already in Immich (accepted = new, rejected = duplicate). It does not upload anything yet. |
| `list_albums.py` | Lists all albums with their IDs. |

Works with Immich **v2** and **v3** (checked against the v3.4.1 API).

## Configuration

Copy `config.ini.sample` to `config.ini` and fill in your values:

```ini
[immich]
apiurl = https://immich.example.com/api
apikey = YOUR_IMMICH_API_KEY

[llm]
base_url = http://ollama.example.com:11434/v1
api_key = ollama
model = gemma3:27b
prompt = Describe the image in English. Do not use any introductory words.

[nextcloud]
url = https://nextcloud.example.com
user = YOUR_NEXTCLOUD_USER
password = YOUR_NEXTCLOUD_APP_PASSWORD
folder = Photos
```

Each tool only needs the sections it uses: `[llm]` is for
`add_image_description.py`, `[nextcloud]` for `migrate_nextcloud.py`.

- **Immich API key:** create one in Immich under *Account Settings → API Keys*.
  `add_image_description.py` needs permission to read albums, search and
  download assets, and update assets. `migrate_nextcloud.py` needs to upload,
  read and update assets, and to create and edit albums and tags.
- **Nextcloud (`[nextcloud]`):** use an app password (*Personal settings →
  Security → Create new app password*), not your login password. `folder` is
  relative to your Nextcloud root; leave it empty to migrate everything.
- **LLM server (`[llm]`):** any server with an OpenAI-compatible chat API and
  a vision model works:

  | Server | `base_url` | `api_key` |
  |---|---|---|
  | [Ollama](https://ollama.com/) | `http://<host>:11434/v1` | any value |
  | OpenAI | `https://api.openai.com/v1` | your OpenAI key |
  | vLLM, LM Studio, llama.cpp server | the server's `/v1` URL | as configured |

- **Model:** the model name as the server knows it. For Ollama, pull it first
  with `ollama pull gemma3:27b`. The available models are printed at start.
- **Prompt:** the sample prompt asks for an English description. Change it to
  get descriptions in another language or style.

Ollama's OpenAI API can't set the context size per request. If you need a
larger one, set `OLLAMA_CONTEXT_LENGTH` on the Ollama server.

`config.ini` holds your API keys and is excluded from git.

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

# Migrate the Nextcloud folder from config.ini (or pass another folder)
docker run --rm -v "$PWD/immich-tools:/data" ghcr.io/linuxrrze/immich-tools migrate_nextcloud.py

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
./migrate_nextcloud.py                  # folder from config.ini
./migrate_nextcloud.py "Photos/2019"    # another Nextcloud folder
./list_albums.py
./sync_images.py ~/Pictures
```

Run the scripts from the folder that contains `config.ini`.

## How the Nextcloud migration works

Nextcloud stores comments in its database, not in the image files, so a plain
file copy loses them. For every image and video below the configured folder,
`migrate_nextcloud.py`:

1. downloads the file via WebDAV and computes its SHA1 checksum,
2. uploads it to Immich, unless Immich already has a file with that checksum
   (then the existing asset is used),
3. reads the file's Nextcloud comments and writes them into the Immich
   description, one line per comment, oldest first:

   ```
   2016-01-16: First comment
   2016-01-18: Grandma in Rome
   ```

   The comments are placed in front of any text the description already has
   (e.g. an AI description). Emoji reactions are not migrated.

Immich has no folders for uploaded files, so the Nextcloud folder structure is
recreated in two ways:

- **Albums:** one album per folder, named after the full folder path, e.g.
  `Photos/2019/Rome`. Immich albums can't be nested, so the full path keeps
  folders with the same name apart. An existing album with exactly that name
  is reused.
- **Tags:** nested tags below `Nextcloud`, e.g. `Nextcloud` → `Photos` →
  `2019` → `Rome`. They show the complete tree and can be used as filters.

A file that exists in several Nextcloud folders (same content) becomes a
single Immich asset. It is added to the album and tag of every folder, and
the comments of all copies are added to its description.

### Running it again

The tool can be run any number of times, e.g. to continue an interrupted
run or to pick up new files and comments. Progress is stored in
`cache/nextcloud-migration.json` after every file:

- Unchanged files are not downloaded again, but their comments are checked;
  text that is already in the description is not added twice.
- A file that was interrupted mid-transfer is downloaded again, but not
  uploaded twice (Immich recognizes the checksum).
- Albums and tags are completed on the next run if a run was aborted.
- If a migrated asset was deleted permanently in Immich (trash emptied), it
  is uploaded again. Assets that are only in the trash are left there.
- Only one run at a time is possible; a second one exits with an error
  while `cache/nextcloud-migration.lock` is held.

The migration only adds things to Immich. A file or comment deleted in
Nextcloud stays in Immich. An edited comment is added as new text and the old
one stays. A file whose content changed is uploaded as a new asset next to the
old one. A moved file is added to its new folder's album and tag, but is not
removed from the old ones.

**Size limits:** Nextcloud limits a single comment to 1000 characters, but a
file can have any number of comments. Immich stores descriptions without a
length limit; only its 10 MB limit for API requests applies, which the tool
checks before writing.

## How the description tool works

For each image in an album, `add_image_description.py`:

1. downloads the original file and checks it against the checksum Immich has
   stored (files from external libraries are checked by path),
2. sends it to the model together with the prompt, the album and file
   name, and any description the image already has (e.g. migrated comments),
3. appends the result to the Immich description and stores it in
   `cache/<sha1>-<model>.description`.

Existing text in the description is never replaced. Images that already have
a cached description are not sent to the model again; the cached text is only
added if it is missing from the description.

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

### Keeping dependencies up to date

Python packages (`requirements.txt`), the base image (pinned by digest) and
the GitHub Actions (pinned by commit SHA) all have fixed versions, so builds
are reproducible. [Dependabot](.github/dependabot.yml) checks them weekly and
opens pull requests for updates; the pipeline builds and scans each of them
before anything is merged. Python minor upgrades (e.g. 3.13 → 3.14) are not
proposed automatically.

The weekly pipeline run additionally installs the latest Debian security
updates into the image.
