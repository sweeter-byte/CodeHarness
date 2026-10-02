import pytest

from users import create_user


def test_create_user_accepts_valid_data_and_trims_name():
    assert create_user(" Ada ", 37) == {"username": "Ada", "age": 37}


@pytest.mark.parametrize("username", ["", "   ", None])
def test_create_user_rejects_empty_username(username):
    with pytest.raises(ValueError):
        create_user(username, 37)


def test_create_user_rejects_negative_age():
    with pytest.raises(ValueError):
        create_user("Ada", -1)


def test_create_user_rejects_non_integer_age():
    with pytest.raises(TypeError):
        create_user("Ada", "37")
