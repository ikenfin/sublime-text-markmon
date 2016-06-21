# coding=utf8

import sublime
import time
import sublime_plugin
import webbrowser
import subprocess
import atexit
import re
from .MarkmonListener import MarkmonListener
import http.client
from . import util
import os
from threading import Timer

MARKDOWN_SYNTAX = re.compile(r'.*[Mm]arkdown.*')

markmon = None
def plugin_loaded():
    """The ST3 entry point for plugins."""
    global markmon
    markmon = Markmon(MarkmonListener.shared_plugin())

class Markmon:
    def __init__(self, listener):
        listener = MarkmonListener.shared_plugin()
        listener.load_settings()

        self.settings = MarkmonSettings()
        self.client = MarkmonClient(self.settings)
        self.server = MarkmonServer(self.settings)
        self.client.set_server(self.server)

        listener.add_on_settings_change(self.settings_updated)
        listener.add_on_modified(self.client.view_updated)

    def settings_updated(self, settings):
        self.settings.update(settings)
        self.server.setup_server()

    def display(self):
        current_view = sublime.active_window().active_view()
        self.client.view_updated(current_view)
        webbrowser.open("http://" + self.settings.client_url)

    def set_running(self, running):
        if running != self.settings.running:
            self.settings.running = running
            if running:
                self.server.setup_server()
                # need a delay to wait for the server to set up
                Timer(1.0, self.display).start()
            else:
                self.server.cleanup_server()
        elif running:
            self.display()


class MarkmonSettings:
    def __init__(self):
        self.running = False
        self.settings = {}
        self.server_command = []
        self.client_url = ""

    def update(self, settings):
        self.settings = {
            "executable": settings.get("executable", 'markmon'),
            "port": settings.get("port", 3000),
            "command": settings.get("command", "pandoc -t HTML5"),
            "stylesheet": settings.get("stylesheet", None),
            "projectdir": settings.get("projectdir", None)
        }
        self.build_strings()

    def build_strings(self):
        self.client_url = "localhost:{:d}".format(self.settings['port'])
        self.server_command = [self.settings["executable"],
                                    "--port", str(self.settings['port']),
                                    "--command", self.settings['command']]
        if self.settings['stylesheet']:
            self.server_command.append("--stylesheet")
            self.server_command.append(self.settings['stylesheet'])

        if self.settings['projectdir']:
            self.server_command.append("--projectdir")
            self.server_command.append(self.settings['projectdir'])

class MarkmonClient:
    def __init__(self, settings):
        self.settings = settings
        self.server = None

    def set_server(self, server):
        self.server = server

    def view_updated(self, view, try_server=True):
         if self.settings.running and MARKDOWN_SYNTAX.match(view.scope_name(0)):
            try:
                connection = http.client.HTTPConnection(self.settings.client_url)
                # connection.request('PUT', '/', view.substr(sublime.Region(0, view.size())).encode('utf-8'))
                openedfile_path = sublime.active_window().active_view().file_name()
                shebang_comment = u'<!--FILEPATH:[' + openedfile_path + u'];-->'
                payload = b"".join([shebang_comment.encode('utf-8'), view.substr(sublime.Region(0, view.size())).encode('utf-8')])
                connection.request('PUT', '/', payload)
                connection.getresponse()
            except ConnectionRefusedError:
                if self.server:
                    if try_server:
                        self.server.setup_server()
                        time.sleep(1)
                        self.view_updated(view, False)
                    else:
                        print("Markmon server is down. Check your preferences.")

class MarkmonServer:
    def __init__(self, settings):
        self.server_url = None
        self.settings = settings
        atexit.register(self.cleanup_server)

    def setup_server(self, _=None):
        try:
            self.cleanup_server()
        except:
            pass
        if not self.settings.running:
            return
        self.server_url = self.settings.client_url
        env = os.environ.copy()
        betterenv = util.create_environment()
        env["PATH"] = betterenv["PATH"]
        try:
            subprocess.Popen(self.settings.server_command, env=env)
        except FileNotFoundError as e:
            print("Markmon Server failed to initialize. Confirm executable path is correct in Markmon Setting. Command used:")
            print(self.settings.server_command)
            raise

    def cleanup_server(self):
        if self.server_url:
            connection = http.client.HTTPConnection(self.server_url)
            connection.request('DELETE', '/')
            connection.getresponse()
