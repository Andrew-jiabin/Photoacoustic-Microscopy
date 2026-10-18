# -*- coding: utf-8 -*-
r"""Live waveform viewer for the DLM3000 in a browser.

Runs a tiny HTTP server next to a background :class:`Streamer` and serves one
page that draws the newest acquisition of every requested channel.  The page
polls ``/data``; the streamer keeps grabbing at the instrument's own pace
(~2 Hz, see the DLM3000 report), so the browser never blocks the instrument
either.

    python -m Tool_code.dlm_scope_web                    # CH1, port 8765
    python -m Tool_code.dlm_scope_web --channels 1,2,3,4
    python -m Tool_code.dlm_scope_web --port 9000 --points 2000

Then open http://<PAM-NEW>:8765/ .  On the tailnet that is
http://100.79.154.4:8765/ .  If the page does not load from another machine,
allow the port through Windows Defender Firewall on PAM-NEW (or use
ssh -L 8765:localhost:8765 HP@100.79.154.4 and open http://localhost:8765).

Notes
-----
* All requested channels come from the **same** acquisition
  (``DlmScope.read_multi`` switches ``:WAVEFORM:TRACE`` after one latch), so
  the traces on screen are time-aligned with each other.
* The update rate is set by the scope's ~320 ms record latch, not by this
  server, the LAN or the browser.  The LAN moves 250 kB in ~11 ms.
"""

from __future__ import absolute_import, division, print_function

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from Nanomax.dlm_scope import DlmScope, Streamer, CODES_PER_DIV
else:
    from Nanomax.dlm_scope import DlmScope, Streamer, CODES_PER_DIV

DEFAULT_HOST = "192.168.1.100"

