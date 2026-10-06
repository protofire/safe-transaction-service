import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import TestCase

# `scripts/` is not a package
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from rpc_logging_proxy import ProxyConfig, RpcLoggingProxy  # noqa: E402


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    server: "FakeUpstream"

    def log_message(self, format, *args):
        pass

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.received.append(payload)
        if isinstance(payload, list):
            # Reverse the order, the proxy must match responses by id
            result = [self._result(request) for request in reversed(payload)]
        else:
            result = self._result(payload)
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _result(request):
        return {"jsonrpc": "2.0", "id": request["id"], "result": request["method"]}


class FakeUpstream(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.received = []
        super().__init__(("127.0.0.1", 0), FakeUpstreamHandler)


def get_logs(request_id, addresses=None, topics=None, from_block="0x1", to_block="0x1"):
    log_filter = {"fromBlock": from_block, "toBlock": to_block}
    if addresses is not None:
        log_filter["address"] = addresses
    if topics is not None:
        log_filter["topics"] = topics
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "eth_getLogs",
        "params": [log_filter],
    }


def block_number(request_id):
    return {"jsonrpc": "2.0", "id": request_id, "method": "eth_blockNumber"}


class TestRpcLoggingProxy(TestCase):
    def setUp(self):
        self.upstream = FakeUpstream()
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)

    def start_proxy(self, **limits) -> RpcLoggingProxy:
        upstream_url = f"http://127.0.0.1:{self.upstream.server_address[1]}"
        proxy = RpcLoggingProxy(
            ("127.0.0.1", 0), ProxyConfig(upstream=upstream_url, **limits)
        )
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        return proxy

    def post(self, proxy, payload, path=""):
        request = urllib.request.Request(
            proxy.url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    def assert_provider_error(self, response, request_id, message):
        self.assertEqual(
            response,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32005, "message": message},
            },
        )

    def test_single_request_counts(self):
        proxy = self.start_proxy()
        request = block_number(1)
        response = self.post(proxy, request)

        self.assertEqual(
            response, {"jsonrpc": "2.0", "id": 1, "result": "eth_blockNumber"}
        )
        self.assertEqual(self.upstream.received, [request])
        summary = proxy.stats.summary()
        self.assertEqual(summary["http_requests"], 1)
        method_stats = summary["methods"]["eth_blockNumber"]
        self.assertEqual(method_stats["requests"], 1)
        self.assertEqual(
            method_stats["request_bytes"],
            len(json.dumps(request, separators=(",", ":"))),
        )
        self.assertEqual(
            method_stats["response_bytes"],
            len(json.dumps(response, separators=(",", ":"))),
        )

    def test_batch_counts(self):
        proxy = self.start_proxy()
        batch = [block_number(1), get_logs(2), block_number(3)]
        responses = self.post(proxy, batch)

        # Original order is restored although the upstream reversed it
        self.assertEqual([response["id"] for response in responses], [1, 2, 3])
        self.assertEqual(self.upstream.received, [batch])
        summary = proxy.stats.summary()
        self.assertEqual(summary["http_requests"], 1)
        self.assertEqual(summary["methods"]["eth_blockNumber"]["requests"], 2)
        self.assertEqual(summary["methods"]["eth_getLogs"]["requests"], 1)
        self.assertGreater(summary["methods"]["eth_getLogs"]["response_bytes"], 0)

    def test_get_logs_histograms(self):
        proxy = self.start_proxy()
        topic = "0x" + "1" * 64
        self.post(
            proxy,
            [
                get_logs(1, addresses=["0x1", "0x2"], topics=[topic, [topic, topic]]),
                get_logs(2, addresses="0x1", topics=[None, [topic] * 3]),
                get_logs(3, from_block="0x10", to_block="0x14"),
                get_logs(4, from_block="0x10", to_block="latest"),
            ],
        )
        histograms = proxy.stats.summary()["eth_getLogs"]
        self.assertEqual(histograms["addresses"], {"2": 1, "1": 1, "0": 2})
        self.assertEqual(
            histograms["topics"], {"0": {"1": 1, "0": 1}, "1": {"2": 1, "3": 1}}
        )
        self.assertEqual(histograms["block_span"], {"1": 2, "5": 1, "unknown": 1})

    def test_get_logs_limits(self):
        topic = "0x" + "1" * 64
        for limits, request, message in (
            (
                {"max_addresses": 2},
                get_logs(7, addresses=["0x1", "0x2", "0x3"]),
                "max-addresses exceeded: got 3, max 2",
            ),
            (
                {"max_topics": 1},
                get_logs(7, topics=[topic, [topic, topic]]),
                "max-topics exceeded: got 2, max 1",
            ),
            (
                {"max_block_range": 5},
                get_logs(7, from_block="0x0", to_block="0x5"),
                "max-block-range exceeded: got 6, max 5",
            ),
        ):
            with self.subTest(limits=limits):
                self.upstream.received.clear()
                proxy = self.start_proxy(**limits)
                self.assert_provider_error(self.post(proxy, request), 7, message)
                self.assertEqual(self.upstream.received, [])
                limit_name = message.split(" ")[0]
                self.assertEqual(
                    proxy.stats.summary()["rejected"],
                    {"eth_getLogs": {limit_name: 1}},
                )

    def test_get_logs_limits_not_exceeded(self):
        proxy = self.start_proxy(max_addresses=2, max_topics=2, max_block_range=5)
        topic = "0x" + "1" * 64
        request = get_logs(
            1,
            addresses=["0x1", "0x2"],
            topics=[[topic, topic]],
            from_block="0x0",
            to_block="0x4",
        )
        self.assertEqual(self.post(proxy, request)["result"], "eth_getLogs")
        # Unknown span is never rejected
        request = get_logs(2, from_block="0x0", to_block="latest")
        self.assertEqual(self.post(proxy, request)["result"], "eth_getLogs")
        self.assertEqual(proxy.stats.summary()["rejected"], {})

    def test_max_batch_size_rejects_whole_batch(self):
        proxy = self.start_proxy(max_batch_size=2)
        batch = [block_number(1), block_number(2), block_number(3)]
        responses = self.post(proxy, batch)

        self.assertEqual(len(responses), 3)
        for request_id, response in zip([1, 2, 3], responses, strict=True):
            self.assert_provider_error(
                response, request_id, "max-batch-size exceeded: got 3, max 2"
            )
        self.assertEqual(self.upstream.received, [])
        self.assertEqual(
            proxy.stats.summary()["rejected"],
            {"eth_blockNumber": {"max-batch-size": 3}},
        )
        # A batch within the limit is forwarded
        self.assertEqual(len(self.post(proxy, batch[:2])), 2)
        self.assertEqual(self.upstream.received, [batch[:2]])

    def test_mixed_batch_rejects_only_offending_elements(self):
        proxy = self.start_proxy(max_addresses=1)
        batch = [
            block_number(1),
            get_logs(2, addresses=["0x1", "0x2"]),
            get_logs(3, addresses=["0x1"]),
        ]
        responses = self.post(proxy, batch)

        self.assertEqual(self.upstream.received, [[batch[0], batch[2]]])
        self.assertEqual(responses[0]["result"], "eth_blockNumber")
        self.assert_provider_error(
            responses[1], 2, "max-addresses exceeded: got 2, max 1"
        )
        self.assertEqual(
            responses[2], {"jsonrpc": "2.0", "id": 3, "result": "eth_getLogs"}
        )

    def test_http_reject_styles(self):
        for reject_style, status in (("http413", 413), ("http429", 429)):
            with self.subTest(reject_style=reject_style):
                self.upstream.received.clear()
                proxy = self.start_proxy(max_addresses=1, reject_style=reject_style)
                batch = [block_number(1), get_logs(2, addresses=["0x1", "0x2"])]
                with self.assertRaises(urllib.error.HTTPError) as context:
                    self.post(proxy, batch)
                self.assertEqual(context.exception.code, status)
                self.assertEqual(
                    context.exception.read(),
                    b"max-addresses exceeded: got 2, max 1",
                )
                self.assertEqual(self.upstream.received, [])
                self.assertEqual(
                    proxy.stats.summary()["rejected"],
                    {"eth_getLogs": {"max-addresses": 1}},
                )

    def test_reset(self):
        proxy = self.start_proxy(max_addresses=1)
        self.post(proxy, [block_number(1), get_logs(2, addresses=["0x1", "0x2"])])
        self.assertEqual(self.post(proxy, {}, path="/__reset"), {"reset": True})
        self.assertEqual(
            proxy.stats.summary(),
            {
                "http_requests": 0,
                "methods": {},
                "eth_getLogs": {"addresses": {}, "topics": {}, "block_span": {}},
                "rejected": {},
            },
        )
        self.assertEqual(self.upstream.received, [[block_number(1)]])

    def test_summary_json(self):
        proxy = self.start_proxy(max_block_range=1)
        self.post(
            proxy,
            [block_number(1), get_logs(2, addresses=["0x1"], to_block="0x2")],
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "summary.json")
            proxy.stats.write(path)
            with open(path) as f:
                summary = json.load(f)

        self.assertEqual(
            set(summary), {"http_requests", "methods", "eth_getLogs", "rejected"}
        )
        self.assertEqual(summary["http_requests"], 1)
        self.assertEqual(
            set(summary["methods"]["eth_getLogs"]),
            {"requests", "request_bytes", "response_bytes"},
        )
        self.assertEqual(
            summary["eth_getLogs"],
            {"addresses": {"1": 1}, "topics": {}, "block_span": {"2": 1}},
        )
        self.assertEqual(summary["rejected"], {"eth_getLogs": {"max-block-range": 1}})

    def test_help_without_django(self):
        result = subprocess.run(
            [
                sys.executable,
                "-I",  # Ignore PYTHON* env vars and user site-packages
                "-S",  # No site-packages: only the standard library is importable
                os.path.join(SCRIPTS_DIR, "rpc_logging_proxy.py"),
                "--help",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--max-block-range", result.stdout)
        self.assertIn("--reject-style", result.stdout)
