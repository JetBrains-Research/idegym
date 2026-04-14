from enum import StrEnum

from idegym.api.download import ArchiveDescriptor
from pydantic import BaseModel, ConfigDict, Field


class GitServer(StrEnum):
    GITHUB = "github.com"
    GITLAB = "gitlab.com"
    HUGGING_FACE_DATASETS = "huggingface.co/datasets"
    HUGGING_FACE_MODELS = "huggingface.co"


class GitRepository(BaseModel):
    server: GitServer
    owner: str = Field(min_length=1)
    name: str = Field(min_length=1)

    model_config = ConfigDict(frozen=True)

    @property
    def qualified_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def url(self) -> str:
        return f"https://{self.server}/{self.qualified_name}.git"

    def head(self) -> "GitRepositorySnapshot":
        return self.at("HEAD")

    def at(self, reference: str) -> "GitRepositorySnapshot":
        return GitRepositorySnapshot(repository=self, reference=reference)

    @classmethod
    def parse(cls, url: str) -> "GitRepository":
        if not url.startswith("https://"):
            raise ValueError("URL must start with 'https://'")
        value = url.removeprefix("https://").removesuffix(".git")
        for server in GitServer:
            if value.startswith(server):
                path = value.removeprefix(server)
                _, owner, name, *other = path.split("/")
                if len(other) != 0:
                    raise ValueError(f"URL must be in the format 'https://{server}/<owner>/<name>.git', got '{url}'")
                return cls(owner=owner, name=name, server=server)
        raise ValueError(f"Invalid URL: '{url}'")


class GitRepositorySnapshot(BaseModel):
    repository: GitRepository
    reference: str = Field(
        description="Branch, tag, commit hash, or HEAD",
        pattern=r"^(HEAD|([\w\-.]+)(/[\w\-.]+)?|refs/(heads|tags|remotes)/[\w\-./]+)$",
        default="HEAD",
    )

    model_config = ConfigDict(frozen=True)

    @property
    def filename(self) -> str:
        return f"{self.repository.name}-{self.reference}.tar.gz"

    def descriptor(self) -> ArchiveDescriptor:
        url = self.repository.url.removesuffix(".git")
        match self.repository.server:
            case GitServer.GITHUB:
                url += f"/archive/{self.reference}.tar.gz"
            case GitServer.GITLAB:
                url += f"/-/archive/{self.reference}/{self.repository.name}-{self.reference}.tar.gz"
            case _:
                # Downloading entire repositories from HuggingFace is not supported!
                raise NotImplementedError(f"Unsupported server: '{self.repository.server}'")
        return ArchiveDescriptor(name=self.filename, url=url)

    def resource(self, path: str) -> "GitRepositoryResource":
        return GitRepositoryResource(snapshot=self, path=path)


class GitRepositoryResource(BaseModel):
    snapshot: GitRepositorySnapshot
    path: str = Field(description="Relative path within the repository")

    model_config = ConfigDict(frozen=True)

    @property
    def filename(self) -> str:
        *_, filename = self.path.split("/")
        return filename

    def descriptor(self) -> ArchiveDescriptor:
        url = self.snapshot.repository.url.removesuffix(".git")
        match self.snapshot.repository.server:
            case GitServer.GITHUB:
                url = url.replace(GitServer.GITHUB, "raw.githubusercontent.com")
                url += f"/{self.snapshot.reference}/{self.path}"
            case GitServer.GITLAB:
                url += f"/-/raw/{self.snapshot.reference}/{self.path}"
            case GitServer.HUGGING_FACE_DATASETS | GitServer.HUGGING_FACE_MODELS:
                url += f"/resolve/{self.snapshot.reference}/{self.path}"
            case _:
                raise NotImplementedError(f"Unsupported server: '{self.snapshot.repository.server}'")
        return ArchiveDescriptor(name=self.filename, url=url)
