#!/usr/bin/env python3
"""启动入口：python3 run.py [--host 0.0.0.0] [--port 8080]"""
import argparse

from app.api.server import main

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--init-demo", action="store_true",
                        help="启动前初始化演示租户/账号/文档")
    args = parser.parse_args()
    if args.host:
        import app.config as cfg
        cfg.HOST = args.host
    if args.port:
        import app.config as cfg
        cfg.PORT = args.port
    if args.init_demo:
        from scripts.seed import seed_demo
        seed_demo()
    main()
