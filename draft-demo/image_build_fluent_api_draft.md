# Fluent Image Builder Draft

This is the current draft surface of the fluent builder API.
Python is the authoring interface. YAML is the serialized image definition produced by `Image.to_yaml()`.

## Example

```python
from idegym.image import Image
from idegym.image.plugins import BaseSystem, IdeGYMServer, User


image = (
    Image.from_base("debian:bookworm-slim")
    .named("idegym-server-local-draft")
    .with_plugin(BaseSystem())
    .with_plugin(
        User(
            username="appuser",
            uid=1000,
            gid=1000,
            home="/home/appuser",
            shell="/bin/bash",
            sudo=True,
        )
    )
    .with_plugin(IdeGYMServer.from_local())
)

local_image = image.build()
```

## Generated YAML Payload

```yaml
images:
  - name: idegym-server-local-draft
    base: debian:bookworm-slim
    plugins:
      - type: base-system
      - type: user
        username: appuser
        uid: 1000
        gid: 1000
        home: /home/appuser
        shell: /bin/bash
        sudo: true
      - type: idegym-server
        source: local
        root: /path/to/idegym-repo
```

## Intended Reading

- `Image.from_base("debian:bookworm-slim")` uses a real base image reference.
- `BaseSystem()` handles the distro bootstrap we still need for the current server install flow.
- `User(...)` defines the working user and home directory.
- `IdegymServer.from_local()` copies the local workspace into the image and runs `uv sync --project server`.
- The base reference is arbitrary, but the current built-in plugins still assume a Debian/Ubuntu-like image.
- `BuildContext` keeps the small typed core and also exposes `extras` for custom plugin state.
- `PluginBase` is just convenience. Any object implementing `apply(ctx)` and `render(ctx)` can be used as a plugin.
- `image.build()` performs a local Docker build.
- `named(...)` sets the image entry name in the serialized definition.
