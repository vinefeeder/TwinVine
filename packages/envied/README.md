

## What is envied?

Envied is a fork of [Devine](https://github.com/devine-dl/devine/). The name 'envied' is an anagram of Devine, and as such, pays homage to the original author. 
Is is based on v 1.4.3 of envied. It is a powerful archival tool for downloading movies, TV shows, and music from streaming services. Built with a focus on modularity and extensibility, it provides a robust framework for content acquisition with support for DRM-protected content.

No commands have been changed 'uv run envied' still works as usual. 

The major difference is that envied comes complete and needs little configuration.
CDM and services are taken care of.
The prime reason for the existence of envied is a --select-titles function.

If you already use envied you'll probably just want to replace envied/envied/envied.yaml
with your own. But the exisiting yaml is close to working - just needs a few directory locations.
## Select Titles Feature
![--select-titles option](https://github.com/vinefeeder/envied/blob/main/img/envied1.png)

## Key Features

- 🚀 **Easy Installation** - Simple UV installation
- 🎥 **Multi-Media Support** - Movies, TV episodes, and music
- 🛠️ **Built-in Parsers** - DASH/HLS and ISM manifest support
- 🔒 **DRM Support** - Widevine and PlayReady integration
- 🌈 **HDR10+DV Hybrid** - Hybrid Dolby Vision injection via [dovi_tool](https://github.com/quietvoid/dovi_tool)
- 💾 **Flexible Storage** - Local and remote key vaults
- 👥 **Multi-Profile Auth** - Support for cookies and credentials
- 🤖 **Smart Naming** - Automatic P2P-style filename structure
- ⚙️ **Configurable** - YAML-based configuration
- ❤️ **Open Source** - Fully open-source with community contributions welcome

## Quick Start

### Installation

This installs the latest version directly from the GitHub repository:

```shell
git clone https://github.com/vinefeeder/envied.git
cd envied
uv sync
uv run envied --help
```

### Install envied as a global (per-user) tool

```bash
uv tool install git+https://github.com/vinefeeder/envied.git
# Then run:
uvx envied --help   # or just `envied` once PATH updated
```

> [!NOTE]
> After installation, you may need to add the installation path to your PATH environment variable if prompted.

> **Recommended:** Use `uv run envied` instead of direct command execution to ensure proper virtual environment activation.


### Basic Usage

```shell
# Check available commands
uv run envied --help

# Configure your settings
git clone https://github.com/vinefeeder/envied.git
cd envied
uv sync
uv run envied --help

# Download content (requires configured services)
uv run envied dl SERVICE_NAME CONTENT_ID
```

## Documentation

For comprehensive setup guides, configuration options, and advanced usage:

📖 **[Visit their WIKI](https://github.com/envied-dl/envied/wiki)**

The WIKI contains detailed information on:

- Service configuration
- DRM configuration
- Advanced features and troubleshooting

For guidance on creating services, see their [WIKI documentation](https://github.com/envied-dl/envied/wiki).

## End User License Agreement

Envied, and it's community pages, should be treated with the same kindness as other projects.
Please refrain from spam or asking for questions that infringe upon a Service's End User License Agreement.

1. Do not use envied for any purposes of which you do not have the rights to do so.
2. Do not share or request infringing content; this includes widevine Provision Keys, Content Encryption Keys,
   or Service API Calls or Code.
3. The Core codebase is meant to stay Free and Open-Source while the Service code should be kept private.
4. Do not sell any part of this project, neither alone nor as part of a bundle.
   If you paid for this software or received it as part of a bundle following payment, you should demand your money
   back immediately.
5. Be kind to one another and do not single anyone out.

## Licensing

This software is licensed under the terms of [GNU General Public License, Version 3.0](LICENSE).  
You can find a copy of the license in the LICENSE file in the root folder.
