# Fluent Image Builder Draft

This is the current draft surface of the fluent builder API.
Python is the authoring interface. YAML is the generated build payload produced by `Image.to_yaml()`.

## Example

```python
from idegym.image import Image
from idegym.image.plugins import BaseSystem, IdegymServer, User


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
    .with_plugin(IdegymServer.from_local())
)
```

## Generated YAML Payload

```yaml
images:
  - name: idegym-server-local-draft
    context_path: /path/to/idegym-repo
    dockerfile_content: |
      FROM debian:bookworm-slim
      ...
```

## Intended Reading

- `Image.from_base("debian:bookworm-slim")` uses a real base image reference.
- `BaseSystem()` handles the distro bootstrap we still need for the current server install flow.
- `User(...)` defines the working user and home directory.
- `IdegymServer.from_local()` copies the local workspace into the image and runs `uv sync --project server`.
- The base reference is arbitrary, but the current built-in plugins still assume a Debian/Ubuntu-like image.
- `BuildContext` keeps the small typed core and also exposes `extras` for custom plugin state.
- `PluginBase` is just convenience. Any object implementing `apply(ctx)` and `render(ctx)` can be used as a plugin.
- `context_path` is explicit in the generated YAML because local `COPY` depends on the Docker build context.
- `named(...)` sets the image entry name in the generated YAML payload.
