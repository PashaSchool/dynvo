from fastapi import APIRouter

from app.models import User

router = APIRouter(prefix="/users", tags=["users"])

_USERS: dict[int, User] = {}


@router.get("/")
def list_users() -> list[User]:
    return list(_USERS.values())


@router.post("/")
def create_user(user: User) -> User:
    _USERS[user.id] = user
    return user


@router.get("/{user_id}")
def get_user(user_id: int) -> User:
    return _USERS[user_id]
