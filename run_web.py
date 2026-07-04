"""Web entry point: serves the dashboard on the network.

Usage:  python run_web.py [--host 0.0.0.0] [--port 8050]
"""
import argparse

from waitress import serve

from app.factory import create_app


def main():
    parser = argparse.ArgumentParser(description="Passive Monitor web server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8050)
    args = parser.parse_args()

    # autostart=True: the web deployment is always-on, so collectors flagged in
    # config (flood by default) begin collecting as soon as the server boots.
    app = create_app(autostart=True)
    print(f"Serving Passive Monitor on http://{args.host}:{args.port}")
    serve(app.server, host=args.host, port=args.port, threads=8)


if __name__ == "__main__":
    main()
