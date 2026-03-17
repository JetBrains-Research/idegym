from idegym.api.git import GitRepository, GitRepositorySnapshot, GitServer
from pytest import fixture, mark, param, raises


@fixture
def repository():
    return GitRepository(
        server=GitServer.GITHUB,
        owner="ghost",
        name="null",
    )


@mark.parametrize(
    "reference",
    [
        param("HEAD", id="head"),
        param("da39a3e", id="hash_short"),
        param("da39a3ee5e6b4b0d3255bfef95601890afd80709", id="hash_long"),
        param("main", id="main_branch"),
        param("feature-123", id="branch_with_hyphen"),
        param("feature/login", id="feature_branch"),
        param("fix/bug-123", id="feature_branch_with_hyphen"),
        param("v1.0.0", id="semantic_version_tag"),
        param("refs/tags/v1.0.0", id="refs_tag"),
        param("refs/heads/main", id="refs_heads"),
        param("refs/remotes/origin/main", id="refs_remote_branch"),
    ],
)
def test_valid_references(repository: GitRepository, reference: str):
    GitRepositorySnapshot(repository=repository, reference=reference)


@mark.parametrize(
    "reference",
    [
        param("", id="empty_string"),
        param(" ", id="whitespace"),
        param("main!", id="branch_with_invalid_characters"),
        param("feature//login", id="branch_with_two_slashes"),
        param("refs/invalid/path", id="refs_invalid"),
        param("refs/heads/main!", id="refs_with_invalid_characters"),
        param("main^", id="branch_parent"),
        param("HEAD^", id="head_parent"),
        param("main~1", id="branch_ancestor"),
        param("HEAD~3", id="head_ancestor"),
    ],
)
def test_invalid_references(repository: GitRepository, reference: str):
    with raises(ValueError):
        GitRepositorySnapshot(repository=repository, reference=reference)
