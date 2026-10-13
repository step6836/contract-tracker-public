"""Add a user or reset a password:  python -m backend.create_user"""
from getpass import getpass

from . import auth


def main() -> None:
    username = input("Username (e.g. stephanie): ").strip()
    display = input("Display name (shown in the activity log): ").strip()
    while True:
        pw = getpass("Password (8+ characters): ")
        if pw != getpass("Repeat password: "):
            print("Those didn't match, try again.")
            continue
        try:
            auth.create_user(username, display, pw)
            break
        except ValueError as e:
            print(e)
    print(f"Saved user '{username.lower()}'.")


if __name__ == "__main__":
    main()
