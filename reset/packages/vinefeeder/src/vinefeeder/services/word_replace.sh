#!/bin/bash

# Replace 'automatarr' with 'self.DOWNLOAD_ORCHESTRATOR' in all files located exactly 2 directories deep

find . -mindepth 1 -maxdepth 3 -type f | while read -r file; do
    sed -i 's/self.DOWNLOAD_ORCHESTRATOR/self.DOWNLOAD_ORCHESTRATOR/g' "$file"
done
