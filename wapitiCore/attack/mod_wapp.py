#!/usr/bin/env python3
# This file is part of the Wapiti project (https://wapiti-scanner.github.io)
# Copyright (C) 2020-2023 Nicolas Surribas
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
import json
import os
import asyncio
import shutil
import string
from typing import Dict, Tuple, Optional, List
import re
from urllib.parse import urlparse

from httpx import RequestError

from arsenic import get_session, browsers, services
from arsenic.errors import JavascriptError, UnknownError, ArsenicError

from wapitiCore.main.log import logging, log_blue
from wapitiCore.main.wapiti import is_valid_url
from wapitiCore.attack.attack import Attack
from wapitiCore.controller.wapiti import InvalidOptionValue
from wapitiCore.net.response import Response
from wapitiCore.wappalyzer.wappalyzer import Wappalyzer, ApplicationData, ApplicationDataException
from wapitiCore.definitions.fingerprint import NAME as TECHNO_DETECTED, WSTG_CODE as TECHNO_DETECTED_WSTG_CODE
from wapitiCore.definitions.fingerprint_webserver import NAME as WEB_SERVER_VERSIONED, WSTG_CODE as WEB_SERVER_WSTG_CODE
from wapitiCore.definitions.fingerprint_webapp import NAME as WEB_APP_VERSIONED, WSTG_CODE as WEB_APP_WSTG_CODE
from wapitiCore.net import Request

MSG_TECHNO_VERSIONED = "{0} {1} detected"
MSG_CATEGORIES = "  -> Categories: {0}"
MSG_GROUPS = "  -> Group(s): {0}"
MSG_CPE = "  -> CPE: {0}"

BULK_SIZE = 50
VERSION_REGEX = re.compile(r"\d[\d.]*")

SCRIPT = (
    "wapiti_results = {};\n"
    "for (var js_tech in wapiti_tests) {\n"
    "  for (var i in wapiti_tests[js_tech]) {\n"
    "    try {\n"
    "      wapiti_results[js_tech] = [String(eval(wapiti_tests[js_tech][i])), wapiti_tests[js_tech][i]]; break;\n"
    "    } catch(wapiti_error) {\n"
    "      continue;\n"
    "    }\n"
    "  }\n"
    "}\n"
    "return wapiti_results;\n"
)


def _is_valid_json(response):
    try:
        return json.loads(response.content)
    except json.JSONDecodeError:
        return False


def is_json_file(filepath):
    try:
        with open(filepath, 'r', encoding="utf-8") as f:
            json.load(f)
    except json.JSONDecodeError as json_error:
        raise ValueError(f"{filepath} is not a valid JSON.") from json_error
    except FileNotFoundError as file_not_found_error:
        raise OSError(f"{filepath} does not exist !") from file_not_found_error
    return True


def merge_json_files(directory_path, output_file):
    # Get a list of all files in the directory
    files = [f for f in os.listdir(directory_path) if f.endswith('.json')]

    # Check if there are any JSON files in the directory
    if not files:
        raise OSError(f"{directory_path} does not contain any file !")

    # Initialize an empty dictionary to store the merged JSON data
    merged_data = {}

    # Iterate through each JSON file and merge its contents
    for file_name in files:
        file_path = os.path.join(directory_path, file_name)

        try:
            with open(file_path, 'r', encoding='utf-8') as file:
                file_data = json.load(file)
                merged_data.update(file_data)
        except json.JSONDecodeError as json_error:
            raise ValueError(f"{file_name} is not a valid JSON.") from json_error

    # Write the merged JSON data to the output file
    with open(output_file, 'w', encoding='utf-8') as output:
        json.dump(merged_data, output, ensure_ascii=False, indent=2)


