"""TLS 证书管理。

自动检测或生成 mkcert 签发的本地证书，确保浏览器信任。
"""

import os
import subprocess
import sys


def find_mkcert() -> str | None:
    """在系统中搜索 mkcert 可执行文件。"""
    # 1. 检查 PATH
    for name in ["mkcert", "mkcert.exe"]:
        try:
            result = subprocess.run([name, "-version"], capture_output=True, timeout=5)
            if result.returncode == 0:
                return name
        except Exception:
            pass

    # 2. winget 安装目录
    home = os.path.expanduser("~")
    winget_paths = [
        os.path.join(home, "AppData", "Local", "Microsoft", "WinGet", "Packages"),
    ]
    for base in winget_paths:
        for root, dirs, _files in os.walk(base):
            for d in dirs:
                path = os.path.join(root, d, "mkcert.exe")
                if os.path.exists(path):
                    return path
            break  # 只搜索一层

    return None


def ensure_certs(cert_dir: str) -> tuple[str, str]:
    """确保证书文件存在，不存在则生成。

    Returns:
        (cert_path, key_path)
    """
    cert_path = os.path.join(cert_dir, "localhost.pem")
    key_path = os.path.join(cert_dir, "localhost-key.pem")

    if os.path.exists(cert_path) and os.path.exists(key_path):
        return cert_path, key_path

    mkcert = find_mkcert()
    if not mkcert:
        print("=" * 60)
        print("  需要 mkcert 来生成本地 TLS 证书")
        print()
        print("  安装方法:")
        print("    winget install mkcert")
        print("  或: scoop install mkcert")
        print("  或: choco install mkcert")
        print("=" * 60)
        sys.exit(1)

    os.makedirs(cert_dir, exist_ok=True)

    # 安装本地 CA（仅第一次）
    try:
        subprocess.run([mkcert, "-install"], check=True, capture_output=True, timeout=10)
    except subprocess.CalledProcessError:
        print("[warn] mkcert -install 失败，证书可能不被信任")

    # 生成证书
    subprocess.run(
        [mkcert, "-cert-file", cert_path, "-key-file", key_path,
         "localhost", "127.0.0.1", "::1"],
        check=True, capture_output=True, timeout=10,
    )

    print(f"[ok] TLS 证书已生成: {cert_path}")
    return cert_path, key_path
