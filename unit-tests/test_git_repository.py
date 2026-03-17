from idegym.api.git import GitRepository
from pytest import mark, param, raises


@mark.parametrize(
    "url",
    [
        param("https://github.com/octocat/hello-world.git", id="github"),
        param("https://gitlab.com/gitlab-org/gitlab.git", id="gitlab"),
        param("https://huggingface.co/deepseek-ai/DeepSeek-R1.git", id="hugging_face_model"),
        param("https://huggingface.co/datasets/bigcode/the-stack.git", id="hugging_face_dataset"),
    ],
)
def test_parse_valid_url(url: str):
    repository = GitRepository.parse(url)
    assert repository.url == url


@mark.parametrize(
    "url",
    [
        param("gitlab.com/team/project.git", id="missing_protocol"),
        param("https://github.com/octocat", id="missing_name"),
    ],
)
def test_parse_invalid_url(url: str):
    with raises(ValueError):
        GitRepository.parse(url)
