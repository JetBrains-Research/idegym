from tempfile import gettempdir

from jinja2 import Environment as JinjaEnvironment
from jinja2 import FileSystemBytecodeCache, PackageLoader

jinja = JinjaEnvironment(
    loader=PackageLoader("idegym.image", "templates"),
    bytecode_cache=FileSystemBytecodeCache(
        directory=gettempdir(),
        pattern="__idegym_jinja_%s.cache",
    ),
    auto_reload=False,
)

runtime_template = jinja.get_template("runtime.Dockerfile.jinja")
server_template = jinja.get_template("server.Dockerfile.jinja")


def render_dockerfile(commands: str) -> str:
    return runtime_template.render(commands=commands)


def render_server_image_dockerfile(
    *,
    image: str,
    tag: str,
    repository: str = "docker.io/library",
) -> str:
    return server_template.render(repository=repository, image=image, tag=tag)
