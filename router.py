# router.py
# Custom High-Speed Dynamic Proxy Router for Edge-Core Cloud Orchestration

import sys
import time
import json
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

# In-Memory Dynamic Routing Table
# Key: URL Prefix Route, Value: Target Pod IP:Port or NodePort
ROUTE_TABLE = {
    "/checkout":     {"service": "checkout-critical-01", "node": "willson", "target": "http://127.0.0.1:30081"},
    "/user-profile": {"service": "user-profile-std-01",  "node": "willson", "target": "http://127.0.0.1:30082"},
    "/analytics":    {"service": "analytics-batch-01",   "node": "willson", "target": "http://127.0.0.1:30083"},
}

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle incoming proxy requests concurrently without blocking."""
    daemon_threads = True

class DynamicRouterHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        # 1. Management API: View live active routing table
        if self.path == "/api/v1/router/routes":
            self._send_json(200, {
                "timestamp": int(time.time()),
                "total_routes": len(ROUTE_TABLE),
                "routes": ROUTE_TABLE
            })
            return

        # 2. Match incoming request path to active route table
        matched_prefix = None
        for route_prefix in sorted(ROUTE_TABLE.keys(), key=len, reverse=True):
            if self.path.startswith(route_prefix):
                matched_prefix = route_prefix
                break

        if not matched_prefix:
            self._send_json(404, {
                "error": "Route Not Found",
                "requested_path": self.path,
                "available_routes": list(ROUTE_TABLE.keys())
            })
            return

        target_info = ROUTE_TABLE[matched_prefix]
        target_url_base = target_info["target"].rstrip('/')

        # 3. Perform Ultra-Fast Reverse Proxy to Target Pod Endpoint
        try:
            req = urllib.request.Request(target_url_base, headers={
                'User-Agent': 'Antigravity-Dynamic-Router/1.0'
            })
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                content = resp.read()
                self.send_response(resp.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("X-Proxy-Routed-By", "Antigravity-Router")
                self.send_header("X-Proxy-Target-Node", target_info["node"])
                self.end_headers()
                self.wfile.write(content)
        except Exception as e:
            self._send_json(502, {
                "error": "Bad Gateway - Target Pod Unreachable",
                "route": matched_prefix,
                "target_node": target_info["node"],
                "target_url": target_url_base,
                "details": str(e)
            })

    def do_POST(self):
        # API Endpoint for Scheduler: Update Routing Table on Pod Migration
        # POST /api/v1/router/update  Body: {"route": "/checkout", "new_node": "archlinux", "new_target": "http://10.243.176.3:30081"}
        if self.path in ["/update", "/api/v1/router/update"]:
            try:
                content_len = int(self.headers.get('Content-Length', 0))
                post_body = self.rfile.read(content_len)
                payload = json.loads(post_body.decode('utf-8'))

                route = payload.get("route")
                new_node = payload.get("new_node")
                new_target = payload.get("new_target")

                if route and new_target:
                    if route not in ROUTE_TABLE:
                        ROUTE_TABLE[route] = {}
                    
                    old_node = ROUTE_TABLE[route].get("node", "unknown")
                    ROUTE_TABLE[route]["node"] = new_node or old_node
                    ROUTE_TABLE[route]["target"] = new_target
                    
                    print(f"🔄 [ROUTER UPDATE] Route '{route}' migrated: {old_node} ➔ {new_node} ({new_target})")
                    self._send_json(200, {
                        "status": "Route Updated Successfully",
                        "route": route,
                        "new_node": new_node,
                        "new_target": new_target
                    })
                    return
            except Exception as e:
                self._send_json(400, {"error": f"Invalid Update Payload: {str(e)}"})
                return

        self._send_json(404, {"error": "Endpoint not found"})

    def _send_json(self, code, data):
        try:
            payload = json.dumps(data, indent=2).encode('utf-8')
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (ConnectionResetError, BrokenPipeError):
            pass

    def log_message(self, format, *args):
        sys.stderr.write("%s - - [%s] %s\n" %
                         (self.address_string(),
                          self.log_date_time_string(),
                          format % args))

def run_router(port=8000):
    server = ThreadedHTTPServer(('0.0.0.0', port), DynamicRouterHandler)
    print(f"🚀 Custom Dynamic Router listening at http://0.0.0.0:{port}/")
    print(f"   - /checkout      ➔ {ROUTE_TABLE['/checkout']['target']}")
    print(f"   - /user-profile  ➔ {ROUTE_TABLE['/user-profile']['target']}")
    print(f"   - /analytics     ➔ {ROUTE_TABLE['/analytics']['target']}")
    server.serve_forever()

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    run_router(port)
