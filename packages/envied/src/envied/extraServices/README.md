## Extra Services ##

Some services are placed here as they may cause parsing issues depending on the python verion you use.

The authors of each service have seen fit to obfuscate their code in a pre-compiled library. The libraries they provide are for particular python versions. If you happen to be using a python for which there is no library, envied will fail to run.

Envied now constrains uv to load python 3.14 without user intervention, so all services in extraServices/python314/ should work.

If you wish to try these services edit evied.yaml to load them as other services load.

The obfuscated python code calls unshackle python libraries. To make them run on envied there are shims added under an unshackle file-path that redirect to envied python souces.