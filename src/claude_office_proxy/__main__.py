"""claude-office-proxy CLI 入口。

用法:
    claude-office-proxy --config config.yaml
    claude-office-proxy --config config.yaml --port 8766
    claude-office-proxy --install-cert   # 生成本地 TLS 证书
"""

import argparse
import os
import ssl
import sys

import yaml

from .cert import ensure_certs
from .proxy import build_app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Claude for Office 国产模型代理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  claude-office-proxy --config config.yaml
  claude-office-proxy --config config.yaml --port 8766
  claude-office-proxy --install-cert

配置文件格式见 config.yaml.example
        """,
    )
    parser.add_argument("--config", type=str, help="YAML 配置文件路径")
    parser.add_argument("--port", type=int, default=8766, help="HTTPS 监听端口 (默认 8766)")
    parser.add_argument("--log", type=str, help="日志文件路径 (默认 ~/.claude-office-proxy/proxy.log)")
    parser.add_argument("--install-cert", action="store_true", help="仅安装 TLS 证书后退出")
    parser.add_argument("--version", action="store_true", help="显示版本")
    args = parser.parse_args()

    if args.version:
        from . import __version__
        print(f"claude-office-proxy v{__version__}")
        return

    # TLS 证书
    cert_dir = os.path.join(os.path.expanduser("~"), ".claude-office-proxy")
    cert_path, key_path = ensure_certs(cert_dir)

    if args.install_cert:
        print(f"证书已就绪: {cert_path}")
        return

    # 加载配置
    config_path = args.config
    if not config_path:
        # 搜索可能的配置位置
        candidates = [
            "config.yaml",
            "config.yml",
            os.path.join(os.path.expanduser("~"), ".claude-office-proxy", "config.yaml"),
        ]
        for c in candidates:
            if os.path.exists(c):
                config_path = c
                break

    if not config_path or not os.path.exists(config_path):
        print("错误: 未找到配置文件。使用 --config 指定路径。")
        print("示例: claude-office-proxy --config config.yaml")
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    port = args.port or config.get("server", {}).get("port", 8766)
    log_path = args.log or os.path.join(os.path.expanduser("~"), ".claude-office-proxy", "proxy.log")

    # 启动服务器
    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ssl_context.load_cert_chain(cert_path, key_path)

    app = build_app(config, log_path)

    print(f"claude-office-proxy: https://127.0.0.1:{port}")
    print(f"Office 插件 Gateway URL: https://127.0.0.1:{port}")
    print(f"Token: {config.get('server', {}).get('token', 'kaicode')}")
    print(f"日志: {log_path}")
    print(f"按 Ctrl+C 停止")

    from aiohttp import web
    web.run_app(app, host="0.0.0.0", port=port, ssl_context=ssl_context)


if __name__ == "__main__":
    main()
