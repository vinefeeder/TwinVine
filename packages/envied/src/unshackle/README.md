**Why is unshackle in Envied?**

This folder contains files that act as a shim for python service files that are pre-compiled by the author to use envied.
Unshackle references would normally break in envied.
In this folder files cotain an import statmenent which re-directs to envied. Each file calls imports from the local envied source.

