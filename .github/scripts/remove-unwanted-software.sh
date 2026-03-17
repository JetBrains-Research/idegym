#!/usr/bin/env bash

set -euo pipefail

echo "Before:"
sudo df -h

echo "Removing unwanted software:"

echo "* Deleting on-runner directories..."
sudo rm -rf /opt/ghc \
            /opt/hostedtoolcache/CodeQL \
            /usr/local/.ghcup \
            /usr/local/lib/android \
            /usr/share/dotnet

echo "* Uninstalling \`apt\` packages..."
sudo apt-get -qq remove -y \
                        "^aspnetcore-.*" \
                        "^dotnet-.*" \
                        "^llvm-.*" \
                        "php.*" \
                        "^mongodb-.*" \
                        "^mysql-.*" > /dev/null

if [[ "$(uname -m)" == "x86_64" ]]; then
  echo "* Uninstalling \`apt\` packages that are platform-specific..."
  sudo apt-get -qq remove -y --fix-missing \
                          google-chrome-stable \
                          microsoft-edge-stable \
                          google-cloud-cli \
                          powershell > /dev/null
fi

sudo apt-get -qq remove -y --fix-missing \
                        azure-cli \
                        firefox \
                        mono-devel \
                        libgl1-mesa-dri > /dev/null

echo "* Cleaning up  \`apt\` package repository..."
sudo apt-get -qq autoremove -y > /dev/null
sudo apt-get -qq clean > /dev/null

echo "After:"
sudo df -h
