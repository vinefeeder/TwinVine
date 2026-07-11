# Thanks Akirainblack for providing this routine
# run script as superuser using command:-
# sudo bash Install-media-tools.sh

# Install MKVToolNix and ffmpeg from Ubuntu repos
apt-get update && \
    apt-get install -y mkvtoolnix mkvtoolnix-gui ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Install Bento4 (mp4decrypt)
wget https://www.bok.net/Bento4/binaries/Bento4-SDK-1-6-0-641.x86_64-unknown-linux.zip && \
    unzip -j Bento4-SDK-1-6-0-641.x86_64-unknown-linux.zip \
    'Bento4-SDK-1-6-0-641.x86_64-unknown-linux/bin/*' -d /usr/local/bin/ && \
    rm Bento4-SDK-1-6-0-641.x86_64-unknown-linux.zip && \
    chmod +x /usr/local/bin/*

# Install N_m3u8DL-RE
wget https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v0.3.0-beta/N_m3u8DL-RE_v0.3.0-beta_linux-x64_20241203.tar.gz && \
    tar -xzf N_m3u8DL-RE_v0.3.0-beta_linux-x64_20241203.tar.gz && \
    find . -name "N_m3u8DL-RE" -type f -exec mv {} /usr/local/bin/ \; && \
    chmod +x /usr/local/bin/N_m3u8DL-RE && \
    rm -rf N_m3u8DL-RE_v0.3.0-beta_linux-x64_20241203.tar.gz

# Install Shaka Packager
wget https://github.com/shaka-project/shaka-packager/releases/download/v3.2.0/packager-linux-x64 && \
    mv packager-linux-x64 /usr/local/bin/shaka-packager && \
    chmod +x /usr/local/bin/shaka-packager

# Install dovi_tool
wget https://github.com/quietvoid/dovi_tool/releases/download/2.3.1/dovi_tool-2.3.1-x86_64-unknown-linux-musl.tar.gz && \
    tar -xzf dovi_tool-2.3.1-x86_64-unknown-linux-musl.tar.gz && \
    find . -name "dovi_tool" -type f -exec mv {} /usr/local/bin/ \; && \
    chmod +x /usr/local/bin/dovi_tool && \
    rm -rf dovi_tool-2.3.1-x86_64-unknown-linux-musl.tar.gz
	
# Install HDR10Plus	
wget https://github.com/quietvoid/hdr10plus_tool/releases/download/1.7.1/hdr10plus_tool-1.7.1-x86_64-unknown-linux-musl.tar.gz && \
    tar -xzf hdr10plus_tool-1.7.1-x86_64-unknown-linux-musl.tar.gz && \
    find . -name "hdr10plus_tool" -type f -exec mv {} /usr/local/bin/ \; && \
    chmod +x /usr/local/bin/hdr10plus_tool && \
    rm -rf hdr10plus_tool-1.7.1-x86_64-unknown-linux-musl.tar.gz

# Install SubtitleEdit
wget https://github.com/SubtitleEdit/subtitleedit/releases/download/4.0.14/SE4014.zip && \
    unzip SE4014.zip -d /SE && \
    awk 'BEGIN{print "#!/bin/bash"}' >> /usr/local/bin/SubtitleEdit && \
    echo "exec mono /SE/SubtitleEdit.exe \"\$@\"" >> /usr/local/bin/SubtitleEdit && \
    chmod +x /usr/local/bin/SubtitleEdit && \
    rm -rf  SE4014.zip

# Install uv by copying from official image
# cp --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
python -m pip install uv
