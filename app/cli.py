"""python -m app.cli run-once [--subreddit NAME]  -- run one monitoring cycle without waiting an hour."""
import argparse
import json

from app.main import create_app


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["run-once"])
    ap.add_argument("--subreddit")
    args = ap.parse_args()
    app = create_app(start_scheduler=False)
    print(json.dumps(app.state.monitor.run_once(only=args.subreddit).to_dict(), indent=2))


if __name__ == "__main__":
    main()
