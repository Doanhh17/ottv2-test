"""Provision an invited participant or administrator without public registration."""

import argparse
import os
from getpass import getpass

from sqlalchemy.orm import Session

from accounts import create_user
from db import create_database_engine, initialize_database


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an OTTv2 invited account")
    parser.add_argument("--student-id", required=True, help="Account ID, 3-32 permitted characters")
    parser.add_argument(
        "--admin",
        action="store_true",
        help="Grant administrator privileges; use only for the initial or a trusted admin",
    )
    arguments = parser.parse_args()

    database_url = os.environ.get("OTTV2_DATABASE_URL")
    if not database_url:
        parser.error("Set OTTV2_DATABASE_URL explicitly before provisioning an account")
    password = getpass("New password (at least 12 characters): ")
    confirmation = getpass("Confirm password: ")
    if password != confirmation:
        parser.error("Passwords do not match")
    if len(password) < 12:
        parser.error("Password must contain at least 12 characters")

    database_engine = create_database_engine(database_url)
    try:
        initialize_database(database_engine)
        with Session(database_engine) as session:
            create_user(
                session,
                arguments.student_id,
                password,
                is_admin=arguments.admin,
            )
    finally:
        database_engine.dispose()

    role = "admin" if arguments.admin else "participant"
    print(f"Created {role} account: {arguments.student_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
