from tempfile import gettempdir

from jinja2 import Environment as JinjaEnvironment
from jinja2 import FileSystemBytecodeCache, PackageLoader

jinja = JinjaEnvironment(
    loader=PackageLoader(
        package_name=__package__,
    ),
    bytecode_cache=FileSystemBytecodeCache(
        directory=gettempdir(),
        pattern="__idegym_jinja_%s.cache",
    ),
    auto_reload=False,
)

template = jinja.get_template("Dockerfile.jinja")


def render_dockerfile(commands: str) -> str:
    return template.render(commands=commands)