def get_tests(data: dict):
    tests = {}

    for tech in data:
        if "js" not in data[tech] or not data[tech]["js"]:
            continue

        tuples = sorted(data[tech]["js"].items(), key=lambda x: len(x[1]), reverse=True)
        tests[tech] = [key_value[0] for key_value in tuples]
        if len(tests) >= BULK_SIZE:
            yield tests
            tests = {}

    if tests:
        yield tests


class ModuleWapp(Attack):
    """
    Identify web technologies used by the web server using Wappalyzer database.
    """

    name = "wapp"

    BASE_URL = Attack.wapp_url
    WAPP_DIR = Attack.wapp_dir
    WAPP_CATEGORIES = "categories.json"
    WAPP_GROUPS = "groups.json"
    WAPP_TECHNOLOGIES = "technologies.json"
    user_config_dir = None
    finished = False

    def __init__(self, crawler, persister, attack_options, stop_event, crawler_configuration):
        Attack.__init__(self, crawler, persister, attack_options, stop_event, crawler_configuration)
        self.user_config_dir = self.persister.CONFIG_DIR
        if not os.path.isdir(self.user_config_dir):
            os.makedirs(self.user_config_dir)

    async def copy_files_to_conf(self, files_to_copy: List[str]):
        """
        This function copies wapp DB files specified as arguments to the config directory.
        """
        for source_file in files_to_copy:
            # Check if file exists before attempting to copy
            if not os.path.isfile(source_file):
                logging.error(f"Warning: File {source_file} does not exist, skipping.")
                continue

            # Construct the destination file path using the filename
            destination_file = os.path.join(self.user_config_dir, os.path.basename(source_file))

            try:
                shutil.copy(source_file, destination_file)
                logging.info(f"Copied {source_file} to {destination_file}")
            except shutil.Error as err:
                logging.error(f"Error copying {source_file}: {err}")

    async def update(self):
        """Update the Wappalizer database from the web and load the patterns."""

        wapp_categories_url = f"{self.BASE_URL}src/categories.json"
        wapp_technologies_base_url = f"{self.BASE_URL}src/technologies/"
        wapp_groups_url = f"{self.BASE_URL}src/groups.json"
        if self.WAPP_DIR:
            categories_file_path = os.path.join(self.WAPP_DIR, self.WAPP_CATEGORIES)
            groups_file_path = os.path.join(self.WAPP_DIR, self.WAPP_GROUPS)
            technologies_directory_path = os.path.join(self.WAPP_DIR, "technologies/")
            technologies_file_path = os.path.join(self.WAPP_DIR, self.WAPP_TECHNOLOGIES)
            try:
                merge_json_files(technologies_directory_path, technologies_file_path)
            except (ValueError, OSError) as error:
                logging.error(error)
                raise ValueError(f"Update failed : Something went wrong with files in {self.WAPP_DIR}") from error
            try:
                if is_json_file(categories_file_path) and is_json_file(groups_file_path) \
                        and is_json_file(technologies_file_path):
                    files_list = [categories_file_path, groups_file_path, technologies_file_path]
                    try:
                        await self.copy_files_to_conf(files_list)
                    except ValueError:
                        return
                else:
                    return
            except (ValueError, OSError) as error:
                logging.error(error)
                raise ValueError(f"Update failed : Something went wrong with files in {self.WAPP_DIR}") from error

        elif self.BASE_URL:
            if not is_valid_url(self.BASE_URL):
                raise InvalidOptionValue(
                    "--wapp-url", self.BASE_URL
                )
            try:
                await self._load_wapp_database(
                    wapp_categories_url,
                    wapp_technologies_base_url,
                    wapp_groups_url
                )
            except RequestError as e:
                logging.error(f"RequestError occurred: {e}")
                raise
            except IOError:
                logging.error("Error downloading wapp database.")
            except ValueError as e:
                logging.error(f"Value error: {e}")
                raise

    async def must_attack(self, request: Request, response: Optional[Response] = None):
        if self.finished:
            return False

        if request.method == "POST":
            return False

        return request.url == await self.persister.get_root_url()

    async def attack(self, request: Request, response: Optional[Response] = None):
        self.finished = True
        request_to_root = Request(request.url)
        categories_file_path = os.path.join(self.user_config_dir, self.WAPP_CATEGORIES)
        groups_file_path = os.path.join(self.user_config_dir, self.WAPP_GROUPS)
        technologies_file_path = os.path.join(self.user_config_dir, self.WAPP_TECHNOLOGIES)

        try:
            await self._verify_wapp_database(categories_file_path, technologies_file_path, groups_file_path)
        except ValueError:
            return

        try:
            application_data = ApplicationData(categories_file_path, groups_file_path, technologies_file_path)
        except FileNotFoundError as exception:
            logging.error(exception)
            logging.error("Try using --store-session option, or update apps.json using --update option.")
            return
        except ApplicationDataException as exception:
            logging.error(exception)
            return

        detected_applications, response = await self._detect_applications(request.url, application_data)

        if detected_applications:
            log_blue("---")

        for application_name in sorted(detected_applications, key=lambda x: x.lower()):

            versions = detected_applications[application_name]["versions"]
            categories = detected_applications[application_name]["categories"]
            groups = detected_applications[application_name]["groups"]
            cpe = detected_applications[application_name]["cpe"]

            log_blue(MSG_TECHNO_VERSIONED, application_name, versions)
            log_blue(MSG_CATEGORIES, categories)
            log_blue(MSG_GROUPS, groups)
            if cpe:
                log_blue(MSG_CPE, cpe)
            log_blue("")
            await self.add_addition(
                category=TECHNO_DETECTED,
                request=request_to_root,
                info=json.dumps(detected_applications[application_name]),
                wstg=TECHNO_DETECTED_WSTG_CODE,
                response=response
            )

            if versions:
                if "Web servers" in categories:
                    await self.add_vuln_info(
                        category=WEB_SERVER_VERSIONED,
                        request=request_to_root,
                        info=json.dumps(detected_applications[application_name]),
                        wstg=WEB_SERVER_WSTG_CODE,
                        response=response
                    )
                else:
                    await self.add_vuln_info(
                        category=WEB_APP_VERSIONED,
                        request=request_to_root,
                        info=json.dumps(detected_applications[application_name]),
                        wstg=WEB_APP_WSTG_CODE,
                        response=response
                    )

    async def _detect_applications(
            self,
            url: str,
            application_data: ApplicationData
    ) -> Tuple[Dict, Optional[Response]]:
        detected_applications = {}
        response = None

        if self.options.get("headless", "no") != "no":
            headless_results = await self._detect_applications_headless(url)
        else:
            headless_results = {}

        # Detecting the applications for the url with and without the follow_redirects flag
        for follow_redirect in [True, False]:
            request = Request(url)

            try:
                response = await self.crawler.async_send(request, follow_redirects=follow_redirect)
            except RequestError:
                self.network_errors += 1
                continue

            wappalyzer = Wappalyzer(application_data, response, headless_results)
            detected_applications.update(wappalyzer.detect())

        return detected_applications, response

    async def _dump_url_content_to_file(self, url: str, file_path: str):
        request = Request(url)
        try:
            response = await self.crawler.async_send(request)
        except RequestError:
            self.network_errors += 1
            raise
        if response.status != 200:
            logging.error(f"Error: Non-200 status code for {url}")
            return
        if not _is_valid_json(response):
            raise ValueError(f"Invalid or empty JSON response for {url}")
        with open(file_path, 'w', encoding='utf-8') as file:
            json.dump(response.json, file)

    async def _load_wapp_database(self, categories_url: str, technologies_base_url: str, groups_url: str):
        categories_file_path = os.path.join(self.user_config_dir, self.WAPP_CATEGORIES)
        groups_file_path = os.path.join(self.user_config_dir, self.WAPP_GROUPS)
        technologies_file_path = os.path.join(self.user_config_dir, self.WAPP_TECHNOLOGIES)
        technology_files_names = list(map(lambda file_name: file_name + ".json", list("_" + string.ascii_lowercase)))
        technologies = {}

        # Requesting all technologies one by one
        for technology_file_name in technology_files_names:
            request = Request(technologies_base_url + technology_file_name)
            try:
                response: Response = await self.crawler.async_send(request)
            except RequestError:
                self.network_errors += 1
                raise
            if response.status != 200:
                raise ValueError(f"{technologies_base_url} is not a valid URL for a wapp database")
            # Merging all technologies in one object
            for technology_name in response.json:
                technologies[technology_name] = response.json[technology_name]
            try:
            # Saving categories & groups
                await asyncio.gather(
                    self._dump_url_content_to_file(categories_url, categories_file_path),
                    self._dump_url_content_to_file(groups_url, groups_file_path))
            except RequestError as req_error:
                logging.error(f"Caught a RequestError: {req_error}")
                raise
            except ValueError as value_error:
                logging.error(f"Caught a ValueError: {value_error}")
                raise
            # Saving technologies
            with open(technologies_file_path, 'w', encoding='utf-8') as file:
                json.dump(technologies, file)

    async def _verify_wapp_database(
            self,
            categories_file_path: str,
            technologies_base_path: str,
            groups_file_path: str
    ):
        try:
            with open(categories_file_path, encoding='utf-8') as categories_file, \
                    open(technologies_base_path, encoding='utf-8') as technologies_file, \
                    open(groups_file_path, encoding='utf-8') as groups_file:
                json.load(categories_file)
                json.load(technologies_file)
                json.load(groups_file)
        except IOError:
            logging.warning("Problem with local wapp database.")
            logging.info("Downloading from the web...")
            await self.update()

    async def _detect_applications_headless(self, url: str) -> dict:
        proxy_settings = None
        if self.crawler_configuration.proxy:
            proxy = urlparse(self.crawler_configuration.proxy).netloc
            proxy_settings = {
                "proxyType": 'manual',
                "httpProxy": proxy,
                "sslProxy": proxy
            }

        service = services.Geckodriver(log_file=os.devnull)
        browser = browsers.Firefox(
            proxy=proxy_settings,
            acceptInsecureCerts=True,
            **{
                "moz:firefoxOptions": {
                    "prefs": {
                        "network.proxy.allow_hijacking_localhost": True,
                        "devtools.jsonview.enabled": False,
                    },
                    "args": ["-headless"]
                }
            }
        )

        technologies_file_path = os.path.join(self.user_config_dir, self.WAPP_TECHNOLOGIES)
        final_results = {}

        with open(technologies_file_path, encoding="utf-8") as fd:
            data = json.load(fd)
            try:
                async with get_session(service, browser) as headless_client:
                    await headless_client.get(url, timeout=self.crawler_configuration.timeout)
                    await asyncio.sleep(5)
                    for tests in get_tests(data):
                        script = f"wapiti_tests = {json.dumps(tests)};\n" + SCRIPT
                        try:
                            results = await headless_client.execute_script(script)
                            for software, version_and_js in results.items():
                                version, js = version_and_js
                                expected_format = data[software]["js"][js]
                                if version == "undefined":
                                    continue

                                if not expected_format or expected_format.startswith(r"\;"):
                                    if VERSION_REGEX.match(version):
                                        final_results[software] = [version]
                                    else:
                                        final_results[software] = []
                                elif isinstance(version, str) and r"\;version:" in expected_format:
                                    final_results[software] = [version]
                                # Other cases seems to be some kind of false positives
                            # final_results.update(results)
                        except (JavascriptError, UnknownError) as exception:
                            logging.exception(exception)
                            continue

            except (ArsenicError, FileNotFoundError, asyncio.TimeoutError):
                # Geckodriver may be missing, etc
                pass

        return final_results
