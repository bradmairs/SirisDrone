"""Run the studio:  python -m app [--host 0.0.0.0] [--port 8080]"""
import argparse

import uvicorn


def main() -> None:
    ap = argparse.ArgumentParser(description="SirisDrone Studio")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    args = ap.parse_args()
    uvicorn.run("app.main:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
