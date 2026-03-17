#!/usr/bin/env -S uv run --script

import argparse
from pathlib import Path

from idegym.utils.dockerfile import render_dockerfile

DEFAULT_COMMANDS = """
    USER root
    RUN set -eux; \
        apt-get update -qq; \
        apt-get install -y --no-install-recommends \
                curl \
                python-is-python3 \
                python3 \
                python3-pip; \
        apt-get clean; \
        rm -rf /var/lib/apt/lists/*;
    USER appuser

    ENV PATH=$PATH:/home/appuser/.local/bin
"""


def main():
    """
    Render the example image Dockerfile from the Dockerfile.jinja template.

    This script renders the Dockerfile.jinja template with predefined commands
    for the example image and writes the result to the specified output file.
    """

    parser = argparse.ArgumentParser(
        description="Render the example image Dockerfile from the Dockerfile.jinja template"
    )
    parser.add_argument("--output", type=Path, required=True, help="Output file for the rendered Dockerfile")

    args = parser.parse_args()

    print(f"Rendering example image Dockerfile with {len(DEFAULT_COMMANDS.splitlines())} lines of commands")
    rendered = render_dockerfile(commands=DEFAULT_COMMANDS)

    output_dir = args.output.parent
    if not output_dir.exists():
        print(f"Creating output directory: {output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)

    args.output.write_text(rendered)
    print(f"Rendered example image Dockerfile written to: {args.output}")


if __name__ == "__main__":
    main()
