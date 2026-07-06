# TwinVine

![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeederA.png)

TwinVine combines two Python packages:

- [Vinefeeder](https://github.com/vinefeeder/TwinVine/blob/main/packages/vinefeeder/src/vinefeeder/README.md)
- [Envied](https://github.com/vinefeeder/TwinVine/blob/main/packages/envied/README.md)

TwinVine helps you find, select, and download media. Use the graphical front end when you want search and selection assistance, and use the command-line downloader for exact URLs.

Envied is forked from unshackle github.com/unshackle-dl/unshackle and I thank the developers for their effort.

## Key workflows

- Use `envied` when you already have an exact program URL.
- Use `vinefeeder` search when you only know a program name.
- Use the browse feature when you want to explore categories like Film, Drama, or Sport.
- Use Batch Mode to select and download multiple items from several services.

## Usage

TwinVine runs through the Python package manager `uv`.

### Run the main tools

```bash
uv run vinefeeder
uv run envied dl --select-titles <service> <url>
```

### Access envied from the GUI

- On Linux: choose **run envied** after clicking the GUI `envied` button.
- On Windows: close the GUI or press `Ctrl+C` to return to the terminal.

## Installation

### Prerequisites

Install `uv` if needed:

```bash
pip install uv
python3 -m pip install uv
```

Or install `python-uv` through your system package manager.

### Install TwinVine

```bash
git clone https://github.com/vinefeeder/TwinVine.git
cd TwinVine
uv clean
uv lock
uv sync
uv run vinefeeder --help
uv run envied --help
uv run envied dl -?
```

## Windows installation

This section is aimed at novice Windows users.
A printer-friendly checklist is available here:

[WINDOWS_INSTALL_CHECKLIST.txt](WINDOWS_INSTALL_CHECKLIST.txt)

### Windows quick overview

1. Install Git for Windows.
2. Clone the TwinVine repository.
3. Run the Windows installer script.
4. Verify that `uv` is installed.
5. Initialize TwinVine and verify the commands.

## Linux installation

On Linux, use the bundled shell script to install required binaries.

1. Open `Install-media-tools.sh` in a text editor.
2. Change the package manager command on lines 6 and 7 from `apt-get` to your distro's package manager (`dnf`, `pacman`, `zypper`, etc.).
3. Save the file.
4. Run:

```bash
sudo bash ./Install-media-tools.sh
```

5. If `uv` is still not installed, run:

```bash
wget -qO- https://astral.sh/uv/install.sh | sh
```

6. Then initialize TwinVine:

```bash
cd TwinVine
uv lock
uv sync
cp ./packages/envied/src/envied/envied-working-example.yaml ./packages/envied/src/envied/envied.yaml
uv run vinefeeder --help
uv run envied --help
uv run envied dl -?
```

## Locations

By default, downloaded files are saved to:

- `TwinVine/packages/envied/src/downloads/`

Edit `packages/envied/src/envied/envied.yaml` to change the media-download location and other values like email:password for a service.

- Use a full path.
- On Windows, use forward slashes: `C:/Users/Downloads`.
- On Linux, use `/home/user/Downloads`.

Cookies live in `packages/envied/src/`. Each cookie file should be named exactly for the service, for example `DNSP.txt`, and contain the service login cookie.

WVD files are stored in `TwinVine/WVDs/`, such as `device.wvd`.

Vaults are not configured locally by default. TwinVine uses a remote vault for caching and license retrieval and may display `DRMLab` as the license source.

## Linux note

Linux terminals can sometimes freeze after `envied` completes a download. If that happens, ensure `TERMINAL_RESET: True` is set in:

- `TwinVine/packages/vinefeeder/src/vinefeeder/config.yaml`

## Services

### Vinefeeder services

Vinefeeder currently supports search, browse, and list-select for these services:

- ALL4
- BBC
- ITVX
- MY5
- PLEX
- RTE
- STV
- TPTV
- TVNZ
- U

### Envied services

Envied supports a broader set of direct-download services:

- ALL4
- AUBC
- CBS
- CWTV
- DSCP
- iP
- MAX
- MY5
- NF
- PCOK
- PLEX
- RTE
- ROKU
- SPOT
- TPTV
- TVNZ
- YTBE
- ARD
- CBC
- CTV
- DSNP
- ITV
- MTSP
- NBLA
- NRK
- PLUTO
- STV
- TUBI
- UKTV
- ZDF

These services have web origins and not all have been tested.

## You can use AI to create your own Vinefeeder service!!

The prompt below was written by ChatGPT after refactoring a Vinefeeder service to reuse Envied service logic.

```
I am working on the TwinVine project:

https://github.com/vinefeeder/TwinVine

TwinVine contains two Python packages:

* `envied`: a command-line video downloader.
* `vinefeeder`: a graphical / interactive front end that searches, lists, selects, and then calls `envied` by subprocess.

I want to create or rewrite a Vinefeeder service by reusing the matching envied service instead of duplicating API logic.

Target service:

* Envied service path:
  `packages/envied/src/envied/services/<SERVICE>/`

* Vinefeeder service path:
  `packages/vinefeeder/src/vinefeeder/services/<SERVICE>/`

Please inspect the current code in both locations.

Goal:

Rewrite the Vinefeeder `<SERVICE>` service so that it imports and reuses the existing envied `<SERVICE>` service class wherever possible.

The Vinefeeder service should:

1. Preserve the standard Vinefeeder service interface:

   * `receive()`
   * `fetch_videos()`
   * `second_fetch()`
   * `fetch_videos_by_category()`

2. Use the envied service for:

   * authentication / token handling
   * search, if the envied service provides `search()`
   * programme/title expansion via `get_titles()` or `get_titles_cached()`
   * service-specific API calls already implemented in envied

3. Avoid reimplementing API endpoints that already exist in envied.

4. Keep Vinefeeder responsible only for:

   * presenting search results
   * letting the user choose a title
   * expanding a multi-episode series into selectable individual episodes
   * building one envied subprocess command per selected single title

5. Use `beaupy.select_multiple()` when there are multiple episodes available.

6. If the selected item is a movie, sport event, clip, highlight, or single episode, skip the beaupy episode list and call envied directly.

7. Preserve Vinefeeder’s existing download behaviour by calling:

   `self.runsubprocess(command)`

   where command should normally be shaped like:

   `["uv", "run", "envied", "dl", *self.options_list, "<SERVICE>", url]`

8. Preserve support for Vinefeeder service options from config, using `split_options()` as existing services do.

9. If an envied service object needs a Click context, create the smallest safe adapter context needed rather than copying envied command-line internals unnecessarily.

10. If the envied service requires credentials, token cache, proxy state, or service config, reuse envied’s existing config-loading patterns as closely as possible.

11. Be careful not to authenticate twice unnecessarily. If `fetch_videos()` and `second_fetch()` are sequential, prefer reusing an already-authenticated envied service instance where safe.

12. Produce a complete replacement `__init__.py` for the Vinefeeder service.

13. Also explain:

* which envied methods/classes are being reused
* what Vinefeeder-specific logic remains
* any assumptions or caveats
* how I should test the rewritten service

Please do not merely describe the approach. Provide the actual rewritten Vinefeeder service code.

```
## Linux users only: Adding a little automation

In .bashrc in your home directory you can add useful aliases and scripts to speed the start-up of TwinVine.

Add the following lines at the end of your .bashrc, (or your system's equivalent), to start Vinefeeder by typing 'v' into a terminal window;  
and 'e' to start envied with a pre-filled command.  BE SURE TO EDIT your/path/to/Twinvine to reflect your needs.
```
alias v="cd /your/path/to/TwinVine/;uv run vinefeeder"

unalias e 2>/dev/null  

e() {
    cd /your/path/to/TwinVine || return

    local cmd
    read -e -i "uv run envied dl --select-titles " -p "TwinVine> " cmd || return

    [[ -n "$cmd" ]] && history -s "$cmd" && eval "$cmd"
}
```
  
## Other README's
    TwinVine/packages/vinefeeder/src/vinefeeder/README.md  
    for details for confuring Envied download options on a service by service basis.
    TwinVine/packages/envied/README.md  links to wiki (envied - envied's parent)



Images
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeederA1.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder1.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder2.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder4.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder5.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder6.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder7.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder8.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder9.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder10.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeeder11.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/vinefeederB.png)
    ![TwinVine GUI](https://github.com/vinefeeder/TwinVine/blob/main/images/hellyes.png)
    