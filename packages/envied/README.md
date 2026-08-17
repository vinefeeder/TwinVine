# Envied in twinvine

**Note**   

*Envied has already been installed by TwinVine. These notes are retained for information only*


## What is envied?

Envied is a fork of [Devine](https://github.com/devine-dl/devine/). The name 'envied' is an anagram of Devine, and as such, pays homage to the original author. 

Envied is based on version 5.3.0 (dev branch) of envied.

It is a powerful archival tool for downloading movies, TV shows, and music from streaming services. Built with a focus on modularity and extensibility, it provides a robust framework for content acquisition with support for DRM-protected content.

No commands have been changed 'uv run envied' still works as usual. 

The major difference is that envied comes complete and needs little configuration.
A basic CDM and services are taken care of.
The prime reason for the existence of envied is a --select-titles function.

If you already use envied you'll probably just want to replace envied/envied/envied.yaml
with your own. But the exisiting yaml is close to working - just needs a few directory locations.

Envied's existence helps keep download tools free. Unshackle is flirting with pay-for access, having a free, closley matched, aternative puts a break on any pay-me aspirations unshackle might have now or in the future.

**Divergence** from Envied's Parent

Envied no longer diverges from its unshackle parent; maintenance became impossible,


 **Recommended:** Use `uv run envied` instead of direct command execution to ensure proper virtual environment activation.


### Basic Usage

```shell


# Download content (requires configured services)
# from inside the TwinVine top level folder:-
uv run envied dl SERVICE_NAME CONTENT_ID
```

## Documentation

For comprehensive setup guides, configuration options, and advanced usage:

📖 **[Visit unshackle online documentation](https://docs.envied.dev/)**

The WIKI contains detailed information on:

- Service configuration
- DRM configuration
- Advanced features and troubleshooting

For guidance on creating services, see their [documentation](https://docs.envied.dev/).


## Licensing

This software is licensed under the terms of [GNU General Public License, Version 3.0](LICENSE).  
You can find a copy of the license in the LICENSE file in the root folder.
