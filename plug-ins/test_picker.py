#!/usr/bin/env python3
"""Tests for the app picker the Now Playing control script serves: a page
listing the applications playing audio, and a POST that chooses one by
writing the capture helper's tap file. Standard library only."""

import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
import importlib.util

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("meta_nowplaying", os.path.join(HERE, "meta_nowplaying.py"))
mnp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mnp)

APPS = [
    {"pid": 101, "name": "djay Pro", "bundle": "com.algoriddim.djay-iphone-free", "playing": True},
    {"pid": 202, "name": "Google Chrome Helper", "bundle": "com.google.Chrome.helper", "playing": False},
]


class Picker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bbpicker-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.tap_file = os.path.join(self.tmp, "capture-app")
        self.server = mnp.picker_server(("127.0.0.1", 0), list_apps=lambda: APPS, tap_file=self.tap_file)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body else {}
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        data = res.read().decode()
        conn.close()
        return res.status, res.getheader("Content-Type", ""), data

    def test_page_lists_the_apps_and_marks_the_current_choice(self):
        with open(self.tap_file, "w") as fh:
            fh.write("com.algoriddim.djay-iphone-free\n")
        status, ctype, html = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        self.assertIn("djay Pro", html)
        self.assertIn("Google Chrome Helper", html)
        self.assertIn("com.algoriddim.djay-iphone-free", html)
        self.assertIn('checked', html)

    def test_apps_json(self):
        status, ctype, data = self.request("GET", "/apps.json")
        self.assertEqual(status, 200)
        self.assertIn("application/json", ctype)
        body = json.loads(data)
        self.assertEqual([a["pid"] for a in body["apps"]], [101, 202])
        self.assertEqual(body["current"], "")

    def test_choosing_an_app_writes_the_tap_file(self):
        status, _, _ = self.request("POST", "/select", "app=com.algoriddim.djay-iphone-free")
        self.assertIn(status, (200, 303))
        with open(self.tap_file) as fh:
            self.assertEqual(fh.read().strip(), "com.algoriddim.djay-iphone-free")
        status, _, _ = self.request("POST", "/select", "app=")
        self.assertIn(status, (200, 303))
        with open(self.tap_file) as fh:
            self.assertEqual(fh.read().strip(), "", "an empty choice means: back to the virtual device")

    def test_mute_checkbox_writes_the_second_line(self):
        status, _, html = self.request("GET", "/")
        self.assertIn('name="mute"', html)
        status, _, _ = self.request("POST", "/select", "app=com.algoriddim.djay-iphone-free&mute=on")
        self.assertIn(status, (200, 303))
        with open(self.tap_file) as fh:
            self.assertEqual(fh.read().split("\n")[:2], ["com.algoriddim.djay-iphone-free", "mute"])
        status, _, html = self.request("GET", "/")
        self.assertIn('name="mute" checked', html)
        status, _, data = self.request("GET", "/apps.json")
        self.assertTrue(json.loads(data)["mute"])
        self.request("POST", "/select", "app=com.algoriddim.djay-iphone-free")
        with open(self.tap_file) as fh:
            self.assertEqual(fh.read().strip(), "com.algoriddim.djay-iphone-free")

    def test_rejects_junk(self):
        status, _, _ = self.request("POST", "/select", "app=../../etc/passwd")
        self.assertEqual(status, 400)
        self.assertFalse(os.path.exists(self.tap_file))
        status, _, _ = self.request("GET", "/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
