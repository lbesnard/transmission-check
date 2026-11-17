# transmission-check

Finds files and folders sitting in Transmission's data folders that Transmission
no longer knows about (leftovers from removed torrents, failed moves, manual copies).
It compares the folders on disk with the torrent list from Transmission's RPC API and
prints the difference in colour.

**It never deletes anything.** It only reports, and with `--rm-list` prints an `rm -rf`
block for you to review and run yourself.

## Install / run

No dependencies, Python >= 3.11.

```sh
# install straight from git as a `transmission-check` command (recommended)
uv tool install git+https://github.com/<user>/transmission-check
transmission-check --help

# run once without installing
uvx --from git+https://github.com/<user>/transmission-check transmission-check --help

# pin a branch, tag or commit
uv tool install git+https://github.com/<user>/transmission-check@v0.1.0

# from a local clone
git clone https://github.com/<user>/transmission-check && cd transmission-check
uv tool install .            # or: uv run transmission-check --help

# or directly, no install
python3 transmission_orphans.py --help
```

Replace `<user>` with the GitHub account hosting the repo (use an `ssh://git@github.com/...`
URL for a private repo). Update with `uv tool upgrade transmission-check`, remove with
`uv tool uninstall transmission-check`.

A tool install has no working directory to find `./transmission-check.toml` in, so put
your config at `~/.config/transmission-check/config.toml` or pass `--config`.

## Quick start

```sh
cp config.example.toml transmission-check.toml
$EDITOR transmission-check.toml     # set url and path mapping
uv run transmission-check
```

Example output:

```
12 torrents known to Transmission

Scanning /mnt/torrent/downloads
  ✗ file /mnt/torrent/downloads/loose.iso  (4.2 GiB)
  ✗ dir  /mnt/torrent/downloads/movies/old-release  (1.3 GiB)

2 orphan(s), 5.5 GiB reclaimable — review before deleting
```

## Configuration

Settings come from a TOML file; any command-line option overrides it.
The config is looked up in this order (first found wins):

1. `--config FILE`
2. `./transmission-check.toml`
3. `~/.config/transmission-check/config.toml`

See [`config.example.toml`](config.example.toml) for every key.

| Key / option | Meaning |
|---|---|
| `url`, `--url` | RPC URL, default `http://localhost:9091/transmission/rpc` |
| `user` / `password`, `-u` / `-p` | RPC auth. Prefer env vars `TRANSMISSION_USER` / `TRANSMISSION_PASS` |
| `[paths]`, `--map C=H` | container → host path mapping (see below) |
| `docker_container`, `--docker-container` | derive the mapping from `docker inspect` |
| `scan`, `--scan` | container dirs to scan (default: every mapped dir) |
| `ignore`, `--ignore` | entry names never reported (`lost+found`, `.stfolder` always ignored) |
| `--rm-list` | also print a reviewable `rm -rf` block |

## Container paths vs host paths

Transmission runs in a container and reports paths as it sees them
(`/downloads/Some.Show`). The script runs on the host, where that same folder lives
elsewhere (`/mnt/torrent/downloads/Some.Show`). A mapping translates between the two:
it is simply both sides of the compose volume.

```yaml
# docker-compose.yml
volumes:
  - $TORRENT_PATH/downloads:/downloads
  - $TORRENT_PATH/data:/data
```

```toml
# transmission-check.toml
[paths]
"/downloads" = "/media/you/disk/torrent/downloads"   # $TORRENT_PATH/downloads
"/data"      = "/media/you/disk/torrent/data"        # $TORRENT_PATH/data
```

The host folders in the mapping are the roots that get scanned. Provide the mapping
one of three ways (highest priority first):

1. `--map /downloads=/mnt/torrent/downloads` (repeatable)
2. the `[paths]` table in the config
3. `--docker-container transmission`, which reads the container's bind mounts via
   `docker inspect`, so nothing needs to be kept in sync with your compose file

If a torrent's download dir isn't covered by any mapping, the script warns and ignores it.

## Behind gluetun

If Transmission uses `network_mode: service:gluetun`, the RPC port is published by
gluetun (9091 in the usual setup), so the default URL works from the host. From another
machine use `--url http://<host>:9091/transmission/rpc`.

## How orphans are decided

For every torrent, `downloadDir/name` is a known path. Walking the scanned folders:

- a known path is fine (`.part` suffixes are ignored);
- a folder that merely *contains* known paths (e.g. a `movies/` category folder) is
  walked into;
- anything else is reported as an orphan, with its size.

Caveat: if other tools (Radarr, Sonarr, ...) put their own files in the same folders,
those show up as orphans too. Review the list before deleting.
