#!/usr/bin/env -S uv run --script --quiet
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "python-on-whales",
# ]
# ///
from python_on_whales import DockerClient

client = DockerClient()
client.volume.prune()
client.container.prune()
client.image.prune()
client.network.prune()
