"""enclave 专用 gunicorn worker：TLS 行为收口点（spec §5）。

iOS 把 sha256(cert.DER) 钉在 REPORT_DATA 里，握手必须精确出示 bootstrap
派生的那张证书，且语义与旧 _enclave_ssl_context 一致：裸 PROTOCOL_TLS_SERVER、
TLS1.2+、无客户端证书校验、无 ALPN 定制。uvicorn 没有公开的"注入现成
SSLContext"入口（Config.load 内部调 create_ssl_context），所以对
uvicorn.config.create_ssl_context 做进程级替换。替换只在
EnclaveUvicornWorker.init_process（fork 后的 worker 子进程，Config.load
之前）安装——import 本模块必须无副作用，否则同进程的其他 uvicorn 使用方
（pytest 全家、未来同进程嵌入部署）会被静默换成这个精简 TLS context。"""

from __future__ import annotations

import ssl

import uvicorn.config
from uvicorn_worker import UvicornWorker


def _enclave_create_ssl_context(*args, **kwargs) -> ssl.SSLContext:
    """裸 PROTOCOL_TLS_SERVER + TLS1.2+，无 ALPN、无客户端证书校验——复刻旧
    _enclave_ssl_context，让 iOS 能按 sha256(cert.DER) 精确匹配。

    位置/关键字两种调用都吃：uvicorn 当前的 Config.load 用关键字调
    create_ssl_context，但其位置签名是 (certfile, keyfile, password,
    ssl_version, cert_reqs, ca_certs, ciphers)。requirements 下限是
    uvicorn>=0.30，将来允许范围内若改成位置调用，keyword-only 的旧签名会在
    TLS enclave 启动时直接 TypeError 崩死（安全攸关且 dev-seed 冒烟测不到 TLS
    路径）。从两种形式里取 certfile/keyfile、其余参数一律忽略，消掉这个单点。"""
    certfile = kwargs["certfile"] if "certfile" in kwargs else args[0]
    keyfile = kwargs["keyfile"] if "keyfile" in kwargs else args[1]
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class EnclaveUvicornWorker(UvicornWorker):
    # limit_concurrency 是 uvicorn 兜底闸（远高于正常并发），防失控堆积。
    CONFIG_KWARGS = {"limit_concurrency": 2048}

    def init_process(self):
        # fork 后的 worker 子进程里、Server.serve → Config.load 之前安装。
        # enclave worker 进程专属，无需恢复。
        uvicorn.config.create_ssl_context = _enclave_create_ssl_context
        super().init_process()
