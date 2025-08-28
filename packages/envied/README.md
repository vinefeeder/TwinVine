# Envied in twinvine

**Note**   

*Envied has already been installed by TwinVine. These notes are retained for information only*


## What is envied?

Envied is a fork of [Devine](https://github.com/devine-dl/devine/). The name 'envied' is an anagram of Devine, and as such, pays homage to the original author. 
Is is based on v 1.4.3 of envied. It is a powerful archival tool for downloading movies, TV shows, and music from streaming services. Built with a focus on modularity and extensibility, it provides a robust framework for content acquisition with support for DRM-protected content.

No commands have been changed 'uv run envied' still works as usual. 

The major difference is that envied comes complete and needs little configuration.
CDM and services are taken care of.
The prime reason for the existence of envied is a --select-titles function.

If you already use envied you'll probably just want to replace envied/envied/envied.yaml
with your own. But the exisiting yaml is close to working - just needs a few directory locations.


 **Recommended:** Use `uv run envied` instead of direct command execution to ensure proper virtual environment activation.


### Basic Usage

```shell


# Download content (requires configured services)
# from inside the TwinVine top level folder:-
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
