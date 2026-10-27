## Extra Services ##

DSNP and YT are placed here as they may cause parsing issues depending on the python verion you use.

The authors of each service have seen fit to obfuscate their code in a pre-compiled library. The libraries they provide are for particular python versions. If you happen to be using a python for which there is no library, envied will fail to run.

**DSNP uses python 3.11 and 3.12**

**YT uses 3.11, 3.12, 3.13, 3.14** ---
it may suit all users. Since I am unable to test it is included here.

If you wish to try one or both of these copy the whole folder into packakges/envied/src/envied/services/

The obfuscated python code calls unshackle python libraries. To make them run on envied there are shims added under an unshackle file-path that redirect to envied python souces.