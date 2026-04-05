#!/usr/bin/env python3

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class Handler(BaseHTTPRequestHandler):
    responses: dict = {}
    vsix_platforms: dict = {}

    @classmethod
    def get_vsix_entry(cls, key: str) -> tuple[list[str], bool]:
        raw = cls.vsix_platforms[key]
        if isinstance(raw, list):
            return raw, False
        return raw.get("platforms", []), bool(raw.get("default", False))

    def do_POST(self) -> None:  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            extension_id = payload["filters"][0]["criteria"][0]["value"]
            data = self.responses.get(extension_id)
            if data is None:
                self.send_error(404, f"Unknown extension: {extension_id}")
                return

            response = {
                "results": [
                    {
                        "extensions": [
                            {
                                "publisher": {
                                    "publisherName": data["publisher"],
                                },
                                "extensionName": data["name"],
                                "versions": data["versions"],
                            }
                        ]
                    }
                ]
            }

            encoded = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except Exception as err:  # pragma: no cover - used only in integration tests
            self.send_error(500, str(err))

    def do_GET(self) -> None:  # noqa: N802
        path = self.path
        for key in self.vsix_platforms:
            if f"/{key}/" in path:
                platforms, has_default = self.get_vsix_entry(key)
                for platform in platforms:
                    if f"targetPlatform={platform}" in path:
                        self.send_response(200)
                        self.send_header("Content-Length", "1000")
                        self.end_headers()
                        return
                if "targetPlatform=" not in path and has_default:
                    self.send_response(200)
                    self.send_header("Content-Length", "1000")
                    self.end_headers()
                    return
                self.send_error(404, "Platform not available")
                return
        self.send_error(404, "Unknown VSIX")

    def log_message(self, format: str, *args: object) -> None:
        print(f"MOCK: {format % args}", file=sys.stderr)


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit("usage: mock-marketplace.py RESPONSES_JSON PORT_FILE [VSIX_PLATFORMS_JSON]")

    responses_path = Path(sys.argv[1])
    port_file = Path(sys.argv[2])
    Handler.responses = json.loads(responses_path.read_text(encoding="utf-8"))

    if len(sys.argv) >= 4:
        vsix_platforms_path = Path(sys.argv[3])
        raw = json.loads(vsix_platforms_path.read_text(encoding="utf-8"))
        Handler.vsix_platforms = raw

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port_file.write_text(str(server.server_port), encoding="utf-8")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
