# Quick Start

This page takes you from a fresh install to your first downloaded file. You will
create the one piece of configuration evied insists on, supply the service
module and CDM it needs to fetch and decrypt media, run a basic `evied dl`
command, and learn where the finished file lands.

!!! note "Before you start"
    This guide assumes evied is already installed and on your `PATH`. If
    `evied --help` does not print a help screen, work through
    [Installation](installation.md) first.

!!! warning "evied includes no services"
    evied handles manifests, DRM, downloading, and muxing. It does not include a
    module for any streaming platform, so you write that part yourself
    ([step 3](#3-write-a-service)). `EXAMPLE` in this guide stands in for the tag you
    give yours.

## 1. Check your environment

evied drives several external tools (FFmpeg, MKVToolNix, shaka-packager, and
others) to decrypt, repack, and mux media. Confirm they are visible before you
download anything:

```shell
evied env check
```

This prints a dependency table. The **required** tools (FFmpeg, FFprobe,
MKVToolNix, mkvpropedit, and shaka-packager) must show a green check. Optional
tools such as `dovi_tool` (Dolby Vision) and CCExtractor (closed captions) only
matter for the features that use them.

!!! tip
    The summary line at the bottom reports `installed/total` and lists anything
    required that is still missing, so you know exactly what to install next.

## 2. Create a minimal config

evied reads a single YAML file named `evied.yaml`. To see where it looks
for that file, and where it *would* accept one if you have not made it yet, run:

```shell
evied env info
```

If no config exists, this prints the candidate locations. In search order they are:

1. `evied.yaml` inside the evied package folder.
2. `evied.yaml` in that folder's parent.
3. `evied.yaml` in your OS user-config directory
   (`~/.config/evied/` on Linux, `%LOCALAPPDATA%\evied\` on Windows,
   `~/Library/Application Support/evied/` on macOS).

The **first** file that exists wins. Create `evied.yaml` in one of those
locations.

### The one key you must set

evied refuses to start a download unless it knows how to name the output file.
That means `output_template` is the one setting a first run genuinely requires.
Everything else has a sensible default. A minimal, working config looks like this:

```yaml title="evied.yaml"
output_template:
  movies: "{title} ({year}) {quality} {source}"
  series: "{title} {season_episode} {episode_name?} {quality} {source}"
```

Each `{variable}` is filled in from the title and the tracks you downloaded. The
full set of valid variables, including `resolution`, `video`, `audio`, `hdr`,
`edition`, `tag`, and more, is documented in [Output and Naming](../guide/output-and-naming.md).

!!! note "Spaces or dots?"
    evied auto-detects your naming style from the template: if the separators
    between variables are mostly spaces, it uses spaces; if they are mostly dots,
    it produces scene-style `Title.S01E01.1080p` names. Write the template in the
    style you want the filenames to look.

!!! warning "Editing config from the CLI strips comments"
    You can read and set keys with `evied cfg` (for example
    `evied cfg tag MYGROUP` or `evied cfg --list`), but writing a value
    rewrites the file and **removes any comments** it contained. If you keep notes
    in your config, edit the file by hand instead.

## 3. Write a service

The service tag you pass to `dl` (like `EXAMPLE`) maps to a **service module**, the
plugin that talks to one streaming platform. evied includes none, so you write
the one you need. [Creating a Service](../dev/creating-a-service.md) covers how.

Put the finished module in your `directories.services` folder (`evied env info`
shows the path). The tag then works with `dl`, `search`, and the other service
commands.

## 4. Add a CDM for DRM

Most streaming content is encrypted. To fetch decryption keys, evied needs a
**CDM**, a Widevine device (`.wvd`) or a PlayReady device (`.prd`). Register a
Widevine device you already have with:

```shell
evied wvd add /path/to/device.wvd
```

This validates the file and moves it into your WVDs directory. Then point services
at it in your config. The `cdm` map is keyed by service tag, with a `default` that
covers everything else:

```yaml title="evied.yaml"
cdm:
  default: my_device        # the .wvd file's name, without the extension
  EXAMPLE: my_other_device       # override for a specific service
```

PlayReady works the same way with `.prd` files created and managed by the
[`prd`](../guide/cli-reference.md#prd) command. If a title is DRM-free, no CDM is needed.

!!! tip
    Run `evied wvd parse my_device` to inspect a device's security level and
    contents, and `evied env info` to confirm where WVDs and PRDs are stored.

## 5. Provide authentication

Services that require a login read either **cookies** or **credentials**.

- **Cookies**: export the service's cookies to a Netscape-format text file and
  place it in your cookies directory. evied looks for, in order:
  `cookies/{SERVICE}.txt`, then `cookies/{SERVICE}/{profile}.txt`, then
  `cookies/{SERVICE}/default.txt`. So a file at `cookies/EXAMPLE.txt` is picked up
  automatically for the `EXAMPLE` service.

- **Credentials**: store a username and password per service in your config:

    ```yaml title="evied.yaml"
    credentials:
      EXAMPLE: "email@example.com:your-password"
    ```

Use the `-p/--profile` flag to switch between multiple accounts for the same
service. Whether a given service needs cookies, credentials, or nothing at all
depends on the service module.

## 6. Find a title (optional)

If you have a URL or ID already, skip this. Otherwise, search the service for a
title and note the `id` it prints, since that is what you feed to `dl`:

```shell
evied search EXAMPLE "My Show"
```

You can also list what a service exposes for a given title without downloading:

```shell
evied dl --list-titles EXAMPLE 81234567     # show seasons/episodes
evied dl --list EXAMPLE 81234567            # show available tracks
```

## 7. Run your first download

A download always has three parts:

```
evied dl  <FLAGS>  <SERVICE-TAG>  <TITLE>
```

- **`dl`** carries every quality, language, track, and output flag.
- **`<SERVICE-TAG>`** picks which service to talk to (e.g. `EXAMPLE`).
- **`<TITLE>`** is the URL, ID, or slug the service understands.

A good first command asks for 1080p with English audio:

```shell title="Your first download"
evied dl -q 1080 -l en EXAMPLE 81234567
```

evied will fetch the title, select the tracks matching your flags, acquire
keys through your CDM (and any key vaults), then decrypt, mux, and tag the result.

### Handy first flags

| Flag | Meaning |
|---|---|
| `-q`, `--quality` | Target resolution(s), e.g. `-q 1080` or `-q 1080,720`. Defaults to best available. |
| `-l`, `--lang` | Language(s) for video and audio, e.g. `-l en` or `-l orig,en`. `orig` = the title's original language. Defaults to `orig`. |
| `-sl`, `--s-lang` | Subtitle language(s); defaults to `all`. |
| `-v`, `--vcodec` | Video codec, e.g. `-v H.265`. Defaults to any. |
| `-r`, `--range` | Dynamic range, e.g. `-r HDR10` or `-r DV`. Defaults to `SDR`. |
| `-w`, `--wanted` | Which episodes, e.g. `-w S01` or `-w S01E01-S01E03`. |
| `-o`, `--output` | Override the output directory for this run. |
| `--list` | List the tracks that would be downloaded, then stop. |

!!! example "A few realistic variations"
    ```shell
    # A whole first season in the best available quality
    evied dl -w S01 EXAMPLE 81234567

    # 2160p HDR10 with the original-language audio plus English subtitles
    evied dl -q 2160 -r HDR10 -l orig -sl en EXAMPLE 81234567

    # Just the newest episode of an ongoing show
    evied dl --latest-episode EXAMPLE 81234567
    ```

See [Downloading](../guide/downloading.md) for the complete flag reference, including
codec, bitrate, channel-layout, and track-type selection.

## 8. Where the output lands

By default, evied writes finished files to the `downloads` directory
(`evied env info` shows its exact path; the built-in default is a `downloads`
folder one level above the installed `evied` package). Override it per run with `-o`:

```shell
evied dl -q 1080 -o /mnt/media/incoming EXAMPLE 81234567
```

- evied writes **movies** as a single `.mkv` file named from your `movies` template.
- **TV episodes** are grouped into a per-show / per-season folder (from your
  `series` template) unless you pass `--no-folder`.
- evied builds the filename from your `output_template` and adds IMDb/TMDB IDs to
  the file's metadata tags when available.

To change the default output location permanently, set it in your config:

```yaml title="evied.yaml"
directories:
  downloads: /mnt/media/incoming
```

## Where to go next

- **[Downloading](../guide/downloading.md)**. The full `dl` command: quality, codecs,
  languages, track selection, hybrid Dolby Vision, and output control.
- **[Configuration](configuration-file.md)**. Every `evied.yaml` key, plus
  directories, key vaults, proxies, and naming templates.
- **[REST API](../dev/rest-api/index.md)**. Run the `serve` server to drive downloads over
  HTTP.

!!! tip "You are never far from help"
    `evied --help`, `evied dl --help`, and `evied <command> --help`
    list every option. When in doubt, add `--list` to a `dl`
    command to preview what it would do before it downloads anything.
