"""启动本地平台 stub（HTTP REST + Bearer），用于 live 适配层演示。

    python scripts/platform_stub.py --port 8765 --token dev-token

另开终端：

    PLATFORM_MODE=live PLATFORM_BASE_URL=http://127.0.0.1:8765 PLATFORM_TOKEN=dev-token \
    python -m specflow run "/prd-input --doc DOC-2041"

注意：stub 只返回 `specflow/platforms/mock_data/*.json` 的预置数据，
用于验证“换实现不改调用方”，不是真实企业系统。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from specflow.platforms.stub_server import make_server  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="specflow 平台 stub（本地 HTTP）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token", default="dev-token")
    parser.add_argument("--quiet", action="store_true", help="不打印每个请求")
    args = parser.parse_args()

    server = make_server(args.host, args.port, token=args.token, verbose=not args.quiet)
    host, port = server.server_address[:2]
    print(f"[stub] 监听 http://{host}:{port}  token={args.token}")
    print("[stub] 端点前缀 /api/v1；Ctrl+C 退出")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[stub] 收到 Ctrl+C，退出")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())