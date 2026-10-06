"""
JSON-RPC logging proxy with fault injection.

Forwards JSON-RPC POST requests (single and batch) to an upstream node, counts
them per method and records ``eth_getLogs`` request shapes. Optional limits make
it reject requests the way a restrictive RPC provider would, so indexer behaviour
can be reproduced against a local node (e.g. Ganache).

Standard library only, so it runs without Django or the project dependencies:

    python scripts/rpc_logging_proxy.py --upstream http://localhost:8545 \\
        --listen 127.0.0.1:8546 --max-block-range 5 --out /tmp/rpc.json

``POST /__reset`` clears the counters.
"""

import argparse
import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PROVIDER_ERROR_CODE = -32005
REJECT_STYLES = ("jsonrpc", "http413", "http429")


@dataclass
class ProxyConfig:
    upstream: str
    max_addresses: int = 0
    max_topics: int = 0
    max_block_range: int = 0
    max_batch_size: int = 0
    reject_style: str = "jsonrpc"
    upstream_timeout: float = 60.0


def _parse_block(value: Any) -> int | None:
    """
    :return: block number for hex/int values, ``None`` for tags such as ``latest``
    """
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.startswith("0x"):
        try:
            return int(value, 16)
        except ValueError:
            return None
    return None


def get_logs_shape(request: dict) -> dict[str, Any]:
    """
    :return: address count, OR-list length per topic position and block span
        (``None`` if unknown) of an ``eth_getLogs`` request
    """
    params = request.get("params") or [{}]
    log_filter = params[0] if params and isinstance(params[0], dict) else {}

    address = log_filter.get("address")
    if address is None:
        addresses = 0
    elif isinstance(address, list):
        addresses = len(address)
    else:
        addresses = 1

    topics = []
    for topic in log_filter.get("topics") or []:
        if topic is None:
            topics.append(0)  # Wildcard
        elif isinstance(topic, list):
            topics.append(len(topic))
        else:
            topics.append(1)

    block_span: int | None
    if "blockHash" in log_filter:
        block_span = 1
    else:
        from_block = _parse_block(log_filter.get("fromBlock", "latest"))
        to_block = _parse_block(log_filter.get("toBlock", "latest"))
        block_span = (
            to_block - from_block + 1
            if from_block is not None and to_block is not None
            else None
        )

    return {"addresses": addresses, "topics": topics, "block_span": block_span}


def check_limits(request: dict, config: ProxyConfig) -> tuple[str, int, int] | None:
    """
    :return: ``(limit name, got, max)`` for the first exceeded per-request limit,
        ``None`` if the request can be forwarded
    """
    if request.get("method") != "eth_getLogs":
        return None
    shape = get_logs_shape(request)
    if config.max_addresses and shape["addresses"] > config.max_addresses:
        return "max-addresses", shape["addresses"], config.max_addresses
    longest_topic = max(shape["topics"], default=0)
    if config.max_topics and longest_topic > config.max_topics:
        return "max-topics", longest_topic, config.max_topics
    if (
        config.max_block_range
        and shape["block_span"] is not None
        and shape["block_span"] > config.max_block_range
    ):
        return "max-block-range", shape["block_span"], config.max_block_range
    return None


def build_error(request: dict, limit: str, got: int, maximum: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request.get("id"),
        "error": {
            "code": PROVIDER_ERROR_CODE,
            "message": f"{limit} exceeded: got {got}, max {maximum}",
        },
    }


def _size(element: Any) -> int:
    return len(json.dumps(element, separators=(",", ":")))


class ProxyStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.http_requests = 0
            self.methods: dict[str, dict[str, int]] = defaultdict(
                lambda: {"requests": 0, "request_bytes": 0, "response_bytes": 0}
            )
            self.get_logs_addresses: dict[str, int] = defaultdict(int)
            self.get_logs_topics: dict[str, dict[str, int]] = defaultdict(
                lambda: defaultdict(int)
            )
            self.get_logs_block_span: dict[str, int] = defaultdict(int)
            self.rejected: dict[str, dict[str, int]] = defaultdict(
                lambda: defaultdict(int)
            )

    def record_request(self, request: dict) -> None:
        method = str(request.get("method"))
        with self.lock:
            self.methods[method]["requests"] += 1
            self.methods[method]["request_bytes"] += _size(request)
            if method == "eth_getLogs":
                shape = get_logs_shape(request)
                self.get_logs_addresses[str(shape["addresses"])] += 1
                for position, length in enumerate(shape["topics"]):
                    self.get_logs_topics[str(position)][str(length)] += 1
                span = shape["block_span"]
                self.get_logs_block_span["unknown" if span is None else str(span)] += 1

    def record_response(self, request: dict, response: Any) -> None:
        with self.lock:
            self.methods[str(request.get("method"))]["response_bytes"] += _size(
                response
            )

    def record_rejection(self, request: dict, reason: str) -> None:
        with self.lock:
            self.rejected[str(request.get("method"))][reason] += 1

    def summary(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(
                json.dumps(
                    {
                        "http_requests": self.http_requests,
                        "methods": self.methods,
                        "eth_getLogs": {
                            "addresses": self.get_logs_addresses,
                            "topics": self.get_logs_topics,
                            "block_span": self.get_logs_block_span,
                        },
                        "rejected": self.rejected,
                    }
                )
            )

    def write(self, path: str) -> None:
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w") as f:
            json.dump(self.summary(), f, indent=2, sort_keys=True)
        os.replace(tmp_path, path)


class ProxyHandler(BaseHTTPRequestHandler):
    server: "RpcLoggingProxy"

    def log_message(self, format, *args):  # Keep stderr quiet
        pass

    def _send(self, status: int, body: bytes, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/__reset":
            self.server.stats.reset()
            return self._send(200, b'{"reset":true}')

        try:
            payload = json.loads(body)
        except ValueError:
            return self._send(400, b"Invalid JSON", "text/plain")

        is_batch = isinstance(payload, list)
        requests = payload if is_batch else [payload]
        if not all(isinstance(request, dict) for request in requests):
            return self._send(400, b"Invalid JSON-RPC request", "text/plain")

        stats, config = self.server.stats, self.server.config
        with stats.lock:
            stats.http_requests += 1
        for request in requests:
            stats.record_request(request)

        if is_batch and config.max_batch_size and len(requests) > config.max_batch_size:
            batch_violation = ("max-batch-size", len(requests), config.max_batch_size)
            rejections = dict.fromkeys(range(len(requests)), batch_violation)
        else:
            rejections = {
                position: violation
                for position, request in enumerate(requests)
                if (violation := check_limits(request, config))
            }

        for position, (limit, _, _) in rejections.items():
            stats.record_rejection(requests[position], limit)

        if rejections and config.reject_style != "jsonrpc":
            status = 413 if config.reject_style == "http413" else 429
            limit, got, maximum = next(iter(rejections.values()))
            message = f"{limit} exceeded: got {got}, max {maximum}"
            return self._send(status, message.encode(), "text/plain")

        to_forward = [
            request
            for position, request in enumerate(requests)
            if position not in rejections
        ]
        upstream_responses: list = []
        if to_forward:
            try:
                upstream_body = self._forward(to_forward if is_batch else to_forward[0])
            except urllib.error.HTTPError as exc:
                return self._send(exc.code, exc.read(), "text/plain")
            except (OSError, ValueError) as exc:
                return self._send(502, f"Upstream error: {exc}".encode(), "text/plain")
            upstream_responses = (
                upstream_body if isinstance(upstream_body, list) else [upstream_body]
            )

        # Upstream may reorder batch responses, match them by id
        by_id: dict[str, list] = defaultdict(list)
        for response in upstream_responses:
            by_id[json.dumps(response.get("id"))].append(response)

        responses = []
        for position, request in enumerate(requests):
            if position in rejections:
                response = build_error(request, *rejections[position])
            else:
                matching = by_id.get(json.dumps(request.get("id")))
                if not matching:
                    continue  # Notification, no response expected
                response = matching.pop(0)
            stats.record_response(request, response)
            responses.append(response)

        result = responses if is_batch else (responses[0] if responses else None)
        return self._send(200, json.dumps(result).encode())

    def _forward(self, payload: Any) -> Any:
        upstream_request = urllib.request.Request(
            self.server.config.upstream,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(
            upstream_request, timeout=self.server.config.upstream_timeout
        ) as response:
            return json.loads(response.read())


class RpcLoggingProxy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], config: ProxyConfig):
        self.config = config
        self.stats = ProxyStats()
        super().__init__(address, ProxyHandler)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        if isinstance(host, bytes):
            host = host.decode()
        return f"http://{host}:{port}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("--upstream", required=True, help="Upstream JSON-RPC URL")
    parser.add_argument(
        "--listen", default="127.0.0.1:8546", help="host:port (default %(default)s)"
    )
    for name, help_text in (
        ("--max-addresses", "eth_getLogs address-array length"),
        ("--max-topics", "OR-list length of any eth_getLogs topic position"),
        ("--max-block-range", "eth_getLogs toBlock - fromBlock + 1"),
        ("--max-batch-size", "JSON-RPC batch length"),
    ):
        parser.add_argument(
            name, type=int, default=0, help=f"Reject above this {help_text}. 0=off"
        )
    parser.add_argument(
        "--reject-style",
        choices=REJECT_STYLES,
        default="jsonrpc",
        help="jsonrpc: -32005 error per rejected request; http413/http429: "
        "reject the whole HTTP request (default %(default)s)",
    )
    parser.add_argument("--out", help="Write the summary JSON to this path")
    parser.add_argument(
        "--flush-seconds",
        type=float,
        default=10.0,
        help="Summary write interval with --out (default %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    host, _, port = args.listen.rpartition(":")
    config = ProxyConfig(
        upstream=args.upstream,
        max_addresses=args.max_addresses,
        max_topics=args.max_topics,
        max_block_range=args.max_block_range,
        max_batch_size=args.max_batch_size,
        reject_style=args.reject_style,
    )
    proxy = RpcLoggingProxy((host or "127.0.0.1", int(port)), config)

    if args.out:

        def flush():
            while True:
                time.sleep(args.flush_seconds)
                proxy.stats.write(args.out)

        threading.Thread(target=flush, daemon=True).start()

    print(f"Proxying {proxy.url} -> {args.upstream}", flush=True)
    try:
        proxy.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        proxy.server_close()
        if args.out:
            proxy.stats.write(args.out)
            print(f"Summary written to {args.out}", flush=True)


if __name__ == "__main__":
    main()
