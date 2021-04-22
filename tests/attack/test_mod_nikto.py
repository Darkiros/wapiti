from unittest.mock import Mock
import re
import os
from asyncio import Event

import httpx
import respx
import pytest

from wapitiCore.net.web import Request
from wapitiCore.net.crawler import AsyncCrawler
from wapitiCore.attack.mod_nikto import mod_nikto


class FakePersister:
    CONFIG_DIR_NAME = "config"
    HOME_DIR = os.getenv("HOME") or os.getenv("USERPROFILE")
    BASE_DIR = os.path.join(HOME_DIR, ".wapiti")
    CONFIG_DIR = os.path.join(BASE_DIR, CONFIG_DIR_NAME)

    def __init__(self):
        self.requests = []
        self.additionals = set()
        self.anomalies = set()
        self.vulnerabilities = []

    def get_links(self, path=None, attack_module: str = ""):
        for request in self.requests:
            if request.method == "GET":
                yield request

    def get_forms(self, attack_module: str = ""):
        return [request for request in self.requests if request.method == "POST"]

    def add_additional(self, request_id: int = -1, category=None, level=0, request=None, parameter="", info=""):
        self.additionals.add(request)

    def add_anomaly(self, request_id: int = -1, category=None, level=0, request=None, parameter="", info=""):
        pass

    def add_vulnerability(self, request_id: int = -1, category=None, level=0, request=None, parameter="", info=""):
        self.vulnerabilities.append((request, info))


@pytest.mark.asyncio
@respx.mock
async def test_whole_stuff():
    # Test attacking all kind of parameter without crashing
    respx.route(host="raw.githubusercontent.com").pass_through()

    respx.get("http://perdu.com/cgi-bin/a1disp3.cgi?../../../../../../../../../../etc/passwd").mock(
        return_value=httpx.Response(200, text="root:0:0:")
    )

    respx.route(host="perdu.com").mock(
        return_value=httpx.Response(404, text="Not found")
    )

    persister = FakePersister()

    request = Request("http://perdu.com/")
    request.path_id = 1
    request.status = 200
    request.set_headers({"content-type": "text/html"})
    persister.requests.append(request)

    crawler = AsyncCrawler("http://perdu.com/", timeout=1)
    options = {"timeout": 10, "level": 2}
    logger = Mock()

    module = mod_nikto(crawler, persister, logger, options, Event())
    module.verbose = 2
    module.do_get = True
    await module.attack(request)

    assert len(persister.vulnerabilities) == 1
    assert persister.vulnerabilities[0][0].url == (
        "http://perdu.com/cgi-bin/a1disp3.cgi?..%2F..%2F..%2F..%2F..%2F..%2F..%2F..%2F..%2F..%2Fetc%2Fpasswd"
    )
    assert "This CGI allows attackers read arbitrary files on the host" in persister.vulnerabilities[0][1]