PAGE = u"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>DLM3000 live</title>
<style>
 html,body{margin:0;background:#101418;color:#d8dee4;
   font:13px/1.4 "Segoe UI",system-ui,sans-serif}
 header{padding:10px 14px;display:flex;gap:18px;align-items:baseline;
   border-bottom:1px solid #2a3138;flex-wrap:wrap}
 header b{font-size:15px}
 #meta{color:#8aa0b0}
 #meta span{margin-right:14px}
 button{background:#1d252c;color:#d8dee4;border:1px solid #3a444d;
   border-radius:4px;padding:3px 12px;cursor:pointer}
 #wrap{padding:12px}
 .lane{position:relative;margin-bottom:8px}
 .lane canvas{width:100%;height:150px;display:block;background:#0a0e12;
   border:1px solid #232b32;border-radius:4px}
 .lane .tag{position:absolute;left:8px;top:4px;font-size:12px}
 .lane .stats{position:absolute;right:8px;top:4px;font-size:12px;color:#8aa0b0}
 #stale{color:#e0a458;display:none}
</style>
</head>
<body>
<header>
 <b>DLM3000 实时波形</b>
 <div id="meta"><span id="m-idn">connecting…</span><span id="m-rate"></span>
 <span id="m-age"></span><span id="m-pts"></span></div>
 <button id="pause">暂停</button>
 <span id="stale">数据过期</span>
</header>
<div id="wrap"></div>
<script>
var COLORS = {1:"#ffd54a", 2:"#4dd7fe", 3:"#ff7bd5", 4:"#7be382"};
var lanes = {}, paused = false, lastStamp = 0;
document.getElementById("pause").onclick = function(){
    paused = !paused;
    this.textContent = paused ? "继续" : "暂停";
};

function build(chs){
    var wrap = document.getElementById("wrap");
    wrap.innerHTML = "";
    lanes = {};
    chs.forEach(function(ch){
        var lane = document.createElement("div");
        lane.className = "lane";
        var c = document.createElement("canvas");
        var tag = document.createElement("div");
        tag.className = "tag";
        tag.style.color = COLORS[ch];
        tag.textContent = "CH" + ch;
        var st = document.createElement("div");
        st.className = "stats";
        st.id = "st-" + ch;
        lane.appendChild(c); lane.appendChild(tag); lane.appendChild(st);
        wrap.appendChild(lane);
        lanes[ch] = c;
    });
}

function draw(ch, wave){
    var c = lanes[ch];
    var dpr = window.devicePixelRatio || 1;
    var W = c.clientWidth, H = c.clientHeight;
    if (c.width !== W*dpr){ c.width = W*dpr; c.height = H*dpr; }
    var g = c.getContext("2d");
    g.setTransform(dpr,0,0,dpr,0,0);
    g.clearRect(0,0,W,H);
    // graticule
    g.strokeStyle = "#1b232a"; g.lineWidth = 1;
    for (var i=1;i<10;i++){ var x=W*i/10; g.beginPath(); g.moveTo(x,0); g.lineTo(x,H); g.stroke(); }
    for (var j=1;j<6;j++){ var y=H*j/6; g.beginPath(); g.moveTo(0,y); g.lineTo(W,y); g.stroke(); }
    g.strokeStyle = "#26323b";
    g.beginPath(); g.moveTo(0,H/2); g.lineTo(W,H/2); g.stroke();
    var codes = wave.codes, n = codes.length;
    if (!n) return;
    var lo = Infinity, hi = -Infinity;
    for (var k=0;k<n;k++){ var v = codes[k]; if (v<lo) lo=v; if (v>hi) hi=v; }
    var pad = Math.max(1, (hi-lo)*0.08); lo -= pad; hi += pad;
    g.strokeStyle = COLORS[ch]; g.lineWidth = 1.25;
    g.beginPath();
    for (var k=0;k<n;k++){
        var x = W*k/(n-1);
        var y = H - (codes[k]-lo)/(hi-lo)*H;
        if (k) g.lineTo(x,y); else g.moveTo(x,y);
    }
    g.stroke();
    var scale = wave.vdiv / 8192;
    document.getElementById("st-"+ch).textContent =
        (hi*scale).toPrecision(3) + " / " + (lo*scale).toPrecision(3) + " V, " +
        "pk-pk " + ((hi-lo)*scale).toPrecision(3) + " V (" + n + " pts)";
}

function tick(){
    fetch("/data").then(function(r){ return r.json(); }).then(function(d){
        document.getElementById("m-idn").textContent = d.idn;
        document.getElementById("m-rate").textContent = d.rate.toFixed(2) + " Hz";
        var age = document.getElementById("m-age");
        age.textContent = "age " + d.age.toFixed(2) + " s";
        age.style.color = d.age > 1.5 ? "#e0a458" : "#8aa0b0";
        document.getElementById("stale").style.display = d.age > 1.5 ? "inline" : "none";
        var chs = Object.keys(d.ch).map(Number).sort();
        if (!lanes || Object.keys(lanes).length !== chs.length) build(chs);
        document.getElementById("m-pts").textContent = d.points + " pts/channel";
        if (paused) return;
        chs.forEach(function(ch){ draw(ch, d.ch[ch]); });
    }).catch(function(){
        document.getElementById("m-idn").textContent = "connection lost";
    });
}
setInterval(tick, 300);
tick();
</script>
</body>
</html>
"""


def _sample(codes, max_points):
    n = len(codes)
    if n <= max_points or n < 2:
        return codes
    out = []
    for i in range(max_points):
        out.append(codes[int(round(i * (n - 1) / (max_points - 1)))])
    return out


class Viewer(object):

    def __init__(self, host, channels, window, max_points, port, bind):
        self.channels = channels
        self.window = window
        self.max_points = max_points
        self.port = port
        self.bind = bind
        self.scope = DlmScope(host, channel=channels[0])
        self.scope.open()
        self.idn = self.scope.idn()
        self.streamer = Streamer(self.scope, channels=tuple(channels),
                                 window=window, autostart=False)
        self.lock = threading.Lock()

    def payload(self):
        with self.lock:
            data, age = self.streamer.snapshot()
            st = self.streamer.stats()
        body = {
            "idn": self.idn,
            "rate": st["rate_hz"],
            "age": age,
            "points": self.max_points,
            "ch": {},
        }
        if data is None:
            return body
        items = data.items() if isinstance(data, dict) else {self.channels[0]: data}.items()
        for ch, wave in items:
            s = wave.summary()
            body["ch"][str(ch)] = {
                "codes": _sample(wave.codes, self.max_points),
                "vdiv": wave.vdiv,
                "offset": wave.offset,
                "srate": wave.srate,
                "pkpk_codes": s.get("pkpk_codes", 0),
                "pkpk_v": s.get("pkpk_v", 0.0),
                "mean_v": s.get("mean_v", 0.0),
            }
        return body

    def serve(self):
        viewer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.startswith("/data"):
                    body = json.dumps(viewer.payload()).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path.startswith("/"):
                    body = PAGE.encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass

        httpd = ThreadingHTTPServer((self.bind, self.port), Handler)
        self.streamer.start()
        print("scope   %s" % self.idn)
        print("channels %s  window %s  %d pts/channel on screen"
              % (self.channels, self.window, self.max_points))
        print("serving http://%s:%d/  (Ctrl-C to stop)" % (self.bind, self.port))
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("stopping")
        finally:
            self.streamer.stop()
            self.scope.close()


def main(argv=None):
    p = argparse.ArgumentParser(description="Browser view of the DLM3000.")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--channels", default="1", help="comma separated, e.g. 1,2,3,4")
    p.add_argument("--window", type=int, nargs=2, metavar=("START", "END"),
                   help="record window to transfer (default 0..9999)")
    p.add_argument("--points", type=int, default=1500,
                   help="points per channel sent to the browser")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--bind", default="0.0.0.0")
    args = p.parse_args(argv)

    channels = [int(c) for c in args.channels.split(",") if c.strip()]
    window = tuple(args.window) if args.window else (0, 9999)
    Viewer(args.host, channels, window, args.points, args.port, args.bind).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
