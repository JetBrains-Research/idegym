from idegym.api.download import Authorization
from idegym.api.type import AuthType
from pytest import mark, param, raises


@mark.parametrize(
    "auth_type,auth_token",
    [
        param(None, None, id="no-auth"),
        param("Bearer", "", id="valid-auth"),
    ],
)
def test_authorization_valid(auth_type: AuthType, auth_token: str):
    Authorization(type=auth_type, token=auth_token)


@mark.parametrize(
    "auth_type,auth_token",
    [
        param("Bearer", None, id="invalid-auth"),
    ],
)
def test_authorization_invalid(auth_type: AuthType, auth_token: str):
    with raises(ValueError):
        Authorization(type=auth_type, token=auth_token)
