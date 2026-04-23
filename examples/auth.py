"""Programmatic auth: set up credentials, check status, and get docker flags."""

from pathlib import Path

from caw.auth import setup, get_docker_flags, get_status


def main():
    # Set up credentials to a custom directory (instead of ~/.caw/auth)
    auth_dir = Path("./my_project_auth")
    print("=== Setting up credentials ===")
    setup(agents=["all"], dest_dir=auth_dir)

    # Check status of all collected auth files
    print("\n=== Auth file status ===")
    statuses = get_status(auth_dir=auth_dir)
    for s in statuses:
        print(f"  {s.agent}/{s.file}: type={s.type}, strategy={s.strategy}, exists={s.exists}")
        if s.token_expiry:
            print(f"    token: {s.token_expiry}")

    # Get docker volume flag for mounting auth into a container
    print("\n=== Docker flags ===")
    flags = get_docker_flags(auth_dir=auth_dir)
    print(f"  {flags}")

    # Use it to construct a docker command
    docker_cmd = f"docker run {flags} my-agent-image"
    print(f"\n  Full command: {docker_cmd}")


if __name__ == "__main__":
    main()
